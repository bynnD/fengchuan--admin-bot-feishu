# 招待/团建物资领用

NAME = "招待/团建物资领用"
APPROVAL_CODE = "D3FA56ED-091E-486F-BF3D-9135C73C4905"
LINK_ONLY = False

FIELD_HINTS = (
    "usage_purpose(物品用途), receive_date(领用日期YYYY-MM-DD), "
    "item_detail(物品明细列表,必填,每项含名称、数量。格式如[{\"名称\":\"笔记本\",\"数量\":\"2\"}])"
)

FIELD_LABELS = {
    "usage_purpose": "物品用途",
    "receive_date":  "领用日期",
    "item_detail":   "物品明细",
}

FIELD_ID_FALLBACK = {
    "usage_purpose": "widget0",
    "receive_date":  "widget1",
    "item_detail":   "widget2",
}

# 物品明细 fieldList 子字段：名称(widget3)、数量(widget4)
FIELDLIST_SUBFIELDS_FALLBACK = {
    "item_detail": [
        {"id": "widget3", "type": "input", "name": "名称"},
        {"id": "widget4", "type": "number", "name": "数量"},
    ]
}

FIELD_ORDER = ["usage_purpose", "receive_date", "item_detail"]
DATE_FIELDS = {"receive_date"}


def get_admin_comment(fields):
    return "请核实以上填报信息无误后提交"
