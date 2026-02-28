# 审批类型配置

每个工单类型一个文件，便于维护。

## 飞书 API 支持条件

新增审批前需确认以下条件，否则需设置 `LINK_ONLY = True`：

**1. 流程类型**：审批定义必须有至少一个审批节点
- 固定流程、自由流程 → 支持 API 创建
- 报备单 / 仅报备不审批（0 审批节点）→ 不支持，返回 1390013

**2. 表单控件**：不能包含以下控件类型
- `address`（地址）、`outGroup`（外出控件组）、`leaveGroup`（请假控件组）
- `tripGroup`（出差控件组）、`workGroup`（加班控件组）、`remedyGroup`（补卡控件组）

支持：`input`、`textarea`、`date`、`radio`、`radioV2`、`number`、`amount`、`contact`、`department` 等基础控件。

## 新增工单类型

1. 在本目录新建 `xxx.py`，参考现有文件结构：

```python
NAME = "工单名称"
APPROVAL_CODE = "飞书审批定义的 approval_code"
LINK_ONLY = False  # True 表示只能用链接跳转，API 不支持

FIELD_HINTS = "字段1(说明), 字段2(说明), ..."
FIELD_LABELS = {"字段1": "中文名", "字段2": "中文名", ...}
FIELD_ID_FALLBACK = {}  # 可选，字段ID映射，空则用缓存自动匹配
FIELD_ORDER = []       # 可选，卡片展示顺序（按工单字段）
DATE_FIELDS = set()    # 可选，日期类型字段名

def get_admin_comment(fields):
    return "行政意见内容"
```

2. 在 `__init__.py` 的 `_TYPES` 列表中加入新模块：`from . import xxx` 并加入 `_TYPES = [..., xxx]`

## 有附件识别读取需求的工单

若工单需要从上传的附件（Word/PDF/图片/扫描件）中自动识别并填写字段，需：

1. 设置 `HAS_FILE_EXTRACTION = True`
2. 实现 `extract_fields_from_file(file_content, file_name, form_opts, get_token) -> dict`
   - 内部调用 `file_extraction.extract_text_from_file()` 获取文本（含 RapidOCR 本地识别图片/扫描件）
   - 根据业务用 AI 从文本中提取字段，返回 `{字段名: 值}`

参考 `seal.py`（用印申请单）的实现。

## 删除工单类型

删除对应 py 文件，并从 `__init__.py` 的 `_TYPES` 中移除即可。
