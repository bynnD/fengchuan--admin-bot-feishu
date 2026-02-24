# 招待/团建物资领用

NAME = "招待/团建物资领用"
APPROVAL_CODE = "D3FA56ED-091E-486F-BF3D-9135C73C4905"
LINK_ONLY = False

FIELD_HINTS = (
    "purpose(物品用途,必填), receive_date(领用日期YYYY-MM-DD), "
    "items(物品明细列表,每项包含:名称、数量。格式如[{\"名称\":\"国缘双开6瓶+红酒4瓶\",\"数量\":\"10\"}])"
)

FIELD_LABELS = {
    "purpose":      "物品用途",
    "receive_date": "领用日期",
    "items":        "物品明细",
}

FIELD_ID_FALLBACK = {
    "purpose":      "widget0",
    "receive_date": "widget1",
    "items":        "widget2",
}

# fieldList 子字段：名称(widget3)、数量(widget4)
FIELDLIST_SUBFIELDS_FALLBACK = {
    "items": [
        {"id": "widget3", "type": "input", "name": "名称"},
        {"id": "widget4", "type": "number", "name": "数量"},
    ]
}

FIELD_ORDER = ["purpose", "receive_date", "items"]
DATE_FIELDS = {"receive_date"}


def get_admin_comment(fields):
    return "请核实以上填报信息无误后提交"
