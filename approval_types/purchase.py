# 采购申请

NAME = "采购申请"
APPROVAL_CODE = "6CF86C28-26AA-4E8B-ABF4-82DFAE86028C"
LINK_ONLY = False

FIELD_HINTS = (
    "purchase_reason(采购事由), purchase_type(采购类别,可根据物品推断), expected_date(期望交付时间YYYY-MM-DD)"
)

FIELD_LABELS = {
    "purchase_reason": "采购事由",
    "purchase_type":   "采购类别",
    "expected_date":   "期望交付时间",
}

FIELD_ID_FALLBACK = {
    "purchase_reason": "widget16510608596030001",
    "purchase_type":   "widget16510608666360001",
    "expected_date":   "widget16510608918180001",
}

FIELD_ORDER = ["purchase_reason", "purchase_type", "expected_date"]
DATE_FIELDS = {"expected_date"}


def get_admin_comment(fields):
    return "请核实以上填报信息无误后提交"
