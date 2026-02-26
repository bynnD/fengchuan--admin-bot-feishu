"""
审批类型配置 - 每个工单类型一个文件，便于维护
新增工单：在此目录新建 py 文件，在 _TYPES 中注册，详见 README.md
"""

from . import seal, purchase, outbound, invoice, reception_supplies

# 注册所有工单类型模块
_TYPES = [seal, purchase, outbound, invoice, reception_supplies]

# 聚合配置
APPROVAL_CODES = {t.NAME: t.APPROVAL_CODE for t in _TYPES}
APPROVAL_FIELD_HINTS = {t.NAME: t.FIELD_HINTS for t in _TYPES}
LINK_ONLY_TYPES = {t.NAME for t in _TYPES if getattr(t, "LINK_ONLY", False)}

FIELD_LABELS = {}
FIELD_ID_FALLBACK = {}
FIELD_ORDER = {}
DATE_FIELDS = set()
FIELDLIST_SUBFIELDS_FALLBACK = {}
FIELD_NAME_ALIASES = {}
for t in _TYPES:
    FIELD_LABELS.update(t.FIELD_LABELS)
    if getattr(t, "FIELD_ID_FALLBACK", None):
        FIELD_ID_FALLBACK[t.NAME] = t.FIELD_ID_FALLBACK
    if getattr(t, "FIELD_ORDER", None):
        FIELD_ORDER[t.NAME] = t.FIELD_ORDER
    DATE_FIELDS.update(getattr(t, "DATE_FIELDS", set()))
    if getattr(t, "FIELDLIST_SUBFIELDS_FALLBACK", None):
        FIELDLIST_SUBFIELDS_FALLBACK[t.NAME] = t.FIELDLIST_SUBFIELDS_FALLBACK
    if getattr(t, "FIELD_NAME_ALIASES", None):
        FIELD_NAME_ALIASES.update(t.FIELD_NAME_ALIASES)

FIELD_LABELS_REVERSE = {v: k for k, v in FIELD_LABELS.items()}
FIELD_LABELS_REVERSE.update(FIELD_NAME_ALIASES)
IMAGE_SUPPORT_TYPES = {t.NAME for t in _TYPES if getattr(t, "SUPPORTS_IMAGE", False)}

# 各类型使用简要说明 + 例句（用于首次/意图不明时的引导）
# 例句必须包含用户必须在对话中说明的字段
# (简要说明, 例句, 是否用「直接发送」- 简短关键词用 True)
APPROVAL_USAGE_GUIDE = {
    # 用印：律师已审核(是/否)必填，印章类型、用印事由可从文件识别
    "用印申请单": ("上传需盖章的文件（Word/PDF/图片），我会自动识别内容；律师是否已审核需明确说「是」或「否」", "帮我盖这份合同的公章，律师已审核是", False),
    # 开票：发票类型、开票项目必填
    "开票申请单": ("发送后按提示上传结算单和合同；必须说明发票类型和开票项目", "我要开增值税发票，发票内容是技术服务费", False),
    # 采购：物品名称、规格、数量、金额、期望交付时间
    "采购申请": ("说明物品、规格、数量、金额、期望交付时间", "采购一台笔记本 ThinkPad X1 8000元，下周一到货", False),
    # 外出：时间、地点、事由
    "外出报备": ("说明开始日期、结束日期、外出地点、事由", "我2月24日9点要外出2个小时，去税务局办理税务变更", False),
    # 招待/团建物资领用：物品用途、领用日期、物品明细
    "招待/团建物资领用": ("说明物品用途、领用日期、物品明细（名称和数量）", "招待用领用2箱矿泉水、5包零食，领用日期明天", False),
}

# 有附件识别读取需求的工单类型 -> 文件内容提取器（均使用 file_extraction 的 OCR/文本提取）
FILE_EXTRACTORS = {
    t.NAME: t.extract_fields_from_file
    for t in _TYPES
    if getattr(t, "HAS_FILE_EXTRACTION", False) and hasattr(t, "extract_fields_from_file")
}


def get_file_extractor(approval_type):
    """获取工单类型的文件内容提取器，无则返回 None"""
    return FILE_EXTRACTORS.get(approval_type)


def get_admin_comment(approval_type, fields):
    for t in _TYPES:
        if t.NAME == approval_type:
            return t.get_admin_comment(fields)
    return "请核实以上填报信息无误后提交"
