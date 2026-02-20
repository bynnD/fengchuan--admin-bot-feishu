# 审批类型配置

每个工单类型一个文件，便于维护。

## 新增工单类型

1. 在本目录新建 `xxx.py`，参考现有文件结构：

```python
NAME = "工单名称"
APPROVAL_CODE = "飞书审批定义的 approval_code"
LINK_ONLY = False  # True 表示只能用链接跳转，API 不支持

FIELD_HINTS = "字段1(说明), 字段2(说明), ..."
FIELD_LABELS = {"字段1": "中文名", "字段2": "中文名", ...}
FIELD_ID_FALLBACK = {}  # 可选，字段ID映射，空则用缓存自动匹配
DATE_FIELDS = set()    # 可选，日期类型字段名

def get_admin_comment(fields):
    return "行政意见内容"
```

2. 在 `__init__.py` 的 `_TYPES` 列表中加入新模块：`from . import xxx` 并加入 `_TYPES = [..., xxx]`

## 删除工单类型

删除对应 py 文件，并从 `__init__.py` 的 `_TYPES` 中移除即可。
