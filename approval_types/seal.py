# 用印申请单（40D94E43 表单仅含备注，需通过链接在飞书中发起）

import json
import logging
import os
from file_extraction import extract_text_from_file
from deepseek_client import call_deepseek_with_retry

logger = logging.getLogger(__name__)

NAME = "用印申请单"
APPROVAL_CODE = "40D94E43-270A-4B16-BCC2-B7A71B6EA7BF"
LINK_ONLY = True  # 新表单仅含备注，不支持 API 创建，走链接流程
HAS_FILE_EXTRACTION = False  # LINK_ONLY 时不再走文件提取流程

FIELD_HINTS = "remarks(备注,可选)"

CONVERSATION_FIELDS = []
IMAGE_FIELDS = []

FIELD_LABELS = {
    "remarks": "备注",
}

FIELD_NAME_ALIASES = {"备注": "remarks"}

# 40D94E43 表单字段 ID
FIELD_ID_FALLBACK = {
    "remarks": "widget17375349954340001",
}

FIELD_ORDER = ["remarks"]
DATE_FIELDS = set()

SUPPORTS_IMAGE = False


def get_admin_comment(fields):
    return "请核实以上填报信息无误后提交"


# 保留供 LINK_ONLY 时若有其他入口使用
DEFAULT_COMPANY_OPTS = ["风船", "微驰", "拓梦", "亿帆", "万数汇", "耀玩社", "利斯特", "利信", "利智", "海南风汇万聚", "海南万数汇集"]
DEFAULT_SEAL_OPTS = ["公章", "合同章", "法人章", "财务章"]


def extract_fields_from_file(file_content, file_name, form_opts, get_token):
    """根据文件内容（含 OCR）用 AI 推断用印公司、印章类型、用印事由。供通用文件处理流程调用。"""
    base_name = file_name.rsplit(".", 1)[0] if "." in file_name else file_name
    file_text = extract_text_from_file(file_content, file_name, get_token)
    has_content = bool(file_text and len(file_text.strip()) > 10)
    if not has_content and file_name:
        logger.debug("用印提取: 文件内容为空或过短，将仅根据文件名推断，len=%d", len(file_text or ''))
    combined = f"文件名：{base_name}\n\n" + (f"文件内容摘要：\n{file_text}" if has_content else "（文件内容无法提取，请仅根据文件名推断）")
    if not combined.strip() or len(combined.strip()) < 3:
        return {}
    company_opts = form_opts.get("company") or DEFAULT_COMPANY_OPTS
    seal_opts = form_opts.get("seal_type") or DEFAULT_SEAL_OPTS
    company_str = "、".join(company_opts) if company_opts else "无"
    seal_str = "、".join(seal_opts) if seal_opts else "公章、合同章、法人章、财务章"
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        logger.warning("用印提取: DEEPSEEK_API_KEY 未配置")
        return {}
    extra = "" if has_content else (
        "\n【重要】文件名中包含关键信息，如「扇贝&风船-流量广告合作协议」表示涉及风船公司、合同类文件。"
        "请据此推断：company 从文件名中的公司名匹配上述选项（风船、扇贝等），seal_type 选合同章或公章，reason 填合同/协议名称，document_type 从文件名推断业务类型。"
    )
    prompt = (
        f"根据合同/文件内容识别用印申请信息。\n\n{combined}\n\n"
        f"可选用印公司：{company_str}\n可选印章类型：{seal_str}\n"
        f"请返回JSON，必须包含能推断的字段。company 和 seal_type 的值必须与上述选项完全一致。\n"
        f"- company: 从合同甲方乙方、签约方、文件名中的公司名匹配上述用印公司选项（如「扇贝&风船」可推断风船）\n"
        f"- seal_type: 合同/协议类用合同章或公章，财务类用财务章\n"
        f"- reason: 文件用途/用印事由（如「流量广告合作协议」）\n"
        f"- document_type: 文件业务类型。根据文件名和内容推断，可选值：结算单（结算单、对账单、月结单等）、合作协议（合作协议、框架协议、合作框架等）、合同（合同、采购合同、服务协议等）、保密协议等特殊交办文件（仅当明确为保密协议、特殊交办时）。"
        f"文件名含「结算」「对账」「月结」→结算单；含「协议」「合作」→合作协议；含「合同」→合同。不要将结算单、合作协议误判为「保密协议等特殊交办文件」。\n"
        f"{extra}\n只返回JSON，不要其他内容。"
    )
    try:
        res = call_deepseek_with_retry(
            [{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            timeout=15,
            max_retries=2,
            api_key=api_key,
        )
        content = res.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        out = json.loads(content)
        if not out:
            logger.warning("用印提取: AI 返回空 JSON")
            return {}
        result = {}
        for k, v in out.items():
            if not v or k not in ("company", "seal_type", "reason", "document_type"):
                continue
            v = str(v).strip()
            if k == "company" and company_opts and v not in company_opts:
                # 返回值不在选项中时，尝试从选项中找包含关系（如「风船」匹配「海南风船」）
                matched = next((o for o in company_opts if v in o or o in v), None)
                v = matched if matched else v
            if k == "seal_type" and seal_opts and v not in seal_opts:
                matched = next((o for o in seal_opts if v in o or o in v), None)
                v = matched if matched else v
            if v:
                result[k] = v
        return result
    except Exception as e:
        import traceback
        logger.warning("从文件内容推断用印信息失败: %s", e)
        traceback.print_exc()
        return {}
