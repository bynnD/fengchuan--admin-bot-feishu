# 开票申请

import json
import logging
import os
from file_extraction import extract_text_from_file
from deepseek_client import call_deepseek_with_retry

logger = logging.getLogger(__name__)

NAME = "开票申请"
APPROVAL_CODE = "692F47D-F6CF-4342-8DAC-32CE84F39E6F"
LINK_ONLY = False
HAS_FILE_EXTRACTION = True  # 从结算单和合同自动提取字段

FIELD_HINTS = (
    "invoice_type(发票类型,用户必须明确说明), invoice_items(开票项目,用户必须明确说明), "
    "amount(金额,从结算单识别), buyer_name(购方名称/开票抬头,从合同识别), tax_id(购方税号,从合同识别), "
    "settlement_file(结算单附件), contract_file(合同附件)"
)

# 用户必须明确说明
USER_REQUIRED_FIELDS = ["invoice_type", "invoice_items"]
# 从结算单/合同自动识别
AUTO_FIELDS = ["amount", "buyer_name", "tax_id", "contract_no", "settlement_no"]

CONVERSATION_FIELDS = ["invoice_type", "invoice_items"]
IMAGE_FIELDS = AUTO_FIELDS

FIELD_LABELS = {
    "invoice_type":   "发票类型",
    "invoice_items": "开票项目",
    "amount":         "金额",
    "buyer_name":    "购方名称/开票抬头",
    "tax_id":        "购方税号",
    "contract_no":   "合同编号",
    "settlement_no": "结算单编号",
    "remarks":       "备注",
}

# 表单字段名可能为「购方名称」「开票抬头」「客户/开票名称」等，均映射到 buyer_name；
# 「购方税号」「税务登记证号」「社会统一信用代码」等映射到 tax_id；「开票金额」「发票金额」映射到 amount
FIELD_NAME_ALIASES = {
    "购方名称": "buyer_name", "开票抬头": "buyer_name", "客户/开票名称": "buyer_name",
    "购方税号": "tax_id", "税务登记证号": "tax_id", "社会统一信用代码": "tax_id",
    "税务登记证号/社会统一信用代码": "tax_id",
    "开票金额": "amount", "发票金额": "amount",
}
# 表单字段 ID 占位，实际值由 get_form_fields 缓存或 debug-form 获取后填写
FIELD_ID_FALLBACK = {}
FIELD_ORDER = ["invoice_type", "invoice_items", "amount", "buyer_name", "tax_id", "contract_no", "settlement_no", "remarks"]
DATE_FIELDS = set()
SUPPORTS_IMAGE = True


def get_admin_comment(fields):
    return "请核实以上填报信息无误后提交"


def extract_fields_from_file(file_content, file_name, form_opts, get_token):
    """
    从结算单或合同文件中提取开票相关字段。
    结算单：金额、结算单编号等
    合同：购方名称、税号、合同编号等
    """
    base_name = file_name.rsplit(".", 1)[0] if "." in file_name else file_name
    file_text = extract_text_from_file(file_content, file_name, get_token)
    has_content = bool(file_text and len(file_text.strip()) > 10)
    combined = f"文件名：{base_name}\n\n" + (f"文件内容摘要：\n{file_text}" if has_content else "（内容无法提取）")

    # 根据文件名判断是结算单还是合同，给 AI 不同提示
    is_settlement = "结算" in base_name or "结算单" in base_name
    is_contract = "合同" in base_name or "协议" in base_name
    doc_type = "结算单" if is_settlement else ("合同" if is_contract else "未知")

    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        logger.warning("开票提取: DEEPSEEK_API_KEY 未配置")
        return {}

    prompt = (
        f"从以下{doc_type}文件中提取开票申请相关字段。\n\n{combined}\n\n"
        f"请返回JSON，包含能识别的字段：\n"
        f"- amount: 金额（数字，如 10000 或 10000.00）\n"
        f"- buyer_name: 购方名称/开票抬头（合同中的甲方、乙方或购买方）\n"
        f"- tax_id: 购方税号/纳税人识别号\n"
        f"- contract_no: 合同编号\n"
        f"- settlement_no: 结算单编号\n"
        f"只返回能明确识别的字段，不要猜测。只返回JSON，不要其他内容。"
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
            return {}
        result = {}
        for k in AUTO_FIELDS:
            v = out.get(k)
            if v and str(v).strip():
                result[k] = str(v).strip()
        return result
    except Exception as e:
        import traceback
        logger.warning("开票申请从文件提取失败: %s", e)
        traceback.print_exc()
        return {}
