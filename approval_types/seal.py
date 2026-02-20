# 用印申请

NAME = "用印申请"
APPROVAL_CODE = "FB855CD4-CA15-4A1B-8B7A-51A56171CE60"
LINK_ONLY = False

FIELD_HINTS = (
    "company(用印公司), usage_method(盖章或外带,默认盖章), "
    "reason(文件用途/用印事由), seal_type(印章类型:公章/合同章/法人章/财务章), "
    "document_name(文件名称), document_count(文件数量,默认1), "
    "document_type(文件类型如合同/协议/报告等), lawyer_reviewed(律师是否已审核:是/否,默认否)"
)

FIELD_LABELS = {
    "company":         "用印公司",
    "usage_method":    "盖章或外带",
    "reason":          "文件用途/用印事由",
    "seal_type":       "印章类型",
    "document_name":   "文件名称",
    "document_count":  "文件数量",
    "document_type":   "文件类型",
    "lawyer_reviewed": "律师是否已审核",
}

FIELD_ID_FALLBACK = {
    "company":         "widget17375357884790001",
    "usage_method":    "widget17375347703620001",
    "reason":          "widget0",
    "seal_type":       "widget15754438920110001",
    "document_name":   "widget3",
    "document_count":  "widget4",
    "document_type":   "widget17375354078970001",
    "lawyer_reviewed": "widget17375349618880001",
}

FIELD_ORDER = ["company", "usage_method", "reason", "seal_type", "document_name", "document_count", "document_type", "lawyer_reviewed"]
DATE_FIELDS = set()

SUPPORTS_IMAGE = True


def get_admin_comment(fields):
    return "行政审核：用印申请已核实，同意。"
