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

from approval_rules_loader import (
    check_auto_approve,
    get_auto_approve_user_ids,
    get_exclude_types,
    get_seal_type_rules,
)
from approval_types import (
    APPROVAL_CODES,
    FIELD_LABELS_REVERSE,
    FIELD_ID_FALLBACK,
    FIELD_LABELS,
)
from field_cache import get_form_fields

logger = logging.getLogger(__name__)

# 状态文件路径
_STATE_FILE = os.environ.get("AUTO_APPROVAL_STATE_FILE") or str(
    Path(__file__).resolve().parent / "auto_approval_state.json"
)
_state_lock = threading.RLock()
_auto_approval_enabled = None  # 从文件加载


def _load_state():
    """加载自动审批开关状态"""
    global _auto_approval_enabled
    path = Path(_STATE_FILE)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                _auto_approval_enabled = data.get("enabled", False)
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
                _auto_approval_enabled = rules.get("default_enabled", False)
                return
    except Exception:
        pass
    _auto_approval_enabled = False


def _save_state(enabled, user_id=""):
    """保存自动审批开关状态"""
    global _auto_approval_enabled
    with _state_lock:
        _auto_approval_enabled = enabled
        try:
            data = {
                "enabled": enabled,
                "updated_by": user_id,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            }
            with open(_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("保存自动审批状态失败: %s", e)


def is_auto_approval_enabled():
    """获取当前自动审批开关状态"""
    global _auto_approval_enabled
    if _auto_approval_enabled is None:
        _load_state()
    return bool(_auto_approval_enabled)


def set_auto_approval_enabled(enabled, user_id=""):
    """设置自动审批开关"""
    _save_state(bool(enabled), user_id)


# approval_code -> 工单类型
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


def parse_form_to_fields(approval_type, form_list, cached, get_token):
    """
    将飞书 form 反解析为逻辑字段 dict。
    form_list: 飞书实例返回的 form 数组
    cached: field_cache 的字段结构 {field_id: {name, type, ...}}
    """
    if not form_list or not cached:
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
            "comment": comment or "自动审批通过",
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
    """在审批实例下添加评论"""
    token = get_token()
    res = httpx.post(
        f"https://open.feishu.cn/open-apis/approval/v4/instances/{instance_code}/comments",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        params={"user_id_type": "user_id"},
        json={"content": content},
        timeout=10,
    )
    data = res.json()
    if data.get("code") == 0:
        logger.info("已添加审批评论: instance=%s", instance_code)
        return True, None
    logger.warning("添加评论失败: code=%s msg=%s", data.get("code"), data.get("msg"))
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
    下载审批附件。优先使用 drive 接口，失败时尝试 media 临时链接。
    返回 (content, None) 或 (None, err)
    """
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


def check_seal_with_ai(file_content, file_name, seal_type, get_token):
    """
    用印 AI 分析：三点判断
    1. 文件内容是否合法合规
    2. 文件内容存在哪些风险点
    3. 用户选择的用印类型与规则是否匹配
    file_content: bytes 或 None，为 None 时仅根据文件名/类型推断
    返回 (can_auto: bool, comment: str, risk_points: list)
    """
    from file_extraction import extract_text_from_file
    from deepseek_client import call_deepseek_with_retry

    seal_rules = get_seal_type_rules()
    rules_text = "\n".join([f"- {k}：{v}" for k, v in seal_rules.items()])

    file_text = ""
    if file_content and isinstance(file_content, bytes) and len(file_content) > 10:
        file_text = extract_text_from_file(file_content, file_name, get_token)
    has_content = bool(file_text and len(file_text.strip()) > 10)
    combined = f"文件名：{file_name}\n\n" + (
        f"文件内容摘要：\n{file_text[:6000]}" if has_content else "（文件内容无法提取，仅根据文件名和类型推断）"
    )

    prompt = f"""你是一个用印合规审核助手。请对以下文件进行三点分析：

{combined}

用户选择的用印类型：{seal_type}

用印类型与适用文件规则：
{rules_text}

请严格按以下三点分析，并返回 JSON：
1. legal_compliant: 文件内容是否合法合规（true/false）
2. risk_points: 文件内容存在的风险点列表，如无则 []
3. seal_match: 用户选择的用印类型「{seal_type}」与上述规则是否匹配（true/false）
4. comment: 综合说明（简短，用于审批意见）

返回格式示例：
{{"legal_compliant": true, "risk_points": [], "seal_match": true, "comment": "文件合法合规，用印类型匹配。"}}

只返回 JSON，不要其他内容。"""

    try:
        res = call_deepseek_with_retry(
            [{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            timeout=30,
            max_retries=2,
        )
        content = res.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        out = json.loads(content)
        legal = out.get("legal_compliant", False)
        risks = out.get("risk_points") or []
        if not isinstance(risks, list):
            risks = [str(risks)] if risks else []
        match = out.get("seal_match", False)
        comment = out.get("comment", "")

        can_auto = legal and match and len(risks) == 0
        if not can_auto:
            parts = []
            if not legal:
                parts.append("文件内容存在合规问题")
            if not match:
                parts.append(f"用印类型「{seal_type}」与文件类型不匹配")
            if risks:
                parts.append("风险点：" + "；".join(risks[:5]))
            comment = "【不符合自动审批规则】" + "；".join(parts) + "。请人工审批。"
        return can_auto, comment, risks
    except Exception as e:
        logger.exception("用印 AI 分析失败: %s", e)
        return False, f"AI 分析异常：{e}，请人工审批。", ["分析失败"]


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
    处理单条待审批任务：获取实例详情 -> 解析表单 -> 判断规则 -> 审批或评论
    """
    if not is_auto_approval_enabled():
        return
    if user_id not in get_auto_approve_user_ids():
        return
    approval_type = APPROVAL_CODE_TO_TYPE.get(approval_code)
    if not approval_type or approval_type in get_exclude_types():
        return

    detail, err = get_instance_detail(instance_code, get_token)
    if not detail:
        logger.warning("获取实例详情失败: %s", err)
        return

    form_str = detail.get("form", "[]")
    form_list = json.loads(form_str) if isinstance(form_str, str) else (form_str or [])
    cached = get_form_fields(approval_type, approval_code, get_token)
    fields = parse_form_to_fields(approval_type, form_list, cached, get_token)

    # 用印申请单：需要 AI 分析附件
    if approval_type == "用印申请单":
        seal_type = fields.get("seal_type", "")
        doc_name = fields.get("document_name", "未知")
        doc_type = fields.get("document_type", "")

        file_tokens = []
        for item in form_list:
            if item.get("type") in ("attach", "attachV2", "attachment", "attachmentV2", "file"):
                val = item.get("value", [])
                if isinstance(val, list):
                    for v in val:
                        if isinstance(v, dict):
                            tok = v.get("file_token") or v.get("code") or v.get("file_code")
                            if tok:
                                file_tokens.append(tok)
                        elif isinstance(v, str) and v.strip():
                            file_tokens.append(v.strip())
                elif val:
                    file_tokens.append(str(val))

        file_content = None
        file_name = f"{doc_name}.{doc_type}" if doc_type else (doc_name or "未知")
        if not file_tokens:
            add_approval_comment(
                instance_code,
                "【自动审批】用印申请单缺少附件，无法进行 AI 分析，请人工审批。",
                get_token,
            )
            return
        file_content, dl_err = _download_approval_file(file_tokens[0], get_token)
        if not file_content:
            logger.warning("用印附件下载失败 instance=%s: %s", instance_code, dl_err)
            add_approval_comment(
                instance_code,
                f"【自动审批】附件下载失败（{dl_err}），无法进行 AI 分析，请人工审批。",
                get_token,
            )
            return
        if not seal_type:
            add_approval_comment(
                instance_code,
                "【自动审批】用印申请单缺少印章类型，请人工审批。",
                get_token,
            )
            return
        try:
            can_auto, comment, risks = check_seal_with_ai(
                file_content, file_name, seal_type, get_token
            )
        except Exception as e:
            logger.warning("用印 AI 分析异常: %s", e)
            add_approval_comment(
                instance_code,
                f"【自动审批】AI 分析异常，请人工审批。{e}",
                get_token,
            )
            return
    else:
        can_auto, comment, risk_points = check_auto_approve(approval_type, fields)
        if can_auto is None:
            add_approval_comment(instance_code, "【自动审批】需人工审核。", get_token)
            return
        risks = risk_points or []

    if can_auto:
        approve_task(approval_code, instance_code, user_id, task_id, comment, get_token)
    else:
        fail_comment = f"【不符合自动审批规则】\n{comment}\n"
        if risks:
            fail_comment += "风险点：" + "；".join(risks[:5]) + "\n"
        fail_comment += "请人工审批。"
        add_approval_comment(instance_code, fail_comment, get_token)


def poll_and_process(get_token):
    """
    轮询待审批任务并处理。
    从实例详情 task_list 中筛选出 user_id 的 PENDING 任务。
    """
    for uid in get_auto_approve_user_ids():
        for approval_code, instance_codes in _iter_instances_for_user(uid, get_token):
            for ic in instance_codes:
                detail, _ = get_instance_detail(ic, get_token)
                if not detail:
                    continue
                task_list = detail.get("task_list") or []
                for t in task_list:
                    if t.get("user_id") == uid and t.get("status") == "PENDING":
                        process_auto_approve_for_task(
                            approval_code, ic, uid, t.get("id"), get_token
                        )
                        break


def _iter_instances_for_user(user_id, get_token):
    """遍历各审批类型下该用户相关的实例（含待审批）"""
    token = get_token()
    end_ts = int(time.time() * 1000)
    start_ts = int((time.time() - 7 * 24 * 3600) * 1000)  # 近7天
    for approval_code in APPROVAL_CODES.values():
        try:
            res = httpx.post(
                "https://open.feishu.cn/open-apis/approval/v4/instances/query",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                params={"user_id_type": "user_id"},
                json={
                    "user_id": user_id,
                    "approval_code": approval_code,
                    "instance_start_time_from": str(start_ts),
                    "instance_start_time_to": str(end_ts),
                    "instance_status": "PENDING",
                },
                timeout=10,
            )
            data = res.json()
            if data.get("code") != 0:
                continue
            page = data.get("data", {})
            codes = page.get("instance_code_list", [])
            if codes:
                yield approval_code, codes
            # 分页
            while page.get("page_token"):
                res2 = httpx.post(
                    "https://open.feishu.cn/open-apis/approval/v4/instances/query",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    params={"user_id_type": "user_id", "page_token": page["page_token"]},
                    json={
                        "user_id": user_id,
                        "approval_code": approval_code,
                        "instance_start_time_from": str(start_ts),
                        "instance_start_time_to": str(end_ts),
                        "instance_status": "PENDING",
                    },
                    timeout=10,
                )
                data2 = res2.json()
                if data2.get("code") != 0:
                    break
                page = data2.get("data", {})
                codes = page.get("instance_code_list", [])
                if codes:
                    yield approval_code, codes
        except Exception as e:
            logger.debug("查询实例失败: %s", e)
