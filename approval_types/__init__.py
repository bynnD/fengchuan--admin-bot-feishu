"""
审批类型配置 - 每个工单类型一个文件，便于维护
新增工单：在此目录新建 py 文件，在 _TYPES 中注册，详见 README.md
"""

from . import seal, purchase, outbound

# 注册所有工单类型模块
_TYPES = [seal, purchase, outbound]

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
    return "行政审核：已确认，同意。"
