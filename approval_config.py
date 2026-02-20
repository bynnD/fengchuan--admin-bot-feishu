# 审批类型配置文件
# 只需维护这里，无需动 main.py

APPROVAL_CODES = {
    "请假":   "5D58D53D-01BA-44C7-BF5E-712D0F4C7820",
    "外出":   "6EB30779-71CD-4148-B444-AEA25E038E4A",
    "用印申请": "FB855CD4-CA15-4A1B-8B7A-51A56171CE60",
    "采购申请": "6CF86C28-26AA-4E8B-ABF4-82DFAE86028C",
    "入职审批": "36060498-23B6-45AF-B5B0-1EDE2A60241E",
}

# 只用链接跳转、不能API直接提交的审批类型
# 原因：含有证件照片等附件必填字段，只能在飞书客户端手动提交
LINK_ONLY_TYPES = {"入职审批"}

# AI提取信息时的字段提示（告诉AI每种审批需要收集哪些字段）
APPROVAL_FIELD_HINTS = {
    "请假":   "leave_type(年假/病假/事假/婚假/产假), start_date(YYYY-MM-DD), end_date(YYYY-MM-DD), days(数字), reason",
    "外出":   "destination(外出地点), start_date(YYYY-MM-DD), end_date(YYYY-MM-DD), reason",
    "用印申请": "seal_type(印章类型), usage_date(YYYY-MM-DD), document_name(文件名称), reason",
    "采购申请": "purchase_reason(采购事由), purchase_type(采购类别), expected_date(期望交付时间YYYY-MM-DD), cost_detail(费用明细)",
    "入职审批": "name(姓名), department(部门), position(职位), entry_date(YYYY-MM-DD)",
}

# 字段中文显示名（用于机器人回复时展示给用户）
FIELD_LABELS = {
    "leave_type":      "假期类型",
    "start_date":      "开始日期",
    "end_date":        "结束日期",
    "days":            "天数",
    "reason":          "原因",
    "destination":     "外出地点",
    "seal_type":       "印章类型",
    "usage_date":      "用印日期",
    "document_name":   "文件名称",
    "purchase_reason": "采购事由",
    "purchase_type":   "采购类别",
    "expected_date":   "期望交付时间",
    "cost_detail":     "费用明细",
    "name":            "姓名",
    "department":      "部门",
    "position":        "职位",
    "entry_date":      "入职日期",
}

# 通用审批类型的字段逻辑名 -> 飞书真实字段ID 映射
# 请假/外出使用特殊控件，不在这里配置
# 用于：当字段缓存无法自动匹配时的兜底映射
FIELD_ID_FALLBACK = {
    "采购申请": {
        "purchase_reason": "widget16510608596030001",
        "purchase_type":   "widget16510608666360001",
        "expected_date":   "widget16510608919180001",
        "cost_detail":     "widget16510609006710001",
    },
    "用印申请": {
        "seal_type":     "widget17375347703620001",
        "reason":        "widget0",
        "document_name": "widget3",
    },
}