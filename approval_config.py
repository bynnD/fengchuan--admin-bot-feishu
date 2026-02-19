# 审批类型配置文件
# 新增或删除审批类型在这里修改

APPROVAL_CODES = {
    "请假":   "5D58D53D-01BA-44C7-BF5E-712D0F4C7820",
    "外出":   "6EB30779-71CD-4148-B444-AEA25E038E4A",
    "用印申请": "FB855CD4-CA15-4A1B-8B7A-51A56171CE60",
    "采购申请": "6CF86C28-26AA-4E8B-ABF4-82DFAE86028C",
    "入职审批": "36060498-23B6-45AF-B5B0-1EDE2A60241E",
}

# 每种审批需要收集的字段
APPROVAL_FIELDS = {
    "请假":   ["leave_type", "start_date", "end_date", "days", "reason"],
    "外出":   ["destination", "start_date", "end_date", "reason"],
    "用印申请": ["seal_type", "usage_date", "document_name", "reason"],
    "采购申请": ["item_name", "quantity", "budget", "reason"],
    "入职审批": ["name", "department", "position", "entry_date"],
}

# 字段的中文显示名称
FIELD_LABELS = {
    "leave_type":    "假期类型(年假/病假/事假等)",
    "start_date":    "开始日期",
    "end_date":      "结束日期",
    "days":          "天数",
    "reason":        "原因",
    "destination":   "外出地点",
    "seal_type":     "印章类型",
    "usage_date":    "用印日期",
    "document_name": "文件名称",
    "item_name":     "采购物品名称",
    "quantity":      "数量",
    "budget":        "预算金额",
    "name":          "姓名",
    "department":    "部门",
    "position":      "职位",
    "entry_date":    "入职日期",
}

# 每种审批字段格式说明（用于引导AI提取信息）
APPROVAL_FIELD_HINTS = {
    "请假":   "leave_type, start_date(YYYY-MM-DD), end_date(YYYY-MM-DD), days(数字), reason",
    "外出":   "destination, start_date(YYYY-MM-DD), end_date(YYYY-MM-DD), reason",
    "用印申请": "seal_type, usage_date(YYYY-MM-DD), document_name, reason",
    "采购申请": "item_name, quantity, budget(金额数字), reason",
    "入职审批": "name, department, position, entry_date(YYYY-MM-DD)",
}
