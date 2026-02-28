# 外出报备（报备单，无需审核）

NAME = "外出报备"
APPROVAL_CODE = "FDBE8929-CDD4-42E4-8174-9B7724D0A69E"
LINK_ONLY = True

FIELD_HINTS = "destination(外出地点), start_date(YYYY-MM-DD), end_date(YYYY-MM-DD), reason(事由)"

FIELD_LABELS = {
    "destination": "外出地点",
}


def get_admin_comment(fields):
    return "请核实以上填报信息无误后提交"
