"""
自动审批规则加载模块
- 加载 approval_rules.yaml
- 提供 check_auto_approve(approval_type, fields) -> (can_auto, comment, risk_points)
- 提供开关指令匹配
"""

import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 规则文件路径：项目根目录
_RULES_FILE = os.environ.get("APPROVAL_RULES_FILE") or str(
    Path(__file__).resolve().parent / "approval_rules.yaml"
)

# 内存缓存
_rules_cache = None
_rules_mtime = 0


def _load_rules():
    """加载 YAML 规则，带缓存"""
    global _rules_cache, _rules_mtime
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML 未安装，无法加载 approval_rules.yaml")
        return {}
    path = Path(_RULES_FILE)
    if not path.exists():
        logger.warning("规则文件不存在: %s", _RULES_FILE)
        return {}
    mtime = path.stat().st_mtime
    if _rules_cache is not None and mtime == _rules_mtime:
        return _rules_cache
    try:
        with open(path, "r", encoding="utf-8") as f:
            _rules_cache = yaml.safe_load(f) or {}
        _rules_mtime = mtime
        return _rules_cache
    except Exception as e:
        logger.exception("加载规则文件失败: %s", e)
        return _rules_cache or {}


def get_auto_approve_user_ids():
    """获取启用自动审批的 user_id 列表"""
    rules = _load_rules()
    return rules.get("auto_approve_user_ids") or []


def get_exclude_types():
    """获取不参与自动审批的工单类型"""
    rules = _load_rules()
    return set(rules.get("exclude_types") or [])


def get_switch_commands():
    """获取开关固定指令"""
    rules = _load_rules()
    cmds = rules.get("switch_commands") or {}
    return {
        "enable": tuple(cmds.get("enable") or ["开启自动审批", "打开自动审批"]),
        "disable": tuple(cmds.get("disable") or ["关闭自动审批"]),
        "enable_all": tuple(cmds.get("enable_all") or ["全部开启"]),
        "disable_all": tuple(cmds.get("disable_all") or ["全部关闭"]),
        "query": tuple(cmds.get("query") or ["自动审批状态", "自动审批开没开"]),
        "poll": tuple(cmds.get("poll") or ["轮询"]),
        "enable_type_keywords": list(cmds.get("enable_type_keywords") or ["采购", "开票", "用印"]),
        "disable_type_keywords": list(cmds.get("disable_type_keywords") or ["采购", "开票", "用印"]),
    }


# 类型简称 -> 完整工单类型名
_TYPE_ALIAS = {
    "采购": "采购申请",
    "开票": "开票申请单",
    "用印": "用印申请单",
}


def get_auto_approval_types():
    """获取支持自动审批的工单类型（rules 中 enabled 的，排除 exclude_types）"""
    rules = _load_rules()
    exclude = get_exclude_types()
    type_rules = rules.get("rules") or {}
    return [t for t, r in type_rules.items() if r.get("enabled", True) and t not in exclude]


def get_approval_code_override(approval_type):
    """获取审批类型的 approval_code 覆盖（若公司使用不同的审批定义）"""
    rules = _load_rules()
    overrides = rules.get("approval_code_override") or {}
    return overrides.get(approval_type)


def get_seal_type_rules():
    """获取用印类型与文件类型匹配规则"""
    rules = _load_rules()
    return rules.get("seal_type_rules") or {}


def check_switch_command(text):
    """
    检查文本是否为开关指令。
    返回 (action, approval_type) 或 None。
    action: "enable" | "disable" | "enable_all" | "disable_all" | "enable_type" | "disable_type" | "query"
    approval_type: 仅 enable_type/disable_type 时有值，如 "采购申请"
    """
    t = (text or "").strip()
    if not t:
        return None
    cmds = get_switch_commands()
    if t in cmds["enable"]:
        return ("enable", None)
    if t in cmds["disable"]:
        return ("disable", None)
    if t in cmds["enable_all"]:
        return ("enable_all", None)
    if t in cmds["disable_all"]:
        return ("disable_all", None)
    if t in cmds["query"]:
        return ("query", None)
    if t in cmds["poll"]:
        return ("poll", None)
    # 按类型：开启采购/开启采购申请、关闭用印 等
    for kw in cmds["enable_type_keywords"]:
        full = _TYPE_ALIAS.get(kw, kw)
        if t in ("开启" + kw, "打开" + kw, "开启" + full, "打开" + full,
                 "开启" + kw + "自动审批", "打开" + kw + "自动审批",
                 "开启" + full + "自动审批", "打开" + full + "自动审批",
                 "开启" + kw + "自动审核", "打开" + kw + "自动审核"):
            return ("enable_type", full)
    for kw in cmds["disable_type_keywords"]:
        full = _TYPE_ALIAS.get(kw, kw)
        if t in ("关闭" + kw, "关闭" + full, "关闭" + kw + "自动审批", "关闭" + full + "自动审批",
                 "关闭" + kw + "自动审核", "关闭" + full + "自动审核"):
            return ("disable_type", full)
    return None


def check_auto_approve(approval_type, fields):
    """
    检查工单是否符合自动审批规则（采购、开票、用印的简单规则，不含用印 AI 分析）。
    用印的 AI 分析由 approval_auto 模块单独调用。
    返回 (can_auto: bool, comment: str, risk_points: list)
    """
    rules = _load_rules()
    exclude = get_exclude_types()
    if approval_type in exclude:
        return False, "", ["该类型不参与自动审批"]

    type_rules = (rules.get("rules") or {}).get(approval_type)
    if not type_rules or not type_rules.get("enabled", True):
        return False, "", ["该类型未启用自动审批"]

    # 采购、开票：默认通过
    if approval_type in ("采购申请", "开票申请单"):
        return True, type_rules.get("pass_comment", "已核实，已自动审批通过。"), []

    # 用印：需要 AI 分析，此处仅返回需 AI 检查的标记，实际判断在 approval_auto 中
    if approval_type == "用印申请单" and type_rules.get("ai_check"):
        # 由 approval_auto 模块调用 seal AI 分析后决定
        return None, type_rules.get("pass_comment", ""), []  # None 表示需 AI 检查

    return True, type_rules.get("pass_comment", "已核实，已自动审批通过。"), []
