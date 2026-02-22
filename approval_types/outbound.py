# 外出报备（报备单，无需审核）

NAME = "外出报备"
APPROVAL_CODE = "FDBE8929-CDD4-42E4-8174-9B7724D0A69E"
LINK_ONLY = False  # 需在管理后台添加自动审批节点，否则 API 返回 1390013

FIELD_HINTS = "destination(外出地点), start_date(YYYY-MM-DD), end_date(YYYY-MM-DD), reason(事由)"

FIELD_LABELS = {
    "destination": "外出地点",
    "start_date":  "开始日期",
    "end_date":    "结束日期",
    "reason":      "原因",
}

FIELD_ID_FALLBACK = {}  # 使用缓存自动匹配
# 卡片展示顺序（按工单字段）
FIELD_ORDER = ["destination", "start_date", "end_date", "reason"]

DATE_FIELDS = {"start_date", "end_date"}


def get_admin_comment(fields):
    return "请核实以上填报信息无误后提交"
