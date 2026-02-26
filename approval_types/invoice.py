# 开票申请单

import json
import logging
import os
from file_extraction import extract_text_from_file
from deepseek_client import call_deepseek_with_retry

logger = logging.getLogger(__name__)

NAME = "开票申请单"
APPROVAL_CODE = "6706BE14-0ED3-4718-8E57-DF5F52935BDE"
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
AUTO_FIELDS = ["amount", "buyer_name", "tax_id", "contract_no", "settlement_no", "business_type", "proof_file_type"]

CONVERSATION_FIELDS = ["invoice_type", "invoice_items"]
IMAGE_FIELDS = AUTO_FIELDS

FIELD_LABELS = {
    "invoice_type":   "发票类型",
    "invoice_items": "开票项目",
    "amount":         "开票金额",
    "buyer_name":    "购方名称/开票抬头",
    "tax_id":        "购方税号",
    "contract_no":   "合同编号",
    "settlement_no": "结算单编号",
    "remarks":       "备注",
    "business_type": "业务类型",
    "proof_file_type": "开票证明文件类型",
    "contract_sealed": "开票合同是否盖章",
}

# 表单字段名可能为「购方名称」「开票抬头」「客户/开票名称」等，均映射到 buyer_name；
# 「购方税号」「税务登记证号」「社会统一信用代码」等映射到 tax_id；「开票金额」「发票金额」映射到 amount
FIELD_NAME_ALIASES = {
    "购方名称": "buyer_name", "开票抬头": "buyer_name", "客户/开票名称": "buyer_name",
    "购方税号": "tax_id", "税务登记证号": "tax_id", "社会统一信用代码": "tax_id",
    "税务登记证号/社会统一信用代码": "tax_id",
    "开票金额": "amount", "发票金额": "amount",
    "开票证明文件类型（可多选）": "proof_file_type",
    "开票合同是否盖章": "contract_sealed",
}
# 与 6706BE14 审批定义一致
FIELD_ID_FALLBACK = {
    "buyer_name":      "widget17334740014470001",   # 客户/开票名称
    "tax_id":          "widget17334740172520001",   # 税务登记证号/社会统一信用代码
    "invoice_type":    "widget16457794296140001",   # 发票类型
    "invoice_items":   "widget17660282371600001",   # 开票项目
    "amount":          "widget17334740447380001",   # 开票金额
    "business_type":   "widget17660274322300001",   # 业务类型
    "proof_file_type": "widget17660291688960001",   # 开票证明文件类型（可多选）
    "contract_sealed": "widget17334733872820001",   # 开票合同是否盖章
}
FIELD_ORDER = ["invoice_type", "invoice_items", "amount", "buyer_name", "tax_id", "business_type", "proof_file_type", "contract_sealed", "contract_no", "settlement_no", "remarks"]
DATE_FIELDS = set()
SUPPORTS_IMAGE = True


def get_admin_comment(fields):
    return "请核实以上填报信息无误后提交"


def extract_fields_from_file(file_content, file_name, form_opts, get_token):
    """
    从开票凭证文件中提取相关字段。支持多种凭证：
    结算单/对账单、合同/协议、银行水单、订单明细、电商发货/收款截图等。
    """
    base_name = file_name.rsplit(".", 1)[0] if "." in file_name else file_name
    file_text = extract_text_from_file(file_content, file_name, get_token)
    has_content = bool(file_text and len(file_text.strip()) > 10)
    combined = f"文件名：{base_name}\n\n" + (f"文件内容摘要：\n{file_text}" if has_content else "（内容无法提取）")

    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        logger.warning("开票提取: DEEPSEEK_API_KEY 未配置")
        return {}

    prompt = (
        f"从以下开票凭证文件中提取相关字段。支持多种凭证类型：结算单/对账单、合同/协议、银行水单、订单明细、电商发货截图、收款截图等。\n\n{combined}\n\n"
        f"请返回JSON，包含能识别的字段：\n"
        f"- amount: 开票金额。仅从结算单、对账单、银行流水、订单明细、电商发货/收款截图中提取。"
        f"若为 Excel 表格，找表头为「合计费用」「合计金额」「总金额」「应付金额」等列，取该列中的数字（如 800、10000.00）；表格常有多列（上调费用、维护费用等），取「合计」行的合计费用列。"
        f"若为普通文档，找上述字样后的数字。合同类文件通常不含金额，不要从合同中猜测。返回数字。\n"
        f"- buyer_name: 购方名称/开票抬头（合同中的甲方、乙方或购买方）\n"
        f"- tax_id: 购方税号/纳税人识别号\n"
        f"- contract_no: 合同编号\n"
        f"- settlement_no: 结算单编号\n"
        f"- business_type: 业务类型。仅从合同/协议类文件中提取（如广告、技术、电商等，从合同中的服务内容、业务描述推断）。结算单、对账单、银行流水、订单明细、截图等非合同类文件不要返回 business_type。\n"
        f"- proof_file_type: 开票证明文件类型，可多选。可选值：合同、对账单、有赞商城后台订单、微信小店后台订单、企业微信收款订单、其他。"
        f"根据文件内容判断：结算单/对账单→对账单；合同/协议→合同；银行流水/水单→其他；订单明细/发货截图/收款截图→有赞商城后台订单或微信小店后台订单或企业微信收款订单或其他（能区分平台则选对应项）\n"
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
            if v is None:
                continue
            if k == "proof_file_type":
                if isinstance(v, list):
                    items = [str(x).strip() for x in v if x and str(x).strip()]
                    if items:
                        result[k] = items
                elif str(v).strip():
                    result[k] = [str(v).strip()]
            elif v and str(v).strip():
                result[k] = str(v).strip()
        return result
    except Exception as e:
        import traceback
        logger.warning("开票申请单从文件提取失败: %s", e)
        traceback.print_exc()
        return {}
