# 外出报备（报备单，无需审核）
# 注：该审批为 free process，API 不支持创建，需通过链接手动填写

NAME = "外出报备"
APPROVAL_CODE = "FDBE8929-CDD4-42E4-8174-9B7724D0A69E"
# 创建链接需 id 参数，从审批管理后台 createApproval 页面 URL 获取
CREATE_LINK_ID = "7609016118423391458"
LINK_ONLY = True  # API 返回 1390013 unsupported approval for free process

FIELD_HINTS = "destination(外出地点), start_date(YYYY-MM-DD), end_date(YYYY-MM-DD), reason(事由)"

FIELD_LABELS = {
    "destination": "外出地点",
    "start_date":  "开始日期",
    "end_date":    "结束日期",
    "reason":      "原因",
}

FIELD_ID_FALLBACK = {}  # 使用缓存自动匹配

DATE_FIELDS = {"start_date", "end_date"}


def get_admin_comment(fields):
    return "外出报备已登记。"
