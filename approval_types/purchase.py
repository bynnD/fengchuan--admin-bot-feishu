# 采购申请

NAME = "采购申请"
APPROVAL_CODE = "0EFA9385-0C3F-446C-AC10-7CC7F8417DFB"
LINK_ONLY = False

FIELD_HINTS = (
    "purchase_reason(采购事由), purchase_type(采购类别,可根据物品推断), expected_date(期望交付时间YYYY-MM-DD), "
    "cost_detail(费用明细列表,必填,每项包含:名称/规格/数量/金额。是否有库存由审批人填写,发起人不填。"
    "格式如[{\"名称\":\"笔记本\",\"规格\":\"ThinkPad X1\",\"数量\":\"1\",\"金额\":\"8000\"}])"
)

FIELD_LABELS = {
    "purchase_reason": "采购事由",
    "purchase_type":   "采购类别",
    "expected_date":   "期望交付时间",
    "cost_detail":     "费用明细",
}

# 表单字段名可能为「物资明细」或「费用明细」，均映射到 cost_detail
FIELD_NAME_ALIASES = {"物资明细": "cost_detail"}

FIELD_ID_FALLBACK = {
    "purchase_reason": "widget16510608596030001",
    "purchase_type":   "widget16510608666360001",
    "expected_date":   "widget16510608918180001",
    "cost_detail":     "widget16510609006710001",
}

FIELD_ORDER = ["purchase_reason", "purchase_type", "expected_date", "cost_detail"]
DATE_FIELDS = {"expected_date"}


def get_admin_comment(fields):
    return "请核实以上填报信息无误后提交"
