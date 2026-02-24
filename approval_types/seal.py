# 用印申请

import json
import logging
import os
from file_extraction import extract_text_from_file
from deepseek_client import call_deepseek_with_retry

logger = logging.getLogger(__name__)

NAME = "用印申请"
APPROVAL_CODE = "58F1B962-73D4-408F-8B1B-3FB1776CF2B8"
LINK_ONLY = False
HAS_FILE_EXTRACTION = True  # 有附件识别读取需求，使用通用文件内容提取（含 OCR）

FIELD_HINTS = (
    "company(用印公司,从文件内容识别), usage_method(盖章或外带,默认盖章), "
    "reason(文件用途/用印事由,从文件内容识别), seal_type(印章类型,从文件内容识别:公章/合同章/法人章/财务章), "
    "lawyer_reviewed(律师是否已审核:是/否,用户必须明确提供), "
    "document_name(文件名称,从上传文档识别), document_count(文件数量,默认1), "
    "document_type(文件类型,从上传文档识别), remarks(备注,可选)"
)

# 用户对话中提供
CONVERSATION_FIELDS = ["company", "usage_method", "reason", "seal_type", "lawyer_reviewed"]
# 从上传文档自动识别（含文件内容 AI 识别的用印公司、印章类型、用印事由）
IMAGE_FIELDS = ["document_name", "document_type", "document_count", "company", "seal_type", "reason"]

FIELD_LABELS = {
    "company":         "用印公司",
    "usage_method":    "盖章或外带",
    "reason":          "文件用途/用印事由",
    "seal_type":       "印章类型",
    "document_name":   "文件名称",
    "document_count":  "文件数量",
    "document_type":   "文件类型",
    "lawyer_reviewed": "律师是否已审核",
    "remarks":         "备注",
    "file_details":    "文件明细",  # 表格控件：每文件一行，列：文件名、律师审核、数量、盖章/外带
}

# 与飞书表单实际控件 ID 一致（从审批实例 form 获取）
FIELD_ID_FALLBACK = {
    "usage_method":    "widget17334699216260001",   # 盖章或外带印章
    "seal_type":       "widget15754438920110001",   # 印章类型
    "document_name":   "widget3",                    # 文件名称
    "document_count":  "widget4",                    # 文件数量
    "document_type":   "widget17334700336550001",   # 文件类型
    "lawyer_reviewed": "widget17334701422160001",   # 律师是否已审核
    "company":         "widget17375357884790001",    # 用印公司（若表单有）
    "reason":          "widget0",                     # 文件用途（若表单有）
    "remarks":         "widget17375349954340001",    # 备注（若表单有）
}

# 表单字段名与逻辑键映射（表单名「盖章或外带印章」对应 usage_method）
FIELD_NAME_ALIASES = {"盖章或外带印章": "usage_method"}

# 律师审核：对话中用「是」/「否」，表单可能用「已审核」/「未审核」，提交时映射
LAWYER_REVIEWED_VALUE_MAP = {"是": "已审核", "否": "未审核"}

FIELD_ORDER = ["company", "usage_method", "reason", "seal_type", "document_name", "document_count", "document_type", "lawyer_reviewed", "file_details", "remarks"]
DATE_FIELDS = set()

# 文件明细表格：在用印表单中新增「表格」控件，命名为「文件明细」，列依次为：文件名、律师审核、数量、盖章/外带
# 添加后删除 field_cache.json 或调用 invalidate_cache 以重新获取结构；若 API 未返回子字段，在此填写实际子字段 id
FIELDLIST_SUBFIELDS_FALLBACK = {
    "file_details": [
        {"id": "widget_file_name", "type": "input", "name": "文件名"},
        {"id": "widget_lawyer", "type": "input", "name": "律师审核"},
        {"id": "widget_count", "type": "number", "name": "数量"},
        {"id": "widget_usage", "type": "input", "name": "盖章/外带"},
    ]
}

SUPPORTS_IMAGE = True


def get_admin_comment(fields):
    return "请核实以上填报信息无误后提交"


# 用印公司默认选项（当表单未返回时使用）
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
        "请据此推断：company 从文件名中的公司名匹配上述选项（风船、扇贝等），seal_type 选合同章或公章，reason 填合同/协议名称。"
    )
    prompt = (
        f"根据合同/文件内容识别用印申请信息。\n\n{combined}\n\n"
        f"可选用印公司：{company_str}\n可选印章类型：{seal_str}\n"
        f"请返回JSON，必须包含能推断的字段。company 和 seal_type 的值必须与上述选项完全一致。\n"
        f"- company: 从合同甲方乙方、签约方、文件名中的公司名匹配上述用印公司选项（如「扇贝&风船」可推断风船）\n"
        f"- seal_type: 合同/协议类用合同章或公章，财务类用财务章\n"
        f"- reason: 文件用途/用印事由（如「流量广告合作协议」）\n"
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
            if not v or k not in ("company", "seal_type", "reason"):
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
