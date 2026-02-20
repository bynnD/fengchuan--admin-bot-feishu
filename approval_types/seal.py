# 用印申请

NAME = "用印申请"
APPROVAL_CODE = "FB855CD4-CA15-4A1B-8B7A-51A56171CE60"
LINK_ONLY = False

FIELD_HINTS = "company(所属公司如风船/微驰等), seal_type(印章类型), usage_date(YYYY-MM-DD), document_name(文件名称), reason"

FIELD_LABELS = {
    "company":       "所属公司",
    "seal_type":     "印章类型",
    "usage_date":    "用印日期",
    "document_name": "文件名称",
    "reason":        "原因",
}

FIELD_ID_FALLBACK = {
    "seal_type":     "widget17375347703620001",
    "usage_date":    "widget17375347703620002",
    "document_name": "widget3",
    "reason":        "widget0",
}

FIELD_ORDER = ["company", "seal_type", "usage_date", "document_name", "reason"]
DATE_FIELDS = {"usage_date"}


def get_admin_comment(fields):
    return "行政审核：用印申请已核实，同意。"
