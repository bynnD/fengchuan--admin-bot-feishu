# 用印申请

import json
import os
import httpx
from file_extraction import extract_text_from_file

NAME = "用印申请"
APPROVAL_CODE = "FB855CD4-CA15-4A1B-8B7A-51A56171CE60"
LINK_ONLY = False
HAS_FILE_EXTRACTION = True  # 有附件识别读取需求，使用通用文件内容提取（含 OCR）

FIELD_HINTS = (
    "company(用印公司,从文件内容识别), usage_method(盖章或外带,默认盖章), "
    "reason(文件用途/用印事由,从文件内容识别), seal_type(印章类型,从文件内容识别:公章/合同章/法人章/财务章), "
    "lawyer_reviewed(律师是否已审核:是/否,默认否), "
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
}

FIELD_ID_FALLBACK = {
    "company":         "widget17375357884790001",
    "usage_method":    "widget17375347703620001",
    "reason":          "widget0",
    "seal_type":       "widget15754438920110001",
    "document_name":   "widget3",
    "document_count":  "widget4",
    "document_type":   "widget17375354078970001",
    "lawyer_reviewed": "widget17375349618880001",
    "remarks":         "widget17375349954340001",
}

FIELD_ORDER = ["company", "usage_method", "reason", "seal_type", "document_name", "document_count", "document_type", "lawyer_reviewed", "remarks"]
DATE_FIELDS = set()

SUPPORTS_IMAGE = True


def get_admin_comment(fields):
    return "行政审核：用印申请已核实，同意。"


def extract_fields_from_file(file_content, file_name, form_opts, get_token):
    """根据文件内容（含 OCR）用 AI 推断用印公司、印章类型、用印事由。供通用文件处理流程调用。"""
    base_name = file_name.rsplit(".", 1)[0] if "." in file_name else file_name
    file_text = extract_text_from_file(file_content, file_name, get_token)
    combined = f"文件名：{base_name}\n\n" + (f"文件内容摘要：\n{file_text}" if file_text else "（无法提取文本）")
    if not combined.strip() or len(combined.strip()) < 3:
        return {}
    company_opts = form_opts.get("company", [])
    seal_opts = form_opts.get("seal_type", ["公章", "合同章", "法人章", "财务章"])
    company_str = "、".join(company_opts) if company_opts else "无"
    seal_str = "、".join(seal_opts) if seal_opts else "公章、合同章、法人章、财务章"
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return {}
    prompt = (
        f"根据合同/文件内容识别用印申请信息。\n\n{combined}\n\n"
        f"可选用印公司：{company_str}\n可选印章类型：{seal_str}\n"
        f"请返回JSON，只包含能识别的字段，无法识别的不要写：\n"
        f"- company: 从合同甲方乙方、签约方等匹配上述用印公司选项\n"
        f"- seal_type: 合同类通常用公章或合同章，财务类用财务章，法人签字用法人章\n"
        f"- reason: 文件用途/用印事由（如合同名称、协议类型等）\n"
        f"只返回JSON，不要其他内容。"
    )
    try:
        res = httpx.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"}
            },
            timeout=15
        )
        res.raise_for_status()
        content = res.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        out = json.loads(content)
        return {k: v for k, v in out.items() if v and k in ("company", "seal_type", "reason")}
    except Exception as e:
        print(f"从文件内容推断用印信息失败: {e}")
        return {}
