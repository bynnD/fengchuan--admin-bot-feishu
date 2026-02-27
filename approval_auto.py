"""
自动审批模块
- 审批通过 API、审批评论 API
- 表单反解析（form -> fields）
- 用印 AI 分析（三点判断）
- 待审批任务轮询与处理
"""

import json
import logging
import os
import threading
import time
from pathlib import Path

import httpx

from approval_auto_rules import (
    check_invoice_attachments_with_ai,
    check_seal_with_ai,
)
from approval_rules_loader import (
    check_auto_approve,
    get_auto_approve_user_ids,
    get_approval_code_override,
    get_auto_approval_types,
    get_exclude_types,
)
from approval_types import (
    APPROVAL_CODES,
    FIELD_LABELS_REVERSE,
    FIELD_ID_FALLBACK,
    FIELD_LABELS,
)
from field_cache import get_form_fields
from pre_check_cache import get_pre_check_result, set_pre_check_result

logger = logging.getLogger(__name__)


def _collect_tokens_from_file_codes(file_codes, file_names_hint=None):
    """
    从 file_codes 提取 (token, name) 列表。
    file_codes: dict {field_id: [token, ...]} 或 list [token, ...]
    file_names_hint: 可选，[(token, name), ...] 或 [name, ...] 或单个 doc_name（用印首文件）
    返回 [(token, name), ...]
    """
    tokens = []
    if isinstance(file_codes, list):
        for i, v in enumerate(file_codes):
            tok = v.get("file_token") or v.get("code") or v.get("file_code") if isinstance(v, dict) else v
            if not tok:
                continue
            tok = str(tok)
            name = ""
            if isinstance(v, dict):
                name = v.get("file_name") or v.get("name") or ""
            if not name and file_names_hint:
                if isinstance(file_names_hint, list):
                    if file_names_hint and isinstance(file_names_hint[0], (list, tuple)):
                        name = next((n for t, n in file_names_hint if str(t) == tok), "")
                    elif i < len(file_names_hint):
                        name = str(file_names_hint[i]) if file_names_hint[i] else ""
                elif isinstance(file_names_hint, dict):
                    name = file_names_hint.get(tok, "")
                elif isinstance(file_names_hint, str):
                    name = file_names_hint if i == 0 else ""
            tokens.append((tok, name or f"附件{i+1}"))
    elif isinstance(file_codes, dict):
        idx = 0
        for v in file_codes.values():
            lst = v if isinstance(v, list) else [v]
            for x in lst:
                tok = x.get("file_token") or x.get("code") or x.get("file_code") if isinstance(x, dict) else x
                if not tok:
                    continue
                tok = str(tok)
                name = ""
                if isinstance(x, dict):
                    name = x.get("file_name") or x.get("name") or ""
                if not name and file_names_hint and isinstance(file_names_hint, list) and idx < len(file_names_hint):
                    name = str(file_names_hint[idx]) if file_names_hint[idx] else ""
                tokens.append((tok, name or f"附件{idx+1}"))
                idx += 1
    return tokens


def run_pre_check(approval_type, fields, file_codes=None, get_token=None, file_tokens_with_names=None):
    """
    工单创建前规则预检。用于发确认卡前或直接创建前。
    返回 (compliant: bool, comment: str, risks: list)
    """
    if not get_token:
        return True, "", []

    if approval_type in get_exclude_types():
        return False, "该类型不参与自动审批", ["该类型不参与自动审批"]

    if approval_type == "用印申请单":
        seal_type = fields.get("seal_type", "")
        doc_name = fields.get("document_name", "未知")
        doc_type = fields.get("document_type", "")
        seal_detail = fields.get("seal_detail", [])
        if seal_detail and isinstance(seal_detail, list) and len(seal_detail) > 0:
            first_row = seal_detail[0]
            if isinstance(first_row, dict):
                _val = first_row.get("印章类型") or first_row.get("seal_type")
                if _val is not None and _val != "":
                    seal_type = _val.get("text", _val) if isinstance(_val, dict) else str(_val)
                if not doc_name or doc_name == "未知":
                    _dn = first_row.get("文件名称")
                    doc_name = (_dn.get("text", _dn) if isinstance(_dn, dict) else _dn) or "未知"
                if not doc_type:
                    _dt = first_row.get("文件类型")
                    doc_type = (_dt.get("text", _dt) if isinstance(_dt, dict) else _dt) or ""

        if file_tokens_with_names:
            tokens_with_names = file_tokens_with_names
        else:
            tokens_with_names = _collect_tokens_from_file_codes(file_codes or {}, [doc_name])

        if not tokens_with_names:
            return False, "用印申请单缺少附件，无法进行 AI 分析。", ["缺少附件"]

        if not seal_type:
            return False, "用印申请单缺少印章类型。", ["缺少印章类型"]

        default_fname = f"{doc_name}.{doc_type}" if doc_type else (doc_name or "未知")
        can_auto = True
        comment = ""
        all_risks = []
        for i, (tok, fname_from_form) in enumerate(tokens_with_names):
            file_content, dl_err = _download_approval_file(tok, get_token)
            if not file_content:
                return False, f"附件{i + 1}下载失败（{dl_err}），无法进行 AI 分析。", [f"附件{i+1}下载失败"]
            file_name = fname_from_form or (default_fname if i == 0 else f"附件{i+1}")
            try:
                file_can_auto, file_comment, risks = check_seal_with_ai(
                    file_content, file_name, seal_type, get_token
                )
                if not file_can_auto:
                    can_auto = False
                    comment = file_comment
                    all_risks.extend(risks or [])
                    break
            except Exception as e:
                logger.warning("用印预检 AI 分析异常: %s", e)
                return False, f"AI 分析异常（附件{i + 1}）：{e}", ["分析失败"]
        return can_auto, comment, all_risks

    if approval_type == "开票申请单":
        if file_tokens_with_names:
            tokens_with_names = file_tokens_with_names
        else:
            tokens_with_names = _collect_tokens_from_file_codes(file_codes or {})

        if not tokens_with_names:
            return False, "开票申请单缺少附件，无法进行 AI 分析。", ["缺少附件"]

        file_contents_with_names = []
        for i, (tok, fname_from_form) in enumerate(tokens_with_names[:10]):
            content, dl_err = _download_approval_file(tok, get_token)
            fname = fname_from_form or f"附件{i+1}"
            file_contents_with_names.append((content or b"", fname))
        try:
            only_contract, comment = check_invoice_attachments_with_ai(file_contents_with_names, get_token)
            if only_contract:
                return False, comment or "附件中仅有合同。", ["仅合同"]
            return True, "", []
        except Exception as e:
            logger.warning("开票预检 AI 分析异常: %s", e)
            return False, f"AI 分析异常：{e}", ["分析失败"]

    # 采购、招待等：规则检查，不调 AI
    can_auto, comment, risk_points = check_auto_approve(approval_type, fields)
    if can_auto is None:
        return False, "需人工审核。", ["需人工审核"]
    return bool(can_auto), comment or "", list(risk_points or [])

# 状态文件路径
_STATE_FILE = os.environ.get("AUTO_APPROVAL_STATE_FILE") or str(
    Path(__file__).resolve().parent / "auto_approval_state.json"
)
_state_lock = threading.RLock()
_state_data = None  # {"enabled": bool, "types": {type: bool}, ...}


def _load_state():
    """加载自动审批开关状态"""
    global _state_data
    path = Path(_STATE_FILE)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                _state_data = {
                    "enabled": data.get("enabled", False),
                    "types": data.get("types") or {},
                    "updated_by": data.get("updated_by", ""),
                    "updated_at": data.get("updated_at", ""),
                }
                return
        except Exception as e:
            logger.warning("加载自动审批状态失败: %s", e)
    # 默认从规则文件
    try:
        import yaml
        rpath = Path(__file__).resolve().parent / "approval_rules.yaml"
        if rpath.exists():
            with open(rpath, "r", encoding="utf-8") as f:
                rules = yaml.safe_load(f) or {}
                _state_data = {
                    "enabled": rules.get("default_enabled", False),
                    "types": {},
                    "updated_by": "",
                    "updated_at": "",
                }
                return
    except Exception:
        pass
    _state_data = {"enabled": False, "types": {}, "updated_by": "", "updated_at": ""}


def _save_state(enabled=None, types=None, user_id=""):
    """保存自动审批开关状态。enabled/types 为 None 时保持原值"""
    global _state_data
    with _state_lock:
        if _state_data is None:
            _load_state()
        data = dict(_state_data)
        if enabled is not None:
            data["enabled"] = bool(enabled)
        if types is not None:
            data["types"] = dict(types)
        data["updated_by"] = user_id
        data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        _state_data = data
        try:
            with open(_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("保存自动审批状态失败: %s", e)


def is_auto_approval_enabled():
    """获取当前自动审批总开关状态"""
    global _state_data
    if _state_data is None:
        _load_state()
    return bool(_state_data.get("enabled", False))


def is_auto_approval_enabled_for_type(approval_type):
    """判断指定工单类型是否启用自动审批"""
    if not is_auto_approval_enabled():
        return False
    if approval_type in get_exclude_types():
        return False
    types = _state_data.get("types") or {}
    if approval_type in types:
        return bool(types[approval_type])
    # 未在 types 中配置时，按 rules.xxx.enabled
    try:
        import yaml
        rpath = Path(__file__).resolve().parent / "approval_rules.yaml"
        if rpath.exists():
            with open(rpath, "r", encoding="utf-8") as f:
                rules = yaml.safe_load(f) or {}
                tr = (rules.get("rules") or {}).get(approval_type)
                return bool(tr and tr.get("enabled", True))
    except Exception:
        pass
    return False


def set_auto_approval_enabled(enabled, user_id=""):
    """设置自动审批总开关"""
    _save_state(enabled=enabled, user_id=user_id)


def set_auto_approval_type_enabled(approval_type, enabled, user_id=""):
    """设置指定工单类型的自动审批开关"""
    global _state_data
    if _state_data is None:
        _load_state()
    types = dict(_state_data.get("types") or {})
    types[approval_type] = bool(enabled)
    _save_state(types=types, user_id=user_id)


def set_all_types_enabled(enabled, user_id=""):
    """设置全部工单类型的自动审批开关，并开启总开关"""
    global _state_data
    if _state_data is None:
        _load_state()
    types = {t: bool(enabled) for t in get_auto_approval_types()}
    _save_state(enabled=True, types=types, user_id=user_id)


def get_auto_approval_status():
    """获取自动审批状态：总开关 + 各类型开关。用于 query 指令"""
    result = {"enabled": is_auto_approval_enabled(), "types": {}}
    for t in get_auto_approval_types():
        result["types"][t] = is_auto_approval_enabled_for_type(t)
    return result


# approval_code -> 工单类型（含 override 后的映射）
def _get_approval_codes_to_query():
    """获取用于查询的 (approval_code, approval_type) 列表，含 override。跳过 exclude_types（不参与自动审批且可能在工作区不存在）"""
    exclude = get_exclude_types()
    result = []
    for approval_type, code in APPROVAL_CODES.items():
        if approval_type in exclude:
            continue
        override = get_approval_code_override(approval_type)
        effective = override if override else code
        result.append((effective, approval_type))
    return result


def _build_approval_code_to_type():
    """构建 approval_code -> approval_type 映射，含 override"""
    mapping = {}
    for approval_type, code in APPROVAL_CODES.items():
        mapping[code] = approval_type
        override = get_approval_code_override(approval_type)
        if override:
            mapping[override] = approval_type
    return mapping


APPROVAL_CODE_TO_TYPE = {v: k for k, v in APPROVAL_CODES.items()}


def _get_logical_key(field_id, field_name, approval_type):
    """根据 field_id/field_name 解析逻辑字段名"""
    fallback = FIELD_ID_FALLBACK.get(approval_type, {})
    name_to_key = {v: k for k, v in FIELD_LABELS.items()}
    if field_name and field_name in name_to_key:
        return name_to_key[field_name]
    for k, fid in fallback.items():
        if fid == field_id:
            return k
    return field_name or field_id


def _build_cache_from_form_list(form_list):
    """
    当审批定义 API 失败时，从实例 form 构建最小字段结构。
    返回 {field_id: {name, type, sub_fields?}}
    """
    if not form_list:
        return {}
    cached = {}
    for item in form_list:
        fid = item.get("id")
        if not fid:
            continue
        cached[fid] = {
            "name": item.get("name", ""),
            "type": item.get("type", "input"),
        }
        if item.get("type") == "fieldList":
            val = item.get("value", [])
            sub_fields = []
            if isinstance(val, list) and val:
                first_row = val[0] if isinstance(val[0], list) else val
                for cell in (first_row if isinstance(first_row, list) else []):
                    if isinstance(cell, dict) and cell.get("id"):
                        sub_fields.append({
                            "id": cell.get("id"),
                            "name": cell.get("name", ""),
                            "type": cell.get("type", "input"),
                        })
            if sub_fields:
                cached[fid]["sub_fields"] = sub_fields
    return cached


def parse_form_to_fields(approval_type, form_list, cached, get_token):
    """
    将飞书 form 反解析为逻辑字段 dict。
    form_list: 飞书实例返回的 form 数组
    cached: field_cache 的字段结构 {field_id: {name, type, ...}}
    """
    if not form_list:
        return {}
    if not cached:
        cached = _build_cache_from_form_list(form_list)
        if cached:
            logger.info("自动审批: 审批定义 API 不可用，已从实例 form 构建字段结构")
    if not cached:
        return {}
    fields = {}
    _FIELDLIST_ALIAS = {
        "名称": "名称", "name": "名称", "规格": "规格", "数量": "数量", "金额": "金额",
        "amount": "金额", "item_name": "名称", "spec": "规格", "quantity": "数量",
    }

    for item in form_list:
        field_id = item.get("id")
        if not field_id:
            continue
        info = cached.get(field_id, {})
        field_name = info.get("name", "")
        field_type = item.get("type", info.get("type", "input"))
        raw_value = item.get("value", "")

        logical_key = _get_logical_key(field_id, field_name, approval_type)

        if field_type == "fieldList" and isinstance(raw_value, list):
            rows = []
            sub_fields = info.get("sub_fields", [])
            for row in raw_value:
                if isinstance(row, list):
                    row_dict = {}
                    for cell in row:
                        if isinstance(cell, dict):
                            cid = cell.get("id", "")
                            cval = cell.get("value", "")
                            for sf in sub_fields:
                                if (sf.get("id") or sf.get("widget_id")) == cid:
                                    sname = sf.get("name", "")
                                    skey = _FIELDLIST_ALIAS.get(sname, sname) or "名称"
                                    row_dict[skey] = cval
                                    break
                    if row_dict:
                        rows.append(row_dict)
            if rows:
                fields["cost_detail" if "费用" in field_name or "物资" in field_name else logical_key] = rows
        elif field_type == "dateInterval" and isinstance(raw_value, dict):
            start = raw_value.get("start", "")
            end = raw_value.get("end", "")
            if start:
                fields["start_date"] = str(start).split("T")[0] if "T" in str(start) else str(start)
            if end:
                fields["end_date"] = str(end).split("T")[0] if "T" in str(end) else str(end)
        elif raw_value is not None and raw_value != "":
            if isinstance(raw_value, (list, dict)):
                continue
            fields[logical_key] = str(raw_value)

    return fields


def approve_task(approval_code, instance_code, user_id, task_id, comment, get_token):
    """调用飞书同意审批任务 API"""
    token = get_token()
    res = httpx.post(
        "https://open.feishu.cn/open-apis/approval/v4/tasks/approve",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        params={"user_id_type": "user_id"},
        json={
            "approval_code": approval_code,
            "instance_code": instance_code,
            "user_id": user_id,
            "task_id": task_id,
            "comment": comment if comment else "",
        },
        timeout=15,
    )
    data = res.json()
    if data.get("code") == 0:
        logger.info("自动审批通过: instance=%s task=%s", instance_code, task_id)
        return True, None
    logger.warning("审批通过失败: code=%s msg=%s", data.get("code"), data.get("msg"))
    return False, data.get("msg", "未知错误")


def add_approval_comment(instance_code, content, get_token):
    """在审批实例下添加评论。飞书 API 要求 content 为 JSON 字符串格式 {"text":"评论内容","files":[]}"""
    token = get_token()
    text = (content or "自动审批").strip()
    # 移除可能导致 field validation failed 的控制字符（保留换行）
    text = "".join(c for c in text if c == "\n" or not (ord(c) < 32 or ord(c) == 127))
    # 按官方示例格式：text + files 数组（无附件时传空数组）
    content_payload = {"text": text, "files": []}
    content_json = json.dumps(content_payload, ensure_ascii=False)
    # 若 content 过长，截断（飞书评论有长度限制）
    if len(content_json) > 60000:
        content_payload = {"text": text[:30000] + "...(内容过长已截断)", "files": []}
        content_json = json.dumps(content_payload, ensure_ascii=False)
    res = httpx.post(
        f"https://open.feishu.cn/open-apis/approval/v4/instances/{instance_code}/comments",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        params={"user_id_type": "user_id"},
        json={"content": content_json},
        timeout=10,
    )
    data = res.json()
    if data.get("code") == 0:
        logger.info("已添加审批评论: instance=%s", instance_code)
        return True, None
    logger.warning("添加评论失败: code=%s msg=%s content_len=%d content_preview=%r", data.get("code"), data.get("msg"), len(content_json), content_json[:200] if content_json else "")
    return False, data.get("msg", "未知错误")


def _download_drive_file(file_token, get_token):
    """
    从飞书云空间下载文件。
    审批附件的 file_code 与 drive file_token 通常一致，可直接用于 drive/v1/files/{file_token}/download。
    支持：1) 直接返回二进制流 2) 返回 JSON 含 download_link 3) 302 重定向
    返回 (content, None) 或 (None, err)
    """
    if not file_token:
        return None, "无 file_token"
    token = get_token()
    url = f"https://open.feishu.cn/open-apis/drive/v1/files/{file_token}/download"
    try:
        # 允许跟随重定向（飞书可能返回 302 到实际文件 URL）
        res = httpx.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
            follow_redirects=True,
        )
        if res.status_code == 200:
            ct = res.headers.get("content-type", "").lower()
            if "application/json" in ct:
                data = res.json()
                if data.get("code") == 0:
                    d = data.get("data") or {}
                    dl_url = d.get("download_link") or d.get("download_url")
                    if dl_url and str(dl_url).startswith("http"):
                        r2 = httpx.get(dl_url, timeout=60, follow_redirects=True)
                        if r2.status_code == 200 and r2.content:
                            return r2.content, None
                return None, data.get("msg", "下载接口返回失败")
            if res.content and len(res.content) > 0:
                return res.content, None
        return None, f"HTTP {res.status_code}"
    except Exception as e:
        logger.warning("下载文件异常 file_token=%s: %s", file_token[:20] if file_token else "", e)
        return None, str(e)


def _download_approval_file(file_token_or_code, get_token):
    """
    下载审批附件。支持：1) 直接 URL 2) drive 接口 3) media 临时链接。
    返回 (content, None) 或 (None, err)
    """
    if file_token_or_code and str(file_token_or_code).startswith("http"):
        try:
            res = httpx.get(str(file_token_or_code), timeout=60, follow_redirects=True)
            if res.status_code == 200 and res.content:
                return res.content, None
        except Exception as e:
            logger.debug("直接下载 URL 失败: %s", e)
        return None, "URL 下载失败"
    content, err = _download_drive_file(file_token_or_code, get_token)
    if content:
        return content, None
    # 备选：batch_get_tmp_download_url（适用于部分媒体类型）
    try:
        token = get_token()
        res = httpx.post(
            "https://open.feishu.cn/open-apis/drive/v1/medias/batch_get_tmp_download_url",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"file_tokens": [file_token_or_code]},
            timeout=15,
        )
        data = res.json()
        if data.get("code") == 0:
            tmp_list = data.get("data", {}).get("tmp_download_urls", [])
            if tmp_list and tmp_list[0].get("url"):
                r2 = httpx.get(tmp_list[0]["url"], timeout=60, follow_redirects=True)
                if r2.status_code == 200 and r2.content:
                    return r2.content, None
    except Exception as e:
        logger.debug("media 临时链接下载失败: %s", e)
    return None, err or "下载失败"


def get_instance_detail(instance_code, get_token):
    """获取审批实例详情"""
    token = get_token()
    res = httpx.get(
        f"https://open.feishu.cn/open-apis/approval/v4/instances/{instance_code}",
        headers={"Authorization": f"Bearer {token}"},
        params={"user_id_type": "user_id"},
        timeout=10,
    )
    data = res.json()
    if data.get("code") != 0:
        return None, data.get("msg", "获取实例失败")
    return data.get("data", {}), None


def query_pending_tasks(user_id, get_token):
    """
    查询指定用户的待审批任务。
    飞书 API: 通过实例列表筛选，或使用任务查询接口。
    参考: GET /approval/v4/instances/query 或 tasks/query
    """
    token = get_token()
    # 飞书 v4 查询待我审批：需要 approval_code，这里遍历已知类型
    tasks = []
    for approval_code in APPROVAL_CODES.values():
        try:
            res = httpx.get(
                "https://open.feishu.cn/open-apis/approval/v4/instances/query",
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "user_id_type": "user_id",
                    "approval_code": approval_code,
                    "instance_code": "",  # 可选
                    "start_time": str(int((time.time() - 7 * 24 * 3600) * 1000)),  # 近7天
                    "end_time": str(int(time.time() * 1000)),
                },
                timeout=10,
            )
            data = res.json()
            if data.get("code") != 0:
                continue
            for ic in data.get("data", {}).get("instance_code_list", []):
                tasks.append({"approval_code": approval_code, "instance_code": ic})
        except Exception as e:
            logger.debug("查询实例列表失败 %s: %s", approval_code, e)
    return tasks


def process_auto_approve_for_task(approval_code, instance_code, user_id, task_id, get_token):
    """
    处理单条待审批任务：轮询只根据预检结果做动作。
    有缓存且合规则 -> 直接 approve（不下载、不调 AI）；
    有缓存但不合规则或无缓存（自建工单等）-> 等待人工处理。
    """
    code_to_type = _build_approval_code_to_type()
    approval_type = code_to_type.get(approval_code)
    if not approval_type or approval_type in get_exclude_types():
        logger.debug("自动审批: 跳过 instance=%s (无类型或已排除)", instance_code)
        return
    if not is_auto_approval_enabled_for_type(approval_type):
        logger.info("自动审批: 跳过 instance=%s %s 未启用自动审批", instance_code, approval_type)
        return
    if user_id not in get_auto_approve_user_ids():
        logger.info("自动审批: 跳过 instance=%s user_id=%s 不在 auto_approve_user_ids", instance_code, user_id)
        return

    # 轮询只根据预检结果做动作：有缓存且合规则直接 approve；有缓存但不合规则或无缓存则等待人工（不下载、不调 AI）
    cached_result = get_pre_check_result(instance_code)
    if cached_result:
        if cached_result.get("compliant"):
            logger.info("自动审批: instance=%s 使用预检缓存直接通过", instance_code)
            ok, err = approve_task(approval_code, instance_code, user_id, task_id, "", get_token)
            if not ok:
                logger.warning("自动审批: 审批 API 失败 instance=%s: %s", instance_code, err)
        else:
            logger.info("自动审批: instance=%s 预检不合规，等待人工处理", instance_code)
    else:
        logger.info("自动审批: instance=%s 无预检缓存（自建工单等），等待人工处理", instance_code)


def poll_and_process(get_token):
    """
    轮询待审批任务并处理。
    从实例详情 task_list 中筛选出 user_id 的 PENDING 任务。
    """
    uids = get_auto_approve_user_ids()
    if not uids:
        logger.info("自动审批轮询: auto_approve_user_ids 为空，跳过")
        return
    logger.info("自动审批轮询: 开始 uid=%s", uids)
    processed_count = 0
    for uid in uids:
        found_any = False
        for approval_code, instance_codes in _iter_instances_for_user(uid, get_token):
            found_any = True
            for ic in instance_codes:
                detail, err = get_instance_detail(ic, get_token)
                if not detail:
                    logger.warning("自动审批: 获取实例详情失败 instance=%s err=%s", ic, err)
                    continue
                task_list = detail.get("task_list") or []
                matched = False
                for t in task_list:
                    t_uid = t.get("user_id")
                    t_status = t.get("status")
                    if t_uid == uid and t_status == "PENDING":
                        logger.info("自动审批: 找到待处理任务 approval=%s instance=%s task_id=%s", approval_code, ic, t.get("id"))
                        process_auto_approve_for_task(
                            approval_code, ic, uid, t.get("id"), get_token
                        )
                        processed_count += 1
                        matched = True
                        break
                if not matched:
                    pending_uids = [t.get("user_id") for t in task_list if t.get("status") == "PENDING"]
                    logger.info(
                        "自动审批: 实例 %s 无匹配任务 (期望 uid=%s, PENDING 任务审批人=%s)",
                        ic, uid, pending_uids,
                    )
        if not found_any:
            logger.info("自动审批轮询: uid=%s 无待审批实例", uid)
    logger.info("自动审批轮询: 完成，本次处理 %d 个任务", processed_count)


def _iter_instances_for_user(user_id, get_token):
    """
    遍历各审批类型下的 PENDING 实例。
    注意：飞书 instances/query 的 user_id 按发起人过滤，不是待审批人。
    因此不传 user_id，查询所有 PENDING 实例，后续在 task_list 中按 user_id 筛选待审批任务。
    """
    token = get_token()
    end_ts = int(time.time() * 1000)
    start_ts = int((time.time() - 7 * 24 * 3600) * 1000)  # 近7天
    logger.info(
        "自动审批: 查询时间范围 %s ~ %s (近7天)",
        time.strftime("%Y-%m-%d %H:%M", time.localtime(start_ts / 1000)),
        time.strftime("%Y-%m-%d %H:%M", time.localtime(end_ts / 1000)),
    )
    for approval_code, approval_type in _get_approval_codes_to_query():
        if not approval_code:
            logger.debug("自动审批: 跳过空 approval_code")
            continue
        try:
            body = {
                "approval_code": approval_code,
                "instance_start_time_from": str(start_ts),
                "instance_start_time_to": str(end_ts),
                "instance_status": "PENDING",
            }
            logger.info(
                "自动审批: 请求 instances/query %s body=%s",
                approval_type,
                body,
            )
            res = httpx.post(
                "https://open.feishu.cn/open-apis/approval/v4/instances/query",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                params={"user_id_type": "user_id"},
                json=body,
                timeout=10,
            )
            if res.status_code != 200:
                try:
                    err_body = res.json()
                    logger.warning(
                        "自动审批: instances/query HTTP %s approval=%s code=%s msg=%s body=%s",
                        res.status_code, approval_code,
                        err_body.get("code"), err_body.get("msg"),
                        err_body,
                    )
                except Exception:
                    logger.warning(
                        "自动审批: instances/query HTTP %s approval=%s body=%s",
                        res.status_code, approval_code, res.text[:500],
                    )
                continue
            data = res.json()
            if data.get("code") != 0:
                logger.warning(
                    "自动审批: instances/query 失败 approval=%s code=%s msg=%s 完整响应=%s",
                    approval_code, data.get("code"), data.get("msg"), data,
                )
                continue
            page = data.get("data", {})
            logger.debug("自动审批: instances/query %s 响应 data 键=%s", approval_type, list(page.keys()) if isinstance(page, dict) else type(page))
            # 飞书 API 可能返回 instance_code_list 或 instance_list（新格式，需从 instance.code 提取）
            codes = page.get("instance_code_list", [])
            if not codes and page.get("instance_list"):
                codes = [
                    item.get("instance", {}).get("code")
                    for item in page["instance_list"]
                    if item.get("instance", {}).get("code")
                ]
            if codes:
                logger.info(
                    "自动审批: 查询到 %d 个 PENDING 实例 %s instance_codes=%s",
                    len(codes), approval_type, codes[:5] if len(codes) > 5 else codes,
                )
                yield approval_code, codes
            else:
                logger.info(
                    "自动审批: 查询 %s 返回 0 个 PENDING 实例 完整响应 data=%s",
                    approval_type,
                    page,
                )
            # 分页
            while page.get("page_token"):
                res2 = httpx.post(
                    "https://open.feishu.cn/open-apis/approval/v4/instances/query",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    params={"user_id_type": "user_id", "page_token": page["page_token"]},
                    json=body,
                    timeout=10,
                )
                data2 = res2.json()
                if data2.get("code") != 0:
                    logger.warning("自动审批: 分页查询失败 approval=%s code=%s", approval_code, data2.get("code"))
                    break
                page = data2.get("data", {})
                codes = page.get("instance_code_list", [])
                if not codes and page.get("instance_list"):
                    codes = [
                        item.get("instance", {}).get("code")
                        for item in page["instance_list"]
                        if item.get("instance", {}).get("code")
                    ]
                if codes:
                    yield approval_code, codes
        except Exception as e:
            logger.warning("自动审批: 查询实例失败 approval=%s: %s", approval_type, e)
