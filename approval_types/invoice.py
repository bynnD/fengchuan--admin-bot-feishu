# 开票申请单

import json
import logging
import os
from file_extraction import extract_text_from_file
from deepseek_client import call_deepseek_with_retry

logger = logging.getLogger(__name__)

NAME = "开票申请单"
APPROVAL_CODE = "624B0174-A255-4EA0-A790-0DE8F2B1F46B"
LINK_ONLY = False
HAS_FILE_EXTRACTION = True  # 从结算单和合同自动提取字段

FIELD_HINTS = (
    "invoice_type(发票类型,用户必须明确说明), invoice_items(开票项目,用户必须明确说明), "
    "amount(金额,从结算单识别), buyer_name(购方名称/开票抬头,从合同识别), tax_id(购方税号,从合同识别), "
    "company(所属公司), settlement_file(开票结算单附件), contract_file(开票合同附件), "
    "settlement_sealed(开票结算单是否已盖章), contract_sealed(开票合同是否已盖章)"
)

# 用户必须明确说明
USER_REQUIRED_FIELDS = ["invoice_type", "invoice_items"]
# 从结算单/合同自动识别
AUTO_FIELDS = ["amount", "buyer_name", "tax_id", "contract_no", "settlement_no"]

CONVERSATION_FIELDS = ["invoice_type", "invoice_items"]
IMAGE_FIELDS = AUTO_FIELDS

FIELD_LABELS = {
    "invoice_type":     "发票类型",
    "invoice_items":    "开票项目",
    "amount":           "开票金额",
    "buyer_name":       "客户/开票名称",
    "tax_id":           "税务登记证号/社会统一信用代码",
    "company":          "所属公司",
    "contract_no":      "合同编号",
    "settlement_no":    "结算单编号",
    "remarks":          "备注",
    "settlement_sealed": "开票结算单是否已盖章",
    "contract_sealed":  "开票合同是否已盖章",
}

# 表单字段名映射到逻辑字段名
FIELD_NAME_ALIASES = {
    "购方名称": "buyer_name", "开票抬头": "buyer_name", "客户/开票名称": "buyer_name",
    "购方税号": "tax_id", "税务登记证号": "tax_id", "社会统一信用代码": "tax_id",
    "税务登记证号/社会统一信用代码": "tax_id",
    "开票金额": "amount", "发票金额": "amount",
    "开票合同是否已盖章": "contract_sealed",
    "开票结算单是否已盖章": "settlement_sealed",
    "所属公司": "company",
}
# 624B0174 开票申请表单字段 ID
FIELD_ID_FALLBACK = {
    "company":          "widget16457793731980001",   # 所属公司
    "buyer_name":       "widget17375318335110001",   # 客户/开票名称
    "tax_id":           "widget17375318355270001",   # 税务登记证号/社会统一信用代码
    "invoice_type":     "widget16457794296140001",   # 发票类型
    "invoice_items":    "widget17375316363080001",   # 开票项目
    "amount":           "widget17375326852760001",   # 开票金额
    "settlement_sealed": "widget17375319011020001",  # 开票结算单是否已盖章
    "contract_sealed":  "widget17375319126200001",   # 开票合同是否已盖章
}
FIELD_ORDER = ["company", "invoice_type", "invoice_items", "amount", "buyer_name", "tax_id", "settlement_sealed", "contract_sealed", "contract_no", "settlement_no", "remarks"]
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
        f"- amount: 开票金额。【重要】金额提取规则：\n"
        f"  · 电商订单/订单详情截图：取「实收」「实收(含运费)」「实付金额」「应付金额」后的数字，单位一般为元；"
        f"不要取「商品总价」「订单编号」(E开头的长串)、「单价」「运费」等。\n"
        f"  · 结算单/对账单/银行流水：取「合计」「应付」「实付」等后的金额。\n"
        f"  · Excel 表格：找「合计费用」「合计金额」「总金额」「应付金额」列，取合计行的数字。\n"
        f"  · 合同：找「合同金额」「服务费」「总价」「价款」等后的数字。\n"
        f"  【关键】OCR常将人民币符号¥误识别为数字7，导致¥15456.00被识别成715456。若金额以7开头且为6位以上(如715456)，应去掉首位7取15456。实收金额通常为几千到几万，极少超50万。返回纯数字。\n"
        f"- buyer_name: 购方名称/开票抬头（合同中的甲方、乙方或购买方）\n"
        f"- tax_id: 购方税号/纳税人识别号\n"
        f"- contract_no: 合同编号（非订单编号，合同编号通常较短）\n"
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
            if v is None:
                continue
            if v and str(v).strip():
                result[k] = str(v).strip()
        return result
    except Exception as e:
        import traceback
        logger.warning("开票申请单从文件提取失败: %s", e)
        traceback.print_exc()
        return {}
