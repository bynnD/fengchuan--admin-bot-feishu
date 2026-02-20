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
for t in _TYPES:
    FIELD_LABELS.update(t.FIELD_LABELS)
    if getattr(t, "FIELD_ID_FALLBACK", None):
        FIELD_ID_FALLBACK[t.NAME] = t.FIELD_ID_FALLBACK
    if getattr(t, "FIELD_ORDER", None):
        FIELD_ORDER[t.NAME] = t.FIELD_ORDER
    DATE_FIELDS.update(getattr(t, "DATE_FIELDS", set()))

FIELD_LABELS_REVERSE = {v: k for k, v in FIELD_LABELS.items()}
IMAGE_SUPPORT_TYPES = {t.NAME for t in _TYPES if getattr(t, "SUPPORTS_IMAGE", False)}


def get_admin_comment(approval_type, fields):
    for t in _TYPES:
        if t.NAME == approval_type:
            return t.get_admin_comment(fields)
    return "行政审核：已确认，同意。"
