"""
预检结果缓存：工单创建后以 instance_code 为 key 存储预检结果，
供审批人轮询环节使用。有缓存且合规则直接 approve，跳过下载和 AI 调用。
"""

import logging
import threading
import time

logger = logging.getLogger(__name__)

# instance_code -> {compliant, comment, risks, created_at}
_cache = {}
_lock = threading.RLock()
# 缓存 TTL：24 小时
CACHE_TTL_SEC = 24 * 3600


def set_pre_check_result(instance_code, compliant, comment="", risks=None):
    """写入预检结果"""
    if not instance_code:
        return
    with _lock:
        _cache[instance_code] = {
            "compliant": bool(compliant),
            "comment": comment or "",
            "risks": list(risks or []),
            "created_at": time.time(),
        }
        logger.debug("预检缓存写入: instance=%s compliant=%s", instance_code, compliant)
        # 写入时顺带清理过期条目，避免缓存无限增长
        if len(_cache) > 500:
            _cleanup_expired()


def get_pre_check_result(instance_code):
    """
    获取预检结果。若不存在或已过期返回 None。
    返回 {compliant, comment, risks} 或 None
    """
    if not instance_code:
        return None
    now = time.time()
    with _lock:
        entry = _cache.get(instance_code)
        if not entry:
            return None
        if now - entry.get("created_at", 0) > CACHE_TTL_SEC:
            del _cache[instance_code]
            return None
        return {
            "compliant": entry["compliant"],
            "comment": entry.get("comment", ""),
            "risks": entry.get("risks") or [],
        }


def _cleanup_expired():
    """清理过期条目"""
    now = time.time()
    with _lock:
        to_remove = [k for k, v in _cache.items() if now - v.get("created_at", 0) > CACHE_TTL_SEC]
        for k in to_remove:
            del _cache[k]
