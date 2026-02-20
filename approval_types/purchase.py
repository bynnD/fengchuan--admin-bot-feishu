# 采购申请

NAME = "采购申请"
APPROVAL_CODE = "6CF86C28-26AA-4E8B-ABF4-82DFAE86028C"
LINK_ONLY = False

FIELD_HINTS = "purchase_reason(采购事由), purchase_type(采购类别), expected_date(期望交付时间YYYY-MM-DD), cost_detail(费用明细)"

FIELD_LABELS = {
    "purchase_reason": "采购事由",
    "purchase_type":   "采购类别",
    "expected_date":   "期望交付时间",
    "cost_detail":     "费用明细",
}

FIELD_ID_FALLBACK = {
    "purchase_reason": "widget16510608596030001",
    "purchase_type":   "widget16510608666360001",
    "expected_date":   "widget16510608919180001",
    "cost_detail":     "widget16510609006710001",
}

DATE_FIELDS = {"expected_date"}


def get_admin_comment(fields):
    try:
        cost = str(fields.get("cost_detail", "0"))
        amount = float("".join(c for c in cost if c.isdigit() or c == ".") or "0")
        if amount <= 1000:
            return "行政审核：金额1000元以内，同意。"
        elif amount <= 5000:
            return "行政审核：金额在5000元以内，同意，请附报价单。"
        else:
            return "行政审核：金额超过5000元，需总经理审批确认。"
    except Exception:
        return "行政审核：采购申请已收到，请确认费用明细。"
