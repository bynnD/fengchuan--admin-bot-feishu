# 采购申请

NAME = "采购申请"
APPROVAL_CODE = "6CF86C28-26AA-4E8B-ABF4-82DFAE86028C"
LINK_ONLY = False

FIELD_HINTS = (
    "purchase_reason(采购事由), purchase_type(采购类别,可根据物品推断), expected_date(期望交付时间YYYY-MM-DD), "
    "cost_detail(费用明细列表,必填,每项包含:名称/规格/数量/金额。是否有库存由审批人填写,发起人不填。"
    "格式如[{\"名称\":\"笔记本\",\"规格\":\"ThinkPad X1\",\"数量\":\"1\",\"金额\":\"8000\"}])"
)

FIELD_LABELS = {
    "purchase_reason": "采购事由",
    "purchase_type":   "采购类别",
    "expected_date":   "期望交付时间",
    "cost_detail":     "费用明细",
}

# 表单字段名可能为「物资明细」或「费用明细」，均映射到 cost_detail
FIELD_NAME_ALIASES = {"物资明细": "cost_detail"}

FIELD_ID_FALLBACK = {
    "purchase_reason": "widget16510608596030001",
    "purchase_type":   "widget16510608666360001",
    "expected_date":   "widget16510608918180001",
    "cost_detail":     "widget16510609006710001",
}

FIELD_ORDER = ["purchase_reason", "purchase_type", "expected_date", "cost_detail"]
DATE_FIELDS = {"expected_date"}


def get_admin_comment(fields):
    try:
        cost_detail = fields.get("cost_detail", "0")
        total = 0
        if isinstance(cost_detail, list):
            for item in cost_detail:
                if isinstance(item, dict):
                    amt = str(item.get("金额") or item.get("amount") or "0")
                    total += float("".join(c for c in amt if c.isdigit() or c == ".") or "0")
        else:
            total = float("".join(c for c in str(cost_detail) if c.isdigit() or c == ".") or "0")
        if total <= 1000:
            return "行政审核：金额1000元以内，同意。"
        elif total <= 5000:
            return "行政审核：金额在5000元以内，同意，请附报价单。"
        else:
            return "行政审核：金额超过5000元，需总经理审批确认。"
    except Exception:
        return "行政审核：采购申请已收到，请确认费用明细。"
