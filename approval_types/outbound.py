# 外出报备（支持 API 创建，需飞书审批定义至少有一个审批节点；若为报备单则仍走链接）

NAME = "外出报备"
APPROVAL_CODE = "FDBE8929-CDD4-42E4-8174-9B7724D0A69E"
LINK_ONLY = False

FIELD_HINTS = "destination(外出地点), start_date(YYYY-MM-DD), end_date(YYYY-MM-DD), reason(事由)"

FIELD_LABELS = {
    "destination": "外出地点",
    "start_date": "开始日期",
    "end_date": "结束日期",
    "reason": "事由",
}

FIELD_NAME_ALIASES = {
    "开始时间": "start_date",
    "结束时间": "end_date",
}

FIELD_ORDER = ["destination", "start_date", "end_date", "reason"]
DATE_FIELDS = {"start_date", "end_date"}


def get_admin_comment(fields):
    return "请核实以上填报信息无误后提交"
