"""
字段缓存模块
- 首次运行时从飞书API获取审批字段结构，保存到 field_cache.json
- 之后直接读本地缓存，不重复调API
- 提交失败时调用 invalidate_cache() 清除对应缓存，下次重新获取
"""

import json
import logging
import os
import threading
import httpx

logger = logging.getLogger(__name__)

_cache_lock = threading.RLock()  # RLock 支持同一线程重入，避免 _load_disk_cache 与 get_form_fields 嵌套调用死锁

# 优先使用 /app（Docker），否则用项目目录
CACHE_FILE = os.path.join(
    "/app" if os.path.exists("/app") else os.path.dirname(os.path.abspath(__file__)),
    "field_cache.json"
)

_memory_cache = {}
_free_process_cache = {}  # approval_code -> bool，缓存是否为报备单


def _load_disk_cache_unsafe():
    """内部使用，调用前必须已持有 _cache_lock"""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("读取缓存文件失败: %s", e)
    return {}


def _save_disk_cache_unsafe(cache):
    """内部使用，调用前必须已持有 _cache_lock"""
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("保存缓存文件失败: %s", e)


def _load_disk_cache():
    """对外暴露，自带锁。持锁期间做文件 I/O，避免在已持锁的调用链中嵌套使用。"""
    with _cache_lock:
        return _load_disk_cache_unsafe()


def _save_disk_cache(cache):
    """对外暴露，自带锁。"""
    with _cache_lock:
        _save_disk_cache_unsafe(cache)


def _fetch_from_api(approval_code, token):
    """从飞书API获取审批表单字段结构"""
    try:
        res = httpx.get(
            f"https://open.feishu.cn/open-apis/approval/v4/approvals/{approval_code}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10
        )
        data = res.json()
        if data.get("code") != 0:
            logger.warning("获取审批定义失败(%s): %s", approval_code, data.get("msg"))
            return None

        form_str = data.get("data", {}).get("form", "[]")
        if isinstance(form_str, str):
            form = json.loads(form_str)
        else:
            form = form_str

        fields = {}
        for item in form:
            field_id = item.get("id")
            field_name = item.get("name", field_id)
            field_type = item.get("type", "input")
            if not field_id:
                continue
            info = {"name": field_name, "type": field_type}
            if field_type == "fieldList":
                # 飞书 fieldList 子字段可能在 children、ext、value、option 中，参考采购申请/招待领用
                sub_items = item.get("children") or item.get("ext") or item.get("value") or item.get("option") or []
                if isinstance(sub_items, str):
                    try:
                        parsed = json.loads(sub_items) if sub_items else []
                        if isinstance(parsed, dict):
                            sub_items = (
                                parsed.get("children") or parsed.get("ext") or parsed.get("list")
                                or parsed.get("fields") or parsed.get("value") or []
                            )
                            # value1-1 等格式：取第一页结构作为子字段定义
                            if not sub_items and parsed:
                                for k in sorted(parsed.keys()):
                                    if k.startswith("value") and isinstance(parsed[k], list) and parsed[k]:
                                        sub_items = parsed[k]
                                        break
                        else:
                            sub_items = parsed if isinstance(parsed, list) else []
                    except json.JSONDecodeError:
                        sub_items = []
                if not isinstance(sub_items, list):
                    sub_items = []
                info["sub_fields"] = []
                for s in sub_items:
                    if not isinstance(s, dict) or not (s.get("id") or s.get("widget_id") or s.get("field_id")):
                        continue
                    sf = {
                        "id": s.get("id") or s.get("widget_id") or s.get("field_id"),
                        "type": s.get("type", "input"),
                        "name": s.get("name") or s.get("title") or s.get("label", ""),
                    }
                    if sf["type"] in ("radioV2", "radio", "checkboxV2", "checkbox"):
                        opts = s.get("option", [])
                        if isinstance(opts, str):
                            try:
                                opts = json.loads(opts) if opts else []
                            except json.JSONDecodeError:
                                opts = []
                        if opts:
                            sf["options"] = opts
                    info["sub_fields"].append(sf)
                if not info["sub_fields"] and sub_items:
                    if isinstance(sub_items[0], list) and sub_items[0]:
                        first_row = sub_items[0]
                        if isinstance(first_row[0], dict) and first_row[0].get("id"):
                            info["sub_fields"] = [
                                {"id": s.get("id"), "type": s.get("type", "input"), "name": s.get("name", "")}
                                for s in first_row if isinstance(s, dict) and s.get("id")
                            ]
                    if not info["sub_fields"]:
                        raw_preview = json.dumps(item, ensure_ascii=False)[:400]
                        logger.debug("fieldList %s(%s) 无有效子字段，原始 item 预览: %s", field_id, field_name, raw_preview)
                # ext 可能只含部分子字段（如数量），value 第一行才有完整结构，优先使用
                val = item.get("value")
                if isinstance(val, list) and val and isinstance(val[0], list) and val[0]:
                    first_row = val[0]
                    if len(first_row) > len(info.get("sub_fields", [])):
                        info["sub_fields"] = []
                        for s in first_row:
                            if not isinstance(s, dict) or not s.get("id"):
                                continue
                            sf = {"id": s.get("id"), "type": s.get("type", "input"), "name": s.get("name", "")}
                            if sf["type"] in ("radioV2", "radio", "checkboxV2", "checkbox"):
                                opts = s.get("option", [])
                                if isinstance(opts, str):
                                    try:
                                        opts = json.loads(opts) if opts else []
                                    except json.JSONDecodeError:
                                        opts = []
                                if opts:
                                    sf["options"] = opts
                            info["sub_fields"].append(sf)
            if field_type in ("radioV2", "radio", "checkboxV2", "checkbox"):
                opts = item.get("option", [])
                if isinstance(opts, str):
                    try:
                        opts = json.loads(opts) if opts else []
                    except json.JSONDecodeError:
                        opts = []
                info["options"] = opts
            fields[field_id] = info

        logger.info("已获取字段结构(%s): %s", approval_code, list(fields.keys()))
        return fields

    except Exception as e:
        logger.warning("获取审批定义异常(%s): %s", approval_code, e)
        return None


def get_form_fields(approval_type, approval_code, token):
    """
    获取指定审批类型的字段结构。
    优先读内存缓存 -> 磁盘缓存 -> API获取。
    """
    with _cache_lock:
        if approval_type in _memory_cache:
            return _memory_cache[approval_type]

        disk_cache = _load_disk_cache_unsafe()
        if approval_type in disk_cache:
            _memory_cache[approval_type] = disk_cache[approval_type]
            logger.info("从缓存加载字段结构: %s", approval_type)
            return disk_cache[approval_type]

    fields = _fetch_from_api(approval_code, token)
    if fields:
        with _cache_lock:
            disk_cache = _load_disk_cache_unsafe()
            disk_cache[approval_type] = fields
            _save_disk_cache_unsafe(disk_cache)
            _memory_cache[approval_type] = fields
        return fields

    return None


def mark_free_process(approval_code):
    """创建失败 1390013 时标记为报备单，下次预检直接返回 True"""
    with _cache_lock:
        _free_process_cache[approval_code] = True


def get_sub_field_options(approval_type, sub_field_id, approval_code, token):
    """
    获取 fieldList 中指定子字段的选项（用于 radioV2 等）。
    返回 [{value, text}, ...] 或 []。
    """
    cached = get_form_fields(approval_type, approval_code, token)
    if not cached:
        return []
    for field_id, info in cached.items():
        if info.get("type") != "fieldList":
            continue
        for sf in (info.get("sub_fields") or []):
            if sf.get("id") == sub_field_id:
                opts = sf.get("options", [])
                if isinstance(opts, str):
                    try:
                        opts = json.loads(opts) if opts else []
                    except json.JSONDecodeError:
                        return []
                return opts if isinstance(opts, list) else []
    return []


def invalidate_cache(approval_type):
    """提交失败时清除该类型的缓存，下次重新从API获取"""
    with _cache_lock:
        if approval_type in _memory_cache:
            del _memory_cache[approval_type]

        disk_cache = _load_disk_cache_unsafe()
        if approval_type in disk_cache:
            del disk_cache[approval_type]
            _save_disk_cache_unsafe(disk_cache)
            logger.info("已清除字段缓存: %s，下次将重新获取", approval_type)


def _fetch_approval_definition_full(approval_code, token):
    """获取审批定义完整数据（含流程节点）"""
    try:
        res = httpx.get(
            f"https://open.feishu.cn/open-apis/approval/v4/approvals/{approval_code}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10
        )
        data = res.json()
        if data.get("code") != 0:
            return None
        return data.get("data", {})
    except Exception as e:
        logger.warning("获取审批定义异常(%s): %s", approval_code, e)
        return None


def is_free_process(approval_code, token):
    """
    判断审批是否为报备单（仅报备不审批）。
    报备单无审批节点，API 不支持创建，返回 1390013。
    结果缓存于内存，避免重复请求。
    """
    with _cache_lock:
        if approval_code in _free_process_cache:
            return _free_process_cache[approval_code]

    definition = _fetch_approval_definition_full(approval_code, token)
    if not definition:
        with _cache_lock:
            _free_process_cache[approval_code] = False
        return False

    node_list = definition.get("node_list")
    if node_list is None:
        with _cache_lock:
            _free_process_cache[approval_code] = False
        return False
    if isinstance(node_list, str):
        try:
            node_list = json.loads(node_list) if node_list else []
        except json.JSONDecodeError:
            node_list = []

    is_free = len(node_list) == 0
    with _cache_lock:
        _free_process_cache[approval_code] = is_free
    if is_free:
        logger.info("预检: %s 为报备单(无审批节点)，将走链接流程", approval_code)
    return is_free