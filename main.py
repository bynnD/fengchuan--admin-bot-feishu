import os
import re
import json
import uuid
import logging
from urllib.parse import quote
from collections import OrderedDict
import httpx
import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
from approval_types import (
    APPROVAL_CODES, FIELD_LABELS, APPROVAL_FIELD_HINTS, APPROVAL_USAGE_GUIDE,
    LINK_ONLY_TYPES, FIELD_ID_FALLBACK, FIELD_ORDER, DATE_FIELDS, FIELD_LABELS_REVERSE,
    IMAGE_SUPPORT_TYPES, FIELDLIST_SUBFIELDS_FALLBACK, get_admin_comment, get_file_extractor
)
from approval_rules_loader import check_switch_command, get_auto_approve_user_ids
from approval_auto import (
    add_approval_comment,
    get_auto_approval_status,
    is_auto_approval_enabled,
    run_pre_check,
    set_auto_approval_enabled,
    set_auto_approval_type_enabled,
    set_all_types_enabled,
    poll_and_process,
)
from pre_check_cache import set_pre_check_result
from field_cache import get_form_fields, get_sub_field_options, invalidate_cache, is_free_process, mark_free_process
from deepseek_client import call_deepseek_with_retry
import datetime
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

logger = logging.getLogger(__name__)

# 配置常量
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
SECRET_TOKEN = os.environ.get("SECRET_TOKEN", "")  # 可选，用于 /debug-form 等调试接口认证

# 飞书审批应用 ID（打开审批详情页用），可通过 FEISHU_APPROVAL_APP_ID 覆盖
FEISHU_APPROVAL_APP_ID = os.environ.get("FEISHU_APPROVAL_APP_ID", "cli_9cb844403dbb9108")

# 文件大小限制（字节），默认 50MB
MAX_FILE_SIZE = int(os.environ.get("MAX_FILE_SIZE", 50 * 1024 * 1024))

# 共享状态锁（SDK 可能多线程调用）
_state_lock = threading.RLock()

# 事件去重：带 TTL，最多保留 24 小时
PROCESSED_EVENTS = OrderedDict()  # event_id -> timestamp
PROCESSED_EVENTS_TTL = 24 * 3600
PROCESSED_EVENTS_MAX = 50000

CONVERSATIONS = {}
_token_cache = {"token": None, "expires_at": 0}
_token_lock = threading.Lock()

# 待办 TTL（秒）
PENDING_TTL = 60 * 60  # 60 分钟（用印选项卡片填写时间可能较长）
SEAL_INITIAL_TTL = 30 * 60  # 30 分钟
# 用户先上传附件但未说明意图时暂存，等用户说明用印/开票后再处理。支持多文件，结构 {open_id: {"files": [{...}], "created_at", "timer"}}
PENDING_FILE_UNCLEAR = {}
# 用印等待上传时收集多文件（防抖）：{open_id: {"files": [{...}], "user_id", "created_at", "timer"}}
PENDING_SEAL_UPLOAD = {}
SEAL_UPLOAD_DEBOUNCE_SEC = 4  # 连续上传多文件时，等待此秒数无新文件后批量处理
# 开票等待上传时收集多文件（防抖）：{open_id: {"files": [{...}], "user_id", "created_at", "timer"}}
PENDING_INVOICE_UPLOAD = {}
INVOICE_UPLOAD_DEBOUNCE_SEC = 12  # 连续上传多文件时，等待此秒数无新文件后批量处理，再统一发卡（合同+对账单需时间上传）
# 多文件用印排队：{open_id: {"items": [{file_name, file_code, doc_fields}, ...], "current_index": 0, "selections": [{lawyer, usage, count}, ...], "user_id", "created_at"}}
PENDING_SEAL_QUEUE = {}
FILE_INTENT_WAIT_SEC = 180  # 3 分钟内未说明意图则弹出选项卡

# 限流：每用户最小间隔（秒）
RATE_LIMIT_SEC = 2
# 开票选项卡片发卡去重：同一用户 3 秒内不重复发卡
_invoice_card_last_sent = {}  # open_id -> timestamp
INVOICE_CARD_DEDUP_SEC = 3

# 取消/重置意图关键词
CANCEL_PHRASES = ("取消", "算了", "不办了", "重新来", "重置", "不要了", "放弃", "不弄了")

client = lark.Client.builder() \
    .app_id(FEISHU_APP_ID) \
    .app_secret(FEISHU_APP_SECRET) \
    .build()


def _validate_env():
    """启动时校验必需环境变量，缺失则退出"""
    required = ["FEISHU_APP_ID", "FEISHU_APP_SECRET", "DEEPSEEK_API_KEY"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"缺少必需环境变量: {', '.join(missing)}，请配置后重试。")


def get_token():
    now = time.time()
    with _token_lock:
        if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
            return _token_cache["token"]
        res = httpx.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
            timeout=10
        )
        data = res.json()
        if data.get("code") != 0:
            err_msg = data.get("msg", "未知错误")
            err_code = data.get("code", "")
            raise RuntimeError(f"获取飞书 token 失败: code={err_code}, msg={err_msg}")
        token = data.get("tenant_access_token")
        if not token:
            raise RuntimeError("获取飞书 token 失败: 响应中无 tenant_access_token")
        _token_cache["token"] = token
        _token_cache["expires_at"] = now + data.get("expire", 7200)
        return _token_cache["token"]


def _event_processed(event_id):
    """检查事件是否已处理。已处理返回 True，未处理则标记并返回 False"""
    now = time.time()
    with _state_lock:
        if event_id in PROCESSED_EVENTS:
            return True
        to_remove = [eid for eid, ts in PROCESSED_EVENTS.items() if now - ts > PROCESSED_EVENTS_TTL]
        for eid in to_remove:
            del PROCESSED_EVENTS[eid]
        while len(PROCESSED_EVENTS) >= PROCESSED_EVENTS_MAX:
            PROCESSED_EVENTS.popitem(last=False)
        PROCESSED_EVENTS[event_id] = now
        return False


def _clean_expired_pending(open_id=None):
    """清理过期的 PENDING_* 和 SEAL_INITIAL_FIELDS。open_id 为 None 时清理所有用户"""
    now = time.time()
    to_notify = []
    with _state_lock:
        for oid in list(PENDING_SEAL.keys()) if open_id is None else ([open_id] if open_id in PENDING_SEAL else []):
            if oid in PENDING_SEAL and now - PENDING_SEAL[oid].get("created_at", 0) > PENDING_TTL:
                del PENDING_SEAL[oid]
                to_notify.append((oid, "用印申请单已超时，请重新发起。"))
        for oid in list(PENDING_INVOICE.keys()) if open_id is None else ([open_id] if open_id in PENDING_INVOICE else []):
            if oid in PENDING_INVOICE and now - PENDING_INVOICE[oid].get("created_at", 0) > PENDING_TTL:
                del PENDING_INVOICE[oid]
                entry = PENDING_INVOICE_UPLOAD.pop(oid, None)
                if entry and entry.get("timer"):
                    try:
                        entry["timer"].cancel()
                    except Exception:
                        pass
                to_notify.append((oid, "开票申请单已超时，请重新发起。"))
        for oid in list(PENDING_SEAL_QUEUE.keys()) if open_id is None else ([open_id] if open_id in PENDING_SEAL_QUEUE else []):
            if oid in PENDING_SEAL_QUEUE and now - PENDING_SEAL_QUEUE[oid].get("created_at", 0) > PENDING_TTL:
                del PENDING_SEAL_QUEUE[oid]
                to_notify.append((oid, "用印申请单已超时，请重新发起。"))
        for oid in list(SEAL_INITIAL_FIELDS.keys()) if open_id is None else ([open_id] if open_id in SEAL_INITIAL_FIELDS else []):
            data = SEAL_INITIAL_FIELDS.get(oid)
            created = data.get("created_at", 0) if isinstance(data, dict) else 0
            if created and now - created > SEAL_INITIAL_TTL:
                del SEAL_INITIAL_FIELDS[oid]
        for oid in list(PENDING_SEAL_UPLOAD.keys()) if open_id is None else ([open_id] if open_id in PENDING_SEAL_UPLOAD else []):
            if oid in PENDING_SEAL_UPLOAD and now - PENDING_SEAL_UPLOAD[oid].get("created_at", 0) > SEAL_INITIAL_TTL:
                entry = PENDING_SEAL_UPLOAD.pop(oid, None)
                if entry and entry.get("timer"):
                    try:
                        entry["timer"].cancel()
                    except Exception:
                        pass
        for oid in list(PENDING_FILE_UNCLEAR.keys()) if open_id is None else ([open_id] if open_id in PENDING_FILE_UNCLEAR else []):
            if oid in PENDING_FILE_UNCLEAR and now - PENDING_FILE_UNCLEAR[oid].get("created_at", 0) > PENDING_TTL:
                entry = PENDING_FILE_UNCLEAR.pop(oid, None)
                if entry and entry.get("timer"):
                    try:
                        entry["timer"].cancel()
                    except Exception:
                        pass
        for cid in list(PENDING_CONFIRM.keys()):
            if now - PENDING_CONFIRM[cid].get("created_at", 0) > CONFIRM_TTL:
                oid = PENDING_CONFIRM[cid].get("open_id")
                del PENDING_CONFIRM[cid]
                if oid and OPEN_ID_TO_CONFIRM.get(oid) == cid:
                    del OPEN_ID_TO_CONFIRM[oid]
        USER_STALE_TTL = 86400
        stale_users = [uid for uid, ts in _user_last_msg.items() if now - ts > USER_STALE_TTL]
        for uid in stale_users:
            _user_last_msg.pop(uid, None)
            CONVERSATIONS.pop(uid, None)
    for oid, msg in to_notify:
        send_message(oid, msg)


def _is_cancel_intent(text):
    """识别用户是否想取消当前流程"""
    t = (text or "").strip()
    if len(t) < 2:
        return False
    return any(p in t for p in CANCEL_PHRASES)


def _try_modify_confirm(open_id, user_id, text):
    """汇总卡出现后，用户通过对话修改字段。解析修改意图，更新字段并重新发卡"""
    with _state_lock:
        confirm_id = OPEN_ID_TO_CONFIRM.get(open_id)
        pending = PENDING_CONFIRM.get(confirm_id) if confirm_id else None
    if not pending:
        return False
    if time.time() - pending.get("created_at", 0) > CONFIRM_TTL:
        with _state_lock:
            PENDING_CONFIRM.pop(confirm_id, None)
            OPEN_ID_TO_CONFIRM.pop(open_id, None)
        send_message(open_id, "确认已超时，请重新发起。", use_red=True)
        return True
    if _is_cancel_intent(text):
        with _state_lock:
            PENDING_CONFIRM.pop(confirm_id, None)
            OPEN_ID_TO_CONFIRM.pop(open_id, None)
        send_message(open_id, "已取消，如需提交请重新发起。", use_red=True)
        return True
    approval_type = pending["approval_type"]
    fields = dict(pending["fields"])
    summary_cur = format_fields_summary(fields, approval_type)
    order = FIELD_ORDER.get(approval_type) or []
    labels = ", ".join(f"{k}({FIELD_LABELS.get(k,k)})" for k in order if k in fields)
    prompt = (
        f"用户正在确认【{approval_type}】的工单信息。\n当前信息：\n{summary_cur}\n\n"
        f"用户说：{text}\n\n"
        f"请解析用户要修改的字段。用户可能说「把金额改成1000」「购方名称改为XXX公司」「税号错了，应该是123」等。"
        f"可修改字段（逻辑键名）：{labels}\n"
        f"只返回JSON，格式如 {{\"amount\": 1000, \"buyer_name\": \"XXX公司\"}}，仅包含用户明确要修改的字段。"
        f"若用户未表达修改意图（如「确认」「没问题」「提交」等），返回 {{}}。"
        f"proof_file_type 为数组，如 [\"合同\",\"对账单\"]。只返回JSON，不要其他内容。"
    )
    try:
        res = call_deepseek_with_retry([{"role": "user", "content": prompt}], response_format={"type": "json_object"}, timeout=15)
        content = res.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        updates = json.loads(content) if content else {}
    except Exception as e:
        logger.warning("解析修改意图失败: %s", e)
        send_message(open_id, "未能识别您的修改内容，请明确说明要修改的字段及新值，如「把开票金额改成1000」。", use_red=True)
        return True
    if not updates or not isinstance(updates, dict):
        send_message(open_id, "如需修改，请明确说明要改的字段及新值。确认无误请直接点击卡片上的「确认提交」按钮。", use_red=True)
        return True
    for k, v in updates.items():
        if k in fields and v is not None:
            if isinstance(v, list):
                fields[k] = [str(x).strip() for x in v if x and str(x).strip()]
            else:
                fields[k] = str(v).strip() if v else ""
    with _state_lock:
        PENDING_CONFIRM.pop(confirm_id, None)
    admin_comment = get_admin_comment(approval_type, fields)
    summary = format_fields_summary(fields, approval_type)
    pre_check = run_pre_check(approval_type, fields, pending.get("file_codes"), get_token)
    send_confirm_card(open_id, approval_type, summary, admin_comment, user_id, fields, file_codes=pending.get("file_codes"), pre_check_result=pre_check)
    send_message(open_id, "已按您的要求修改，请再次确认信息无误后点击「确认提交」。", use_red=True)
    return True


def _parse_file_intents(text, file_count):
    """
    解析「第一个用印、第二个开票」等分别指定意图。
    返回 {0: "用印", 1: "开票"} 或 None（无法解析时）。
    支持：第1个用印、第一个文件用印、1用印、第2个开票 等。
    """
    if not text or file_count < 2:
        return None
    import re
    # 按逗号、顿号、分号、空格分割
    parts = re.split(r"[,，、;；\s]+", text)
    intents = {}
    ord_map = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5}
    for part in parts:
        part = part.strip()
        if not part:
            continue
        intent = None
        if "用印" in part or "盖章" in part:
            intent = "用印"
        elif "开票" in part:
            intent = "开票"
        if not intent:
            continue
        # 解析序号：第X个、第X、X个
        m = re.search(r"第?([一二三四五12345])个?", part) or re.search(r"^([12345])", part)
        if m:
            idx = ord_map.get(m.group(1), 1) - 1  # 转为 0-based
            if 0 <= idx < file_count:
                intents[idx] = intent
        elif len(intents) == 0 and file_count == 1:
            intents[0] = intent
    if not intents or not all(0 <= i < file_count for i in intents):
        return None
    # 未指定意图的文件沿用前一个，或默认用印
    last = "用印"
    for i in range(file_count):
        if i in intents:
            last = intents[i]
        else:
            intents[i] = last
    return intents


def _handle_split_file_intents(open_id, user_id, files_list, intents):
    """按意图拆分文件分别处理：部分用印、部分开票"""
    with _state_lock:
        entry = PENDING_FILE_UNCLEAR.pop(open_id, None)
        if entry and entry.get("timer"):
            try:
                entry["timer"].cancel()
            except Exception:
                pass
    seal_files = [f for i, f in enumerate(files_list) if intents.get(i) == "用印"]
    invoice_files = [f for i, f in enumerate(files_list) if intents.get(i) == "开票"]
    if seal_files:
        with _state_lock:
            SEAL_INITIAL_FIELDS[open_id] = {"fields": {}, "created_at": time.time()}
        _handle_file_message(open_id, user_id, None, None, files_list=seal_files)
    if invoice_files:
        with _state_lock:
            PENDING_INVOICE[open_id] = {
                "step": "need_files",
                "file_codes": [],
                "doc_fields": {},
                "user_id": user_id,
                "created_at": time.time(),
            }
        with _state_lock:
            if open_id not in PENDING_INVOICE_UPLOAD:
                PENDING_INVOICE_UPLOAD[open_id] = {"files": [], "user_id": user_id, "created_at": time.time(), "timer": None}
            for f in invoice_files[:1]:  # 开票仅支持单文件
                item = {"message_id": f.get("message_id"), "content_json": f.get("content_json", {}), "file_name": f.get("file_name", "未知文件")}
                if "resource_type" in f:
                    item["resource_type"] = f["resource_type"]
                PENDING_INVOICE_UPLOAD[open_id]["files"].append(item)
        if len(invoice_files) > 1:
            send_message(open_id, "每次仅支持开一张发票，已为您处理第一个文件。如需为其他文件开票，请单独发起。", use_red=True)
        _schedule_invoice_upload_process(open_id, user_id, immediate=True)
    if seal_files and invoice_files:
        send_message(open_id, f"已接收：{len(seal_files)} 份用印、{len(invoice_files)} 份开票，将分别处理。")
    elif seal_files:
        send_message(open_id, f"已接收 {len(seal_files)} 份用印文件，正在处理。")
    elif invoice_files:
        send_message(open_id, f"已接收 {len(invoice_files)} 份开票文件，正在处理。")


def download_message_file(message_id, file_key, file_type="file"):
    """从飞书消息下载文件。返回 (content, None) 成功，(None, 错误信息) 失败"""
    try:
        token = get_token()
        res = httpx.get(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}",
            params={"type": file_type},
            headers={"Authorization": f"Bearer {token}"},
            timeout=30
        )
        if res.status_code == 200:
            content = res.content
            if len(content) > MAX_FILE_SIZE:
                max_mb = MAX_FILE_SIZE // 1024 // 1024
                logger.warning("文件大小超过限制: %d > %d", len(content), MAX_FILE_SIZE)
                return None, f"文件大小超过限制（最大 {max_mb}MB），请压缩后重试"
            return content, None
        logger.error("下载文件失败: status=%s", res.status_code)
        return None, f"下载失败(status={res.status_code})"
    except Exception as e:
        logger.exception("下载文件异常: %s", e)
        return None, str(e)


def upload_approval_file(file_name, file_content):
    """上传文件到飞书审批，返回 (file_code, None) 成功，(None, 错误信息) 失败。超过 MAX_FILE_SIZE 则拒绝"""
    if len(file_content) > MAX_FILE_SIZE:
        max_mb = MAX_FILE_SIZE // 1024 // 1024
        return None, f"文件大小超过限制（最大 {max_mb}MB），请压缩后重试"
    try:
        token = get_token()
        res = httpx.post(
            "https://open.feishu.cn/open-apis/approval/v4/files/upload",
            headers={"Authorization": f"Bearer {token}"},
            data={"name": file_name, "type": "attachment"},
            files={"content": (file_name, file_content)},
            timeout=30
        )
        raw_text = res.text.strip()
        if raw_text.startswith("\ufeff"):
            raw_text = raw_text[1:]
        data = None
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as je:
            # "Extra data" 常因响应含前缀(如 BOM、数字)或拼接多个 JSON，尝试从首个 { 解析
            if "{" in raw_text:
                try:
                    data = json.loads(raw_text[raw_text.index("{"):])
                except json.JSONDecodeError:
                    pass
        if data is None:
            logger.warning("文件上传响应非JSON: status=%s, body前200字: %s", res.status_code, raw_text[:200])
            return None, "接口返回格式异常，请稍后重试"
        if data.get("code") == 0:
            d = data.get("data", {})
            # 飞书 v4 接口可能返回 urls_detail: [{code: "xxx", ...}]，code 在数组首项中
            urls = d.get("urls_detail") or []
            first = urls[0] if isinstance(urls, list) and urls else {}
            file_code = (
                (first.get("code") or first.get("file_code") or "")
                or d.get("code") or d.get("file_token") or d.get("file_code")
                or ""
            )
            logger.info("文件上传成功: %s -> %s", file_name, file_code)
            if not file_code:
                logger.warning("API 返回成功但无 file code，完整 data: %s", d)
                return None, "接口返回成功但未返回文件标识，请重试"
            return file_code, None
        err_msg = data.get("msg", "未知错误")
        err_code = data.get("code", "")
        logger.error("文件上传失败: code=%s, msg=%s", err_code, err_msg)
        return None, err_msg
    except Exception as e:
        logger.exception("文件上传异常: %s", e)
        return None, str(e)


def _sanitize_message_text(text):
    """飞书消息内容校验：移除控制字符、限制长度，避免 invalid message content 错误"""
    if not text or not isinstance(text, str):
        return " "
    # 移除控制字符（保留 \n \r \t）
    sanitized = "".join(c for c in text if c in "\n\r\t" or ord(c) >= 32)
    # 飞书文本消息限制约 20KB，预留余量
    max_len = 18000
    if len(sanitized) > max_len:
        sanitized = sanitized[:max_len] + "\n...(内容过长已截断)"
    return sanitized.strip() or " "


def _get_usage_guide_message():
    """生成使用简要说明，含各类型例句，便于用户一条消息完成工单"""
    lines = [
        "你好！我是行政助理，可帮你快速提交审批，尽量一条消息说完需求即可。",
        "",
    ]
    for name in APPROVAL_CODES.keys():
        guide = APPROVAL_USAGE_GUIDE.get(name)
        if not guide:
            continue
        brief, example, direct_send = guide
        prefix = "例如，直接发送：" if direct_send else "例如，你可以这样说："
        lines.append(f"【{name}】{brief}")
        lines.append(f"  {prefix}{example}")
        lines.append("")
    lines.append("请直接告诉我你要办理哪种，或按例句格式描述。")
    return "\n".join(lines)


def _process_approval_type_click(open_id, user_id, approval_type, example_text):
    """用户从工单类型选择卡片点击后，用例句走 AI 分析并处理（采购、外出、招待等）"""
    with _state_lock:
        conv_copy = list(CONVERSATIONS.get(open_id, []))
    result = analyze_message(conv_copy)
    requests = [r for r in result.get("requests", []) if r.get("approval_type") == approval_type]
    if not requests:
        send_message(open_id, f"未能识别{approval_type}相关信息，请补充说明。", use_red=True)
        return
    req = requests[0]
    miss = req.get("missing", [])
    fields_check = req.get("fields", {})
    if approval_type == "采购申请":
        cd = fields_check.get("cost_detail")
        if not cd or (isinstance(cd, list) and len(cd) == 0) or cd == "":
            if "cost_detail" not in miss:
                miss.append("cost_detail")
                req["missing"] = miss
    if approval_type == "招待/团建物资领用":
        idetail = fields_check.get("item_detail")
        if not idetail or (isinstance(idetail, list) and len(idetail) == 0) or idetail == "":
            if "item_detail" not in miss:
                miss.append("item_detail")
                req["missing"] = miss
    remaining_requests = [req]
    complete = [r for r in remaining_requests if not r.get("missing")]
    incomplete = [(r["approval_type"], r.get("missing", [])) for r in remaining_requests if r.get("missing")]
    for r in complete:
        at = r.get("approval_type")
        fields = r.get("fields", {})
        admin_comment = get_admin_comment(at, fields)
        summary = format_fields_summary(fields, at)
        if at in LINK_ONLY_TYPES:
            link = f"https://applink.feishu.cn/client/approval?tab=create&definitionCode={APPROVAL_CODES[at]}"
            send_card_message(open_id, f"【{at}】\n{summary}\n\n请点击下方按钮发起工单（需在飞书客户端内打开）。", link, f"打开{at}审批表单")
        else:
            token = get_token()
            if is_free_process(APPROVAL_CODES[at], token):
                link = f"https://applink.feishu.cn/client/approval?tab=create&definitionCode={APPROVAL_CODES[at]}"
                send_card_message(open_id, f"【{at}】\n{summary}\n\n该类型暂不支持自动创建，请点击下方按钮在飞书中发起。", link, f"打开{at}审批表单")
            else:
                pre_check = run_pre_check(at, fields, None, get_token)
                send_confirm_card(open_id, at, summary, admin_comment, user_id, fields, pre_check_result=pre_check)
    if incomplete:
        parts = [f"{at}还缺少：{'、'.join([FIELD_LABELS.get(m, m) for m in miss])}" for at, miss in incomplete]
        send_message(open_id, "请补充以下信息：\n" + "\n".join(parts), use_red=True)
    with _state_lock:
        if open_id in CONVERSATIONS:
            CONVERSATIONS[open_id].append({"role": "assistant", "content": "已处理" if complete else "请补充信息"})


def send_message(open_id, text, use_red=False):
    """发送消息。use_red=True 时以红色字体呈现（用于提示用户的语句）"""
    text = _sanitize_message_text(text)
    if use_red:
        safe = _escape_lark_md(text).replace("<", "&lt;").replace(">", "&gt;")
        content = f"<font color='red'>{safe}</font>"
        card = {
            "config": {"wide_screen_mode": True},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": content}}],
        }
        body = CreateMessageRequestBody.builder() \
            .receive_id(open_id) \
            .msg_type("interactive") \
            .content(json.dumps(card, ensure_ascii=False)) \
            .build()
    else:
        body = CreateMessageRequestBody.builder() \
            .receive_id(open_id) \
            .msg_type("text") \
            .content(json.dumps({"text": text}, ensure_ascii=False)) \
            .build()
    request = CreateMessageRequest.builder() \
        .receive_id_type("open_id") \
        .request_body(body) \
        .build()
    resp = client.im.v1.message.create(request)
    if not resp.success():
        logger.error("发送消息失败: %s, content前100字: %r", resp.msg, text[:100])


def send_card_message(open_id, text, url, btn_label, use_desktop_link=False):
    """发送卡片消息。use_desktop_link=True 时使用飞书官方审批 applink，在应用内打开"""
    if use_desktop_link and "instanceCode=" in url:
        m = re.search(r"instanceCode=([^&]+)", url)
        ic = m.group(1).strip() if m else ""
        if ic:
            # 飞书官方文档：https://open.feishu.cn/document/applink-protocol/supported-protocol/open-an-approval-page
            # 使用 client/mini_program/open 协议，在飞书应用内打开审批详情
            app_id = FEISHU_APPROVAL_APP_ID
            mobile_path = quote(f"pages/detail/index?instanceId={ic}", safe="")
            pc_path = quote(f"pc/pages/in-process/index?instanceId={ic}", safe="")
            mobile_url = f"https://applink.feishu.cn/client/mini_program/open?appId={app_id}&path={mobile_path}"
            pc_url = f"https://applink.feishu.cn/client/mini_program/open?mode=appCenter&appId={app_id}&path={pc_path}"
            btn_config = {"tag": "button", "text": {"tag": "plain_text", "content": btn_label}, "type": "primary", "multi_url": {"url": mobile_url, "pc_url": pc_url, "android_url": mobile_url, "ios_url": mobile_url}}
        else:
            btn_config = {"tag": "button", "text": {"tag": "plain_text", "content": btn_label}, "type": "primary", "url": url}
    else:
        btn_config = {"tag": "button", "text": {"tag": "plain_text", "content": btn_label}, "type": "primary", "url": url}
    card = {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": text}},
            {"tag": "action", "actions": [btn_config]}
        ]
    }
    body = CreateMessageRequestBody.builder() \
        .receive_id(open_id) \
        .msg_type("interactive") \
        .content(json.dumps(card, ensure_ascii=False)) \
        .build()
    request = CreateMessageRequest.builder() \
        .receive_id_type("open_id") \
        .request_body(body) \
        .build()
    resp = client.im.v1.message.create(request)
    if not resp.success():
        logger.error("发送卡片消息失败: %s", resp.msg)


def send_approval_type_options_card(open_id):
    """发送工单类型选择卡片，用户点击即可选择，无需文字输入"""
    text = "你好！我是行政助理，可帮你快速提交审批。\n\n请选择您要办理的工单类型："
    btns = []
    for name in APPROVAL_CODES.keys():
        btns.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": name},
            "type": "default",
            "behaviors": [{"type": "callback", "value": {"action": "approval_type_select", "approval_type": name}}],
        })
    card = {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": text}},
            {"tag": "action", "actions": btns},
        ],
    }
    body = CreateMessageRequestBody.builder() \
        .receive_id(open_id) \
        .msg_type("interactive") \
        .content(json.dumps(card, ensure_ascii=False)) \
        .build()
    request = CreateMessageRequest.builder() \
        .receive_id_type("open_id") \
        .request_body(body) \
        .build()
    resp = client.im.v1.message.create(request)
    if not resp.success():
        logger.error("发送工单类型选择卡片失败: %s", resp.msg)


def send_file_intent_options_card(open_id, file_names):
    """发送文件意图选择卡片：用印申请单 / 开票申请单，3 分钟内未说明意图时使用。file_names 可为 str 或 list"""
    if isinstance(file_names, list):
        names_str = "、".join(f"「{n}」" for n in file_names)
    else:
        names_str = f"「{file_names}」"
    text = (
        f"已收到文件{names_str}。\n\n"
        f"请选择您需要办理的业务："
    )
    card = {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": text}},
            {"tag": "action", "actions": [
                {"tag": "button", "text": {"tag": "plain_text", "content": "用印申请单（盖章）"}, "type": "primary",
                 "behaviors": [{"type": "callback", "value": {"action": "file_intent", "intent": "用印申请单"}}]},
                {"tag": "button", "text": {"tag": "plain_text", "content": "开票申请单"}, "type": "default",
                 "behaviors": [{"type": "callback", "value": {"action": "file_intent", "intent": "开票申请单"}}]},
            ]},
        ],
    }
    body = CreateMessageRequestBody.builder() \
        .receive_id(open_id) \
        .msg_type("interactive") \
        .content(json.dumps(card, ensure_ascii=False)) \
        .build()
    request = CreateMessageRequest.builder() \
        .receive_id_type("open_id") \
        .request_body(body) \
        .build()
    resp = client.im.v1.message.create(request)
    if not resp.success():
        logger.error("发送文件意图选项卡片失败: %s", resp.msg)


def _schedule_file_intent_card(open_id):
    """3 分钟后若用户仍未说明意图，发送意图选择选项卡。会取消该用户之前的定时器"""

    def _send():
        with _state_lock:
            pending = PENDING_FILE_UNCLEAR.get(open_id)
        if not pending:
            return
        files = pending.get("files", [])
        if not files:
            return
        file_names = [f.get("file_name", "未知文件") for f in files]
        send_file_intent_options_card(open_id, file_names)

    with _state_lock:
        old = PENDING_FILE_UNCLEAR.get(open_id)
        if old and old.get("timer"):
            try:
                old["timer"].cancel()
            except Exception:
                pass
    timer = threading.Timer(FILE_INTENT_WAIT_SEC, _send)
    timer.daemon = True
    timer.start()
    with _state_lock:
        if open_id in PENDING_FILE_UNCLEAR:
            PENDING_FILE_UNCLEAR[open_id]["timer"] = timer


def _schedule_invoice_upload_process(open_id, user_id, immediate=False):
    """开票等待上传时，防抖后批量处理已收集的文件，全部处理完再统一发送选项卡片。immediate=True 时立即处理（用于 file_intent 单次传入）"""

    def _process():
        with _state_lock:
            entry = PENDING_INVOICE_UPLOAD.pop(open_id, None)
        if not entry:
            return
        files = entry.get("files", [])
        uid = entry.get("user_id", "")
        if not files:
            return
        threading.Thread(target=lambda: _process_invoice_upload_batch(open_id, uid, files), daemon=True).start()

    with _state_lock:
        if open_id not in PENDING_INVOICE_UPLOAD:
            return
        old = PENDING_INVOICE_UPLOAD[open_id]
        if old and old.get("timer"):
            try:
                old["timer"].cancel()
            except Exception:
                pass
    if immediate:
        _process()
    else:
        timer = threading.Timer(INVOICE_UPLOAD_DEBOUNCE_SEC, _process)
        timer.daemon = True
        timer.start()
        with _state_lock:
            if open_id in PENDING_INVOICE_UPLOAD:
                PENDING_INVOICE_UPLOAD[open_id]["timer"] = timer


def _schedule_seal_upload_process(open_id, user_id):
    """用印等待上传时，防抖后批量处理已收集的文件。多文件走排队模式，单文件走单卡模式。"""

    def _process():
        with _state_lock:
            entry = PENDING_SEAL_UPLOAD.pop(open_id, None)
        if not entry:
            return
        files = entry.get("files", [])
        uid = entry.get("user_id", "")
        if not files:
            return
        # 构建 files_list 格式
        files_list = [
            {"message_id": f.get("message_id"), "content_json": f.get("content_json", {}), "file_name": f.get("file_name", "未知文件")}
            for f in files
        ]
        threading.Thread(target=lambda: _handle_file_message(open_id, uid, None, None, files_list=files_list), daemon=True).start()

    with _state_lock:
        if open_id not in PENDING_SEAL_UPLOAD:
            return
        old = PENDING_SEAL_UPLOAD[open_id]
        if old and old.get("timer"):
            try:
                old["timer"].cancel()
            except Exception:
                pass
    timer = threading.Timer(SEAL_UPLOAD_DEBOUNCE_SEC, _process)
    timer.daemon = True
    timer.start()
    with _state_lock:
        if open_id in PENDING_SEAL_UPLOAD:
            PENDING_SEAL_UPLOAD[open_id]["timer"] = timer


def _escape_lark_md(s):
    """转义 lark_md 中的特殊字符，避免被解析为链接等语法导致卡片报错"""
    if not s:
        return s
    return str(s).replace("[", "\\[").replace("]", "\\]")


def _validate_seal_options(doc_fields):
    """校验三项必选：律师是否已审核、盖章形式、盖章份数。通过返回 (True, None)，否则返回 (False, 错误提示)"""
    lawyer = str(doc_fields.get("lawyer_reviewed", "")).strip()
    usage = str(doc_fields.get("usage_method", "")).strip()
    count = str(doc_fields.get("document_count", "1")).strip()
    if lawyer not in ("是", "否"):
        return False, "请先完成律师审核、盖章方式、盖章份数的选择"
    if usage not in ("纸质章", "电子章", "外带印章"):
        return False, "请先完成律师审核、盖章方式、盖章份数的选择"
    if count not in ("1", "2", "3", "4", "5"):
        return False, "请先完成律师审核、盖章方式、盖章份数的选择"
    return True, None


def _build_seal_queue_card(doc_fields, file_name, index, total, is_last):
    """多文件排队模式：每文件一张卡，仅「确认」按钮。全部确认后再出「提交工单」汇总卡片"""
    card = _build_seal_options_card(doc_fields, file_name)
    btn_label = f"确认（{index + 1}/{total}）" if not is_last else "确认"
    btn_action = "seal_next" if not is_last else "seal_finish"
    btns = [{
        "tag": "button", "text": {"tag": "plain_text", "content": btn_label},
        "type": "primary",
        "behaviors": [{"type": "callback", "value": {"action": btn_action}}],
    }]
    for elem in card.get("elements", []):
        if elem.get("tag") == "action" and elem.get("actions"):
            acts = elem["actions"]
            if acts and acts[-1].get("text", {}).get("content") == "提交":
                elem["actions"] = btns
                break
    for elem in card.get("elements", []):
        if elem.get("tag") == "div" and elem.get("text", {}).get("tag") == "lark_md":
            c = elem["text"].get("content", "")
            if "已接收文件" in c:
                c = c.replace("选完后点击「提交」", "选完后点击「确认」")
                elem["text"]["content"] = f"**第 {index + 1}/{total} 份文件**\n\n" + c
                break
    return card


def _build_seal_options_card(doc_fields, file_name):
    """根据 doc_fields 构建用印选项卡片，已选选项使用 type=primary 显示蓝色边框"""
    opts = _get_seal_form_options()
    lawyer_opts = opts.get("lawyer_reviewed") or ["是", "否"]
    usage_opts = opts.get("usage_method") or ["纸质章", "电子章", "外带印章"]
    doc_name = doc_fields.get("document_name", file_name.rsplit(".", 1)[0] if file_name else "")
    text = (
        f"已接收文件：{_escape_lark_md(file_name or doc_name)}\n"
        f"· 文件名称：{_escape_lark_md(doc_name)}\n\n"
        f"请点击下方选项完成补充，选完后点击「提交」："
    )
    selected_lawyer = str(doc_fields.get("lawyer_reviewed", "")).strip()
    selected_usage = str(doc_fields.get("usage_method", "")).strip()
    selected_count = str(doc_fields.get("document_count", "1")).strip()
    lawyer_btns = [
        {"tag": "button", "text": {"tag": "plain_text", "content": v},
         "type": "primary" if v == selected_lawyer else "default",
         "behaviors": [{"type": "callback", "value": {"action": "seal_option", "field": "lawyer_reviewed", "value": v}}]}
        for v in lawyer_opts
    ]
    usage_btns = [
        {"tag": "button", "text": {"tag": "plain_text", "content": v},
         "type": "primary" if v == selected_usage else "default",
         "behaviors": [{"type": "callback", "value": {"action": "seal_option", "field": "usage_method", "value": v}}]}
        for v in usage_opts
    ]
    count_btns = [
        {"tag": "button", "text": {"tag": "plain_text", "content": str(i)},
         "type": "primary" if str(i) == selected_count else "default",
         "behaviors": [{"type": "callback", "value": {"action": "seal_option", "field": "document_count", "value": str(i)}}]}
        for i in range(1, 6)
    ]
    submit_btn = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": "提交"},
        "type": "primary",
        "behaviors": [{"type": "callback", "value": {"action": "seal_submit"}}],
    }
    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": text}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "plain_text", "content": "律师是否已审核（必填）", "lines": 1}},
            {"tag": "action", "actions": lawyer_btns},
            {"tag": "div", "text": {"tag": "plain_text", "content": "盖章形式（纸质章/电子章/外带印章）", "lines": 1}},
            {"tag": "action", "actions": usage_btns},
            {"tag": "div", "text": {"tag": "plain_text", "content": "文件数量（1-5份）", "lines": 1}},
            {"tag": "action", "actions": count_btns},
            {"tag": "hr"},
            {"tag": "action", "actions": [submit_btn]},
        ],
    }


def _update_seal_card_delayed(token, open_id, doc_fields, file_name, card=None, delay_sec=0.08):
    """延时更新用印选项卡片以显示选中状态（蓝色框）。delay_sec 尽量短以减少用户感知延迟。"""
    if not token or not open_id:
        return
    time.sleep(delay_sec)
    try:
        card = card or _build_seal_options_card(doc_fields, file_name)
        body = {"token": token, "card": {"open_ids": [open_id], "config": card.get("config", {}), "elements": card.get("elements", [])}}
        resp = httpx.post(
            "https://open.feishu.cn/open-apis/interactive/v1/card/update",
            headers={"Authorization": f"Bearer {get_token()}", "Content-Type": "application/json"},
            json=body,
            timeout=10,
        )
        data = resp.json()
        if data.get("code") != 0:
            logger.warning("延时更新用印卡片失败: code=%s msg=%s", data.get("code"), data.get("msg"))
    except Exception as e:
        logger.warning("延时更新用印卡片异常: %s", e)


def _update_invoice_card_delayed(token, open_id, card, delay_sec=0.08):
    """延时更新开票选项卡片以显示选中状态（蓝色 primary 按钮）"""
    if not token or not open_id or not card:
        return
    time.sleep(delay_sec)
    try:
        body = {"token": token, "card": {"open_ids": [open_id], "config": card.get("config", {}), "elements": card.get("elements", [])}}
        resp = httpx.post(
            "https://open.feishu.cn/open-apis/interactive/v1/card/update",
            headers={"Authorization": f"Bearer {get_token()}", "Content-Type": "application/json"},
            json=body,
            timeout=10,
        )
        data = resp.json()
        if data.get("code") != 0:
            logger.warning("延时更新开票卡片失败: code=%s msg=%s", data.get("code"), data.get("msg"))
    except Exception as e:
        logger.warning("延时更新开票卡片异常: %s", e)


def _send_seal_final_card(open_id, total):
    """全部文件确认后，发送汇总卡片，含「提交工单」按钮"""
    card = {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"以上 **{total}** 份文件均已选择完成，请点击下方按钮生成一张工单。"}},
            {"tag": "hr"},
            {"tag": "action", "actions": [{
                "tag": "button", "text": {"tag": "plain_text", "content": "提交工单"},
                "type": "primary",
                "behaviors": [{"type": "callback", "value": {"action": "seal_submit_queue"}}],
            }]},
        ],
    }
    body = CreateMessageRequestBody.builder() \
        .receive_id(open_id) \
        .msg_type("interactive") \
        .content(json.dumps(card, ensure_ascii=False)) \
        .build()
    request = CreateMessageRequest.builder() \
        .receive_id_type("open_id") \
        .request_body(body) \
        .build()
    resp = client.im.v1.message.create(request)
    if not resp.success():
        logger.error("发送用印最终提交卡片失败: %s", resp.msg)


def _send_seal_queue_card(open_id, user_id, queue_data):
    """发送排队模式下当前文件的选项卡片"""
    items = queue_data.get("items", [])
    idx = queue_data.get("current_index", 0)
    if idx >= len(items):
        return
    item = items[idx]
    card = _build_seal_queue_card(
        item["doc_fields"], item["file_name"], idx, len(items), idx == len(items) - 1
    )
    body = CreateMessageRequestBody.builder() \
        .receive_id(open_id) \
        .msg_type("interactive") \
        .content(json.dumps(card, ensure_ascii=False)) \
        .build()
    request = CreateMessageRequest.builder() \
        .receive_id_type("open_id") \
        .request_body(body) \
        .build()
    resp = client.im.v1.message.create(request)
    if not resp.success():
        logger.error("发送用印排队卡片失败: %s", resp.msg)


def send_seal_options_card(open_id, user_id, doc_fields, file_codes, file_name):
    """发送用印补充选项卡片：律师是否已审核、盖章形式、文件数量，选完后点击提交。file_codes 为 list"""
    card = _build_seal_options_card(doc_fields, file_name)
    body = CreateMessageRequestBody.builder() \
        .receive_id(open_id) \
        .msg_type("interactive") \
        .content(json.dumps(card, ensure_ascii=False)) \
        .build()
    request = CreateMessageRequest.builder() \
        .receive_id_type("open_id") \
        .request_body(body) \
        .build()
    resp = client.im.v1.message.create(request)
    if not resp.success():
        logger.error("发送用印选项卡片失败: %s", resp.msg)


def send_confirm_card(open_id, approval_type, summary, admin_comment, user_id, fields, file_codes=None, pre_check_result=None):
    """发送工单确认卡片，用户点击「确认」后创建工单。支持用户通过对话修改字段后重新发卡。
    pre_check_result: (compliant, comment, risks) 预检结果，不合规时在卡片中展示不符合项（不阻止提交）"""
    confirm_id = str(uuid.uuid4())
    compliant = True
    pre_comment = ""
    pre_risks = []
    if pre_check_result:
        compliant, pre_comment, pre_risks = pre_check_result
    with _state_lock:
        old_cid = OPEN_ID_TO_CONFIRM.pop(open_id, None)
        if old_cid:
            PENDING_CONFIRM.pop(old_cid, None)
        OPEN_ID_TO_CONFIRM[open_id] = confirm_id
        PENDING_CONFIRM[confirm_id] = {
            "open_id": open_id,
            "user_id": user_id,
            "approval_type": approval_type,
            "fields": dict(fields),
            "file_codes": dict(file_codes) if file_codes else None,
            "admin_comment": admin_comment,
            "created_at": time.time(),
            "pre_check": {"compliant": compliant, "comment": pre_comment, "risks": pre_risks},
        }
    text = f"【{approval_type}】\n\n{_escape_lark_md(summary)}\n\n"
    if not compliant and (pre_comment or pre_risks):
        # 整合预检提示，避免「附件中仅有合同」与「风险点：仅合同」等重复表述
        risks_to_show = []
        if pre_risks:
            redundant_with_contract = {"仅合同"}
            if pre_comment and "合同" in pre_comment:
                risks_to_show = [r for r in pre_risks[:5] if r not in redundant_with_contract]
            else:
                risks_to_show = pre_risks[:5]
        parts = []
        if pre_comment:
            parts.append(pre_comment.strip())
        if risks_to_show:
            parts.append("风险点：" + "；".join(risks_to_show))
        consolidated = "；".join(parts) if parts else ""
        text += f"<font color='red'>**预检提示**：{_escape_lark_md(consolidated)}</font>\n\n"
    text += "<font color='red'>请确认以上信息无误后，点击下方按钮提交工单。\n\n若信息有误，可直接回复说明要修改的字段及新值（如「把开票金额改成1000」），我会重新发卡供您确认。</font>"
    btn_config = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": "确认提交"},
        "type": "primary",
        "behaviors": [{"type": "callback", "value": {"confirm_id": confirm_id}}],
    }
    card = {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": text}},
            {"tag": "action", "actions": [btn_config]},
        ],
    }
    body = CreateMessageRequestBody.builder() \
        .receive_id(open_id) \
        .msg_type("interactive") \
        .content(json.dumps(card, ensure_ascii=False)) \
        .build()
    request = CreateMessageRequest.builder() \
        .receive_id_type("open_id") \
        .request_body(body) \
        .build()
    resp = client.im.v1.message.create(request)
    if not resp.success():
        logger.error("发送确认卡片失败: %s", resp.msg)
        with _state_lock:
            PENDING_CONFIRM.pop(confirm_id, None)
            if OPEN_ID_TO_CONFIRM.get(open_id) == confirm_id:
                del OPEN_ID_TO_CONFIRM[open_id]


def on_card_action_confirm(data):
    """处理用户点击确认按钮的回调，创建工单；也处理用印选项卡片的点击"""
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
    try:
        ev = data.event
        if not ev:
            return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": "参数无效"}})
        operator = ev.operator
        action = ev.action
        open_id = operator.open_id if operator else ""
        user_id = operator.user_id if operator else ""
        value = action.value if action and action.value else {}
        if isinstance(value, str):
            try:
                value = json.loads(value) if value else {}
            except json.JSONDecodeError:
                value = {}
        # 工单类型选择卡片：用户点击后直接进入对应流程
        if value.get("action") == "approval_type_select":
            at = value.get("approval_type", "")
            if at not in APPROVAL_CODES:
                return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": "无效的工单类型"}})
            def _handle_type_select():
                if at == "用印申请单":
                    with _state_lock:
                        SEAL_INITIAL_FIELDS[open_id] = {"fields": {}, "created_at": time.time()}
                    send_message(open_id, "请补充以下信息：\n用印申请单还缺少：上传用章文件\n请先上传需要盖章的文件（Word/PDF/图片均可），我会自动识别内容。", use_red=True)
                elif at == "开票申请单":
                    with _state_lock:
                        PENDING_INVOICE[open_id] = {
                            "step": "need_files",
                            "file_codes": [],
                            "doc_fields": {},
                            "user_id": user_id,
                            "created_at": time.time(),
                        }
                    send_message(open_id, "请补充以下信息：\n开票申请单需要：上传开票凭证\n请上传凭证（结算单+合同、合同+银行水单、合同+订单明细、电商发货/收款截图等，Word/PDF/图片均可），我会自动识别类型。\n\n**说明**：每次仅支持开一张发票，您上传的所有文件将合并为一张发票的凭证。", use_red=True)
                else:
                    # 采购、外出、招待等：模拟用户消息，走 AI 分析流程
                    example = (APPROVAL_USAGE_GUIDE.get(at) or ("", "", False))[1]
                    with _state_lock:
                        CONVERSATIONS.setdefault(open_id, [])
                        CONVERSATIONS[open_id].append({"role": "user", "content": example})
                        if len(CONVERSATIONS[open_id]) > 10:
                            CONVERSATIONS[open_id] = CONVERSATIONS[open_id][-10:]
                    _process_approval_type_click(open_id, user_id, at, example)
            threading.Thread(target=_handle_type_select, daemon=True).start()
            return P2CardActionTriggerResponse(d={"toast": {"type": "success", "content": f"已选择{at}，正在处理"}})

        # 文件意图选择卡片：用印申请单 / 开票申请单（3 分钟内未说明意图时弹出）
        if value.get("action") == "file_intent":
            intent = value.get("intent", "")
            with _state_lock:
                pending_file = PENDING_FILE_UNCLEAR.pop(open_id, None)
                if pending_file and pending_file.get("timer"):
                    try:
                        pending_file["timer"].cancel()
                    except Exception:
                        pass
            if not pending_file or not pending_file.get("files"):
                return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": "会话已过期，请重新上传文件"}})
            if intent == "用印申请单":
                files_list = pending_file.get("files", [])
                with _state_lock:
                    SEAL_INITIAL_FIELDS[open_id] = {"fields": {}, "created_at": time.time()}
                if files_list:
                    threading.Thread(target=lambda: _handle_file_message(open_id, user_id, None, None, files_list=files_list), daemon=True).start()
                return P2CardActionTriggerResponse(d={"toast": {"type": "success", "content": "已选择用印申请单，正在处理"}})
            if intent == "开票申请单":
                files_list = pending_file.get("files", [])
                with _state_lock:
                    PENDING_INVOICE[open_id] = {
                        "step": "need_files",
                        "file_codes": [],
                        "doc_fields": {},
                        "user_id": user_id,
                        "created_at": time.time(),
                    }
                    if open_id not in PENDING_INVOICE_UPLOAD:
                        PENDING_INVOICE_UPLOAD[open_id] = {"files": [], "user_id": user_id, "created_at": time.time(), "timer": None}
                    for f in files_list:
                        item = {"message_id": f.get("message_id"), "content_json": f.get("content_json", {}), "file_name": f.get("file_name", "未知文件")}
                        if "resource_type" in f:
                            item["resource_type"] = f["resource_type"]
                        PENDING_INVOICE_UPLOAD[open_id]["files"].append(item)
                if files_list:
                    _schedule_invoice_upload_process(open_id, user_id, immediate=True)
                return P2CardActionTriggerResponse(d={"toast": {"type": "success", "content": "已选择开票申请单，正在处理"}})
            return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": "参数无效"}})
        # 用印选项卡片（含排队模式）：律师是否已审核、盖章形式、文件数量
        if value.get("action") == "seal_option":
            field = value.get("field")
            val = value.get("value")
            if not field or not val:
                return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": "参数无效"}})
            with _state_lock:
                queue_data = PENDING_SEAL_QUEUE.get(open_id)
                pending = PENDING_SEAL.get(open_id)
            if queue_data:
                idx = queue_data.get("current_index", 0)
                items = queue_data.get("items", [])
                if idx < len(items):
                    doc_fields = items[idx]["doc_fields"]
                    if field == "document_count" and val in ("1", "2", "3", "4", "5"):
                        doc_fields[field] = val
                    elif field in ("lawyer_reviewed", "usage_method"):
                        doc_fields[field] = val
                    queue_data["created_at"] = time.time()
                    update_token = getattr(ev, "token", None) or getattr(getattr(ev, "event", None), "token", None) or ""
                    if update_token:
                        qcard = _build_seal_queue_card(doc_fields, items[idx]["file_name"], idx, len(items), idx == len(items) - 1)
                        threading.Thread(target=lambda t=update_token, oid=open_id, c=qcard: _update_seal_card_delayed(t, oid, None, None, card=c), daemon=True).start()
                    return P2CardActionTriggerResponse(d={"toast": {"type": "success", "content": f"已选择：{val}"}})
            if not pending:
                return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": "会话已过期，请重新上传文件"}})
            pending["created_at"] = time.time()  # 刷新会话，延长 TTL
            doc_fields = pending["doc_fields"]
            if field == "document_count" and val in ("1", "2", "3", "4", "5"):
                doc_fields[field] = val
            elif field in ("lawyer_reviewed", "usage_method"):
                doc_fields[field] = val
            update_token = getattr(ev, "token", None) or getattr(getattr(ev, "event", None), "token", None) or ""
            if update_token:
                _doc, _fname = dict(doc_fields), pending.get("file_name", "")
                threading.Thread(target=lambda t=update_token, oid=open_id, d=_doc, f=_fname: _update_seal_card_delayed(t, oid, d, f), daemon=True).start()
            return P2CardActionTriggerResponse(d={"toast": {"type": "success", "content": f"已选择：{val}"}})

        # 多文件排队：下一份（确认）
        if value.get("action") == "seal_next":
            with _state_lock:
                queue_data = PENDING_SEAL_QUEUE.get(open_id)
            if not queue_data:
                return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": "会话已过期，请重新上传文件"}})
            idx = queue_data.get("current_index", 0)
            items = queue_data.get("items", [])
            if idx >= len(items):
                return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": "数据异常"}})
            doc_fields = items[idx]["doc_fields"]
            ok, err = _validate_seal_options(doc_fields)
            if not ok:
                return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": err}})
            lawyer = str(doc_fields.get("lawyer_reviewed", "")).strip()
            usage = str(doc_fields.get("usage_method", "")).strip()
            count = str(doc_fields.get("document_count", "1")).strip()
            queue_data["selections"].append({"lawyer_reviewed": lawyer, "usage_method": usage, "document_count": count or "1"})
            queue_data["current_index"] = idx + 1
            queue_data["created_at"] = time.time()
            _send_seal_queue_card(open_id, user_id, queue_data)
            return P2CardActionTriggerResponse(d={"toast": {"type": "success", "content": f"已记录第 {idx + 1} 份，请选择第 {idx + 2} 份"}})

        # 多文件排队：确认（最后一份，仅保存选择，不发新卡）
        if value.get("action") == "seal_finish":
            with _state_lock:
                queue_data = PENDING_SEAL_QUEUE.get(open_id)
            if not queue_data:
                return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": "会话已过期，请重新上传文件"}})
            idx = queue_data.get("current_index", 0)
            items = queue_data.get("items", [])
            if idx >= len(items):
                return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": "数据异常"}})
            doc_fields = items[idx]["doc_fields"]
            ok, err = _validate_seal_options(doc_fields)
            if not ok:
                return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": err}})
            lawyer = str(doc_fields.get("lawyer_reviewed", "")).strip()
            usage = str(doc_fields.get("usage_method", "")).strip()
            count = str(doc_fields.get("document_count", "1")).strip()
            queue_data["selections"].append({"lawyer_reviewed": lawyer, "usage_method": usage, "document_count": count or "1"})
            queue_data["created_at"] = time.time()
            _send_seal_final_card(open_id, len(items))
            return P2CardActionTriggerResponse(d={"toast": {"type": "success", "content": "已选择完成，请点击提交工单"}})

        # 多文件排队：提交工单（在汇总卡片上点击）
        if value.get("action") == "seal_submit_queue":
            with _state_lock:
                queue_data = PENDING_SEAL_QUEUE.get(open_id)
            if not queue_data:
                return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": "会话已过期，请重新上传文件"}})
            items = queue_data.get("items", [])
            selections = list(queue_data.get("selections", []))
            # 若用户直接点提交工单未先点确认，补全最后一份的选择
            if len(selections) < len(items):
                idx = len(selections)
                doc_fields = items[idx]["doc_fields"]
                ok, err = _validate_seal_options(doc_fields)
                if not ok:
                    return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": err}})
                lawyer = str(doc_fields.get("lawyer_reviewed", "")).strip()
                usage = str(doc_fields.get("usage_method", "")).strip()
                count = str(doc_fields.get("document_count", "1")).strip()
                selections.append({"lawyer_reviewed": lawyer, "usage_method": usage, "document_count": count or "1"})
            elif len(selections) != len(items):
                return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": "数据异常，请重新发起"}})
            with _state_lock:
                queue_data = PENDING_SEAL_QUEUE.pop(open_id, None)
            if not queue_data:
                return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": "会话已过期，请重新上传文件"}})
            def _do_seal_queue_async():
                _do_create_seal_multi(open_id, queue_data["user_id"], items, selections)

            threading.Thread(target=_do_seal_queue_async, daemon=True).start()
            return P2CardActionTriggerResponse(d={"toast": {"type": "success", "content": "正在生成工单，请稍候"}})

        # 开票选项卡片：发票类型、开票项目（保存选项并更新卡片以显示蓝色选中状态）
        if value.get("action") == "invoice_option":
            field = value.get("field")
            val = value.get("value")
            if not field or not val or field not in ("invoice_type", "invoice_items"):
                return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": "参数无效"}})
            with _state_lock:
                pending = PENDING_INVOICE.get(open_id)
            if not pending or pending.get("step") != "user_fields":
                return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": "会话已过期，请重新发起开票申请单"}})
            pending["doc_fields"][field] = val
            pending["created_at"] = time.time()
            update_token = getattr(ev, "token", None) or getattr(getattr(ev, "event", None), "token", None) or ""
            if update_token:
                prefix = pending.get("summary_prefix", "")
                card = _build_invoice_options_card(pending["doc_fields"], prefix)
                threading.Thread(target=lambda t=update_token, oid=open_id, c=card: _update_invoice_card_delayed(t, oid, c), daemon=True).start()
            return P2CardActionTriggerResponse(d={"toast": {"type": "success", "content": f"已选择：{val}"}})

        # 开票确认提交：选项选完后点击
        if value.get("action") == "invoice_submit":
            with _state_lock:
                pending = PENDING_INVOICE.pop(open_id, None)
            if not pending or pending.get("step") != "user_fields":
                return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": "会话已过期，请重新发起开票申请单"}})
            doc_fields = pending.get("doc_fields", {})
            if not doc_fields.get("invoice_type") or not doc_fields.get("invoice_items"):
                with _state_lock:
                    PENDING_INVOICE[open_id] = pending
                return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": "请先选择发票类型和开票项目"}})
            fc_list = pending.get("file_codes", [])
            if not fc_list:
                return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": "缺少开票凭证，请重新发起"}})
            def _do_invoice_async():
                _do_create_invoice(open_id, user_id, doc_fields, fc_list)
            threading.Thread(target=_do_invoice_async, daemon=True).start()
            return P2CardActionTriggerResponse(d={"toast": {"type": "success", "content": "正在生成工单，请稍候"}})

        # 用印提交：选完后点击提交，创建工单
        if value.get("action") == "seal_submit":
            with _state_lock:
                pending = PENDING_SEAL.pop(open_id, None)
            if not pending:
                return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": "会话已过期，请重新上传文件"}})
            doc_fields = pending["doc_fields"]
            lawyer = str(doc_fields.get("lawyer_reviewed", "")).strip()
            usage = str(doc_fields.get("usage_method", "")).strip()
            count = str(doc_fields.get("document_count", "1")).strip()
            if lawyer not in ("是", "否"):
                with _state_lock:
                    PENDING_SEAL[open_id] = pending
                return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": "请先选择「律师是否已审核」"}})
            if usage not in ("纸质章", "电子章", "外带印章"):
                with _state_lock:
                    PENDING_SEAL[open_id] = pending
                return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": "请先选择「盖章形式」"}})
            if count not in ("1", "2", "3", "4", "5"):
                doc_fields.setdefault("document_count", "1")
            file_codes = pending.get("file_codes") or []

            def _do_seal_async():
                _do_create_seal(open_id, user_id, doc_fields, file_codes, direct_create=True)

            threading.Thread(target=_do_seal_async, daemon=True).start()
            return P2CardActionTriggerResponse(d={"toast": {"type": "success", "content": "正在生成工单，请稍候"}})

        confirm_id = value.get("confirm_id", "")
        if not confirm_id:
            return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": "参数无效"}})
        with _state_lock:
            pending = PENDING_CONFIRM.pop(confirm_id, None)
            if pending and OPEN_ID_TO_CONFIRM.get(open_id) == confirm_id:
                del OPEN_ID_TO_CONFIRM[open_id]
        if not pending:
            return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": "确认已过期，请重新发起"}})
        if time.time() - pending.get("created_at", 0) > CONFIRM_TTL:
            return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": "确认已超时，请重新发起"}})
        approval_type = pending["approval_type"]
        fields = pending["fields"]
        file_codes = pending.get("file_codes")
        admin_comment = pending.get("admin_comment", "")
        pre_check = pending.get("pre_check", {})

        def _create_and_notify():
            nonlocal open_id
            success, msg, resp_data, summary = create_approval(user_id, approval_type, fields, file_codes=file_codes)
            if success:
                with _state_lock:
                    if open_id in CONVERSATIONS:
                        CONVERSATIONS[open_id] = []
                instance_code = resp_data.get("instance_code", "")
                if instance_code:
                    compliant = pre_check.get("compliant", True)
                    comment = pre_check.get("comment", "")
                    risks = pre_check.get("risks", [])
                    set_pre_check_result(instance_code, compliant, comment, risks)
                    if compliant:
                        add_approval_comment(instance_code, "AI已审核通过", get_token, user_id=user_id)
                    elif comment or risks:
                        fail_comment = comment
                        if risks:
                            fail_comment = (fail_comment + "\n风险点：" + "；".join(risks[:5])).strip()
                        add_approval_comment(instance_code, fail_comment, get_token, user_id=user_id)
                    link = f"https://applink.feishu.cn/client/approval?instanceCode={instance_code}"
                    send_card_message(open_id, "工单已创建，点击下方按钮查看：", link, "查看工单", use_desktop_link=True)
                else:
                    send_message(open_id, f"· {approval_type}：✅ 已提交\n{summary}")
            else:
                send_message(open_id, f"提交失败：{msg}", use_red=True)

        threading.Thread(target=_create_and_notify, daemon=True).start()
        return P2CardActionTriggerResponse(d={"toast": {"type": "success", "content": "正在提交，请稍候"}})
    except Exception as e:
        logger.exception("卡片确认回调处理失败: %s", e)
        return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": "系统异常，请稍后重试"}})


def analyze_message(history):
    approval_list = "\n".join([f"- {k}" for k in APPROVAL_CODES.keys()])
    field_hints = "\n".join([f"{k}: {v}" for k, v in APPROVAL_FIELD_HINTS.items()])
    today = datetime.date.today()
    system_prompt = (
        f"你是一个行政助理，帮员工提交审批申请。今天是{today}。\n"
        f"可处理的审批类型：\n{approval_list}\n\n"
        f"各类型需要的字段：\n{field_hints}\n\n"
        f"【关键】分析用户最新消息，可能包含一个或多个审批需求，分别识别并提取。"
        f"例如「我要采购笔记本，还要给合同盖章」= 采购申请 + 用印申请单。"
        f"每个需求单独列出，每个需求的 fields 和 missing 独立。\n\n"
        f"重要规则：\n"
        f"1. 尽量从用户消息中推算字段，不要轻易列为missing\n"
        f"2. 明天、后天、下周一等换算成具体日期(YYYY-MM-DD)\n"
        f"3. 只有真的无法推断的字段才放入missing\n"
        f"4. reason可根据上下文推断，实在没有才列为missing\n"
        f"5. 采购：purchase_reason可包含具体物品，expected_date为期望交付时间\n"
        f"6. 采购的cost_detail是费用明细列表(必填)，每项必须含名称、规格、数量、金额。"
        f"「是否有库存」由审批人填写，发起人不填，不要提取。"
        f"格式为[{{\"名称\":\"笔记本电脑\",\"规格\":\"ThinkPad X1\",\"数量\":\"1\",\"金额\":\"8000\"}}]。"
        f"缺少名称/规格/数量/金额任一项就把cost_detail列入missing。purchase_reason可从物品信息推断(如'采购笔记本电脑')。"
        f"purchase_type(采购类别)可根据采购物品自动推断，如办公电脑、办公桌→办公用品，设备、机器→设备类等。\n"
        f"7. 招待/团建物资领用：item_detail是物品明细列表(必填)，每项含名称、数量。"
        f"格式为[{{\"名称\":\"矿泉水\",\"数量\":\"2\"}}]。缺少名称或数量任一项就把item_detail列入missing。\n"
        f"8. 用印申请单：识别到用印需求时，只提取对话中能得到的字段(company/seal_type/reason等)，"
        f"document_name/document_type不需要用户说，会从上传文件自动获取。"
        f"lawyer_reviewed(律师是否已审核)必须用户明确提供「是」或「否」，未明确说明则放入 missing。"
        f"若用户明确说「盖公章」「要盖公章」「公章」等，必须将 seal_type 提取为「公章」，不要放入 missing。"
        f"若用户还没上传文件，在 unclear 中提示「请上传需要盖章的文件」。\n\n"
        f"返回JSON：\n"
        f"- requests: 数组，每项含 approval_type、fields、missing\n"
        f"  若只有1个需求，数组长度为1；若无法识别任何需求，返回空数组\n"
        f"- unclear: 无法判断时用中文说明（requests为空时必填）\n"
        f"只返回JSON。"
    )
    messages = [{"role": "system", "content": system_prompt}] + history
    try:
        res = call_deepseek_with_retry(messages, response_format={"type": "json_object"}, timeout=30)
        content = res.json()["choices"][0]["message"]["content"]
        if content is None:
            raise ValueError("AI 返回内容为空")
        content = content.strip()
        if not content:
            raise ValueError("AI 返回内容为空")
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        if not content:
            raise ValueError("AI 返回内容为空")
        try:
            raw = json.loads(content)
        except json.JSONDecodeError as je:
            logger.warning("AI 返回非 JSON，content 前 200 字: %r", content[:200])
            raise ValueError(f"AI 返回格式异常: {je}") from je
        if "requests" in raw:
            return raw
        if raw.get("approval_type"):
            return {"requests": [{"approval_type": raw["approval_type"], "fields": raw.get("fields", {}), "missing": raw.get("missing", [])}], "unclear": raw.get("unclear", "")}
        return {"requests": [], "unclear": raw.get("unclear", "无法识别审批类型。")}
    except Exception as e:
        logger.exception("AI分析失败: %s", e)
        return {"requests": [], "unclear": "AI助手暂时无法响应，请稍后再试。"}


_FIELDLIST_ALIAS = {
    "名称": ["name", "item_name", "物品名称", "物品", "品名"],
    "规格": ["spec", "specification", "model", "规格型号", "型号"],
    "数量": ["quantity", "qty", "count", "num"],
    "金额": ["amount", "price", "cost", "单价", "总价", "费用"],
    "是否有库存": ["has_stock", "in_stock", "库存", "stock"],
    # 用印申请单表格结构
    "文件名称": ["document_name", "doc_name", "文件名"],
    "文件类型": ["document_type", "doc_type", "类型"],
    "用印公司": ["company", "公司"],
    "印章类型": ["seal_type", "seal", "印章"],
    "用印事由": ["reason", "事由", "用途"],
    "盖章形式": ["usage_method", "纸质章/电子章/外带印章"],
    "文件数量": ["document_count", "数量"],
}


def _match_sub_field(sf_name, item):
    """根据子字段名称从 AI 输出的 dict 中匹配值"""
    if sf_name in item:
        return str(item[sf_name])
    for alias_name, aliases in _FIELDLIST_ALIAS.items():
        if sf_name == alias_name or sf_name in aliases:
            for key in [alias_name] + aliases:
                if key in item:
                    return str(item[key])
    for key in item:
        if sf_name and (sf_name in key or key in sf_name):
            return str(item[key])
    return ""


def _format_field_value(logical_key, raw_value, field_type, field_info=None):
    """根据控件类型格式化值。fieldList 需传二维数组 [[{id,type,value},...]]。"""
    if field_type == "fieldList":
        sub_fields = (field_info or {}).get("sub_fields", [])
        if isinstance(raw_value, list) and raw_value:
            if sub_fields:
                rows = []
                for item in raw_value:
                    if isinstance(item, dict):
                        row = []
                        for sf in sub_fields:
                            sf_name = sf.get("name", "")
                            sf_type = sf.get("type", "input")
                            # 附件类型在行内时，value 需为 list，不能转 str
                            if sf_type in ("attachmentV2", "attach", "attachV2") and sf_name in item:
                                val = item[sf_name]
                                if isinstance(val, list):
                                    pass  # 保持 list
                                else:
                                    val = [val] if val else []
                            else:
                                val = _match_sub_field(sf_name, item)
                            # 采购申请等：单选框子字段（如「是否有库存」）发起人不填，提交时需给有效值
                            if sf_type in ("radioV2", "radio") and not val:
                                opts = sf.get("options", [])
                                if opts and isinstance(opts, list) and opts[0]:
                                    opt = opts[0]
                                    if isinstance(opt, dict):
                                        val = opt.get("value") or opt.get("key", "") or opt.get("text", "")
                            row.append({"id": sf["id"], "type": sf_type, "value": val})
                        rows.append(row)
                    elif isinstance(item, list):
                        rows.append(item)
                return rows if rows else []
            if all(isinstance(r, list) for r in raw_value):
                return raw_value
        if isinstance(raw_value, str) and raw_value and sub_fields:
            row = []
            for i, sf in enumerate(sub_fields):
                val = raw_value if i == 0 else ""
                row.append({"id": sf["id"], "type": sf.get("type", "input"), "value": val})
            return [row]
        return []
    if logical_key in DATE_FIELDS and raw_value:
        return f"{raw_value}T00:00:00+08:00" if "T" not in str(raw_value) else str(raw_value)
    if field_type == "checkboxV2" and isinstance(raw_value, list):
        return raw_value
    return str(raw_value) if raw_value else ""


def _to_rfc3339(date_val):
    """将日期值转为 RFC3339 格式（dateInterval 需要）"""
    s = str(date_val).strip()
    if len(s) == 10:
        return f"{s}T00:00:00+08:00"
    if "T" in s and "+" not in s:
        return f"{s}+08:00"
    if "T" not in s and " " in s:
        return s.replace(" ", "T") + "+08:00"
    return s


def build_form(approval_type, fields, token, file_codes=None):
    """根据审批类型构建表单数据。file_codes: {field_id: [code1, ...]} 附件字段。"""
    approval_code = APPROVAL_CODES[approval_type]
    cached = get_form_fields(approval_type, approval_code, token)
    if not cached:
        logger.warning("无法获取 %s 的字段结构", approval_type)
        return None

    file_codes = file_codes or {}
    fallback = FIELD_ID_FALLBACK.get(approval_type, {})
    name_to_key = {v: k for k, v in FIELD_LABELS.items()}
    name_to_key.update({k: k for k in FIELD_LABELS})

    used_keys = set()
    form_list = []
    for field_id, field_info in cached.items():
        field_type = field_info.get("type", "input")
        field_name = field_info.get("name", "")
        if field_type in ("description",):
            continue
        if field_type in ("attach", "attachV2", "image", "imageV2", "attachmentV2", "attachment", "file"):
            files = file_codes.get(field_id)
            if not files and file_codes:
                # 用印申请单等：传入的 file_codes 可能用固定 ID，实际表单的附件字段 ID 可能不同
                files = next(iter(file_codes.values()), None)
            if files:
                # 飞书附件字段 value 需为文件 code 数组
                form_list.append({"id": field_id, "type": field_type, "value": files if isinstance(files, list) else [files]})
            continue

        if field_type == "dateInterval":
            start_val = fields.get("start_date") or fields.get("开始日期") or ""
            end_val = fields.get("end_date") or fields.get("结束日期") or ""
            if not start_val:
                start_val = str(datetime.date.today())
            if not end_val:
                end_val = start_val
            used_keys.update(["start_date", "end_date", "开始日期", "结束日期"])
            form_list.append({
                "id": field_id,
                "type": "dateInterval",
                "value": {
                    "start": _to_rfc3339(start_val),
                    "end": _to_rfc3339(end_val),
                    "interval": 1.0
                }
            })
            continue

        logical_key = FIELD_LABELS_REVERSE.get(field_name) or name_to_key.get(field_name)
        if not logical_key:
            for k, v in fallback.items():
                if v == field_id:
                    logical_key = k
                    break
        if not logical_key:
            logical_key = field_name

        raw = fields.get(logical_key) or fields.get(field_id) or fields.get(field_name) or ""
        if not raw and field_type == "amount":
            raw = fields.get("amount") or fields.get("金额") or ""
        if not raw and field_name in ("开票金额", "发票金额"):
            raw = fields.get("amount") or ""
        # 开票申请单：客户/开票名称、税务登记证号/社会统一信用代码 等表单字段名兜底
        if not raw and field_name in ("客户/开票名称", "购方名称", "开票抬头"):
            raw = fields.get("buyer_name") or ""
        if not raw and field_name in ("税务登记证号/社会统一信用代码", "购方税号", "税务登记证号", "社会统一信用代码"):
            raw = fields.get("tax_id") or ""
        if raw:
            used_keys.add(logical_key)
        if not raw and logical_key == "reason":
            raw = "审批申请"
        if field_type in ("radioV2", "radio"):
            opts = field_info.get("options", [])
            if opts and isinstance(opts, list):
                raw_str = str(raw).strip()
                # 用印申请单「律师是否已审核」：表单选项为「已审核/未审核」，用户说「是/否」时需映射
                if logical_key == "lawyer_reviewed" and raw_str in ("是", "yes"):
                    raw_str = "已审核"
                elif logical_key == "lawyer_reviewed" and raw_str in ("否", "no"):
                    raw_str = "未审核"
                matched = False
                for opt in opts:
                    if isinstance(opt, dict):
                        opt_val = opt.get("value") or opt.get("key", "")
                        opt_text = opt.get("text", "")
                        if opt_val == raw_str or opt_text == raw_str:
                            raw = opt_val or opt_text
                            matched = True
                            break
                        if raw_str and raw_str in (opt_text, opt_val):
                            raw = opt_val or opt_text
                            matched = True
                            break
                # 开票申请单「业务类型」：无精确匹配时做关键词模糊匹配，避免误用 opts[0]（如 Right-bot）
                if not matched and logical_key == "business_type" and approval_type == "开票申请单" and raw_str:
                    for kw in ("小蓝词", "NBBOSS", "Right-bot", "right-bot"):
                        if kw.lower() in raw_str.lower():
                            for opt in opts:
                                if isinstance(opt, dict):
                                    ot = str(opt.get("text", "") or opt.get("value", ""))
                                    if kw.lower() in ot.lower():
                                        raw = opt.get("value") or opt.get("key", "") or ot
                                        matched = True
                                        break
                            break
                if not matched:
                    raw = (opts[0].get("value") or opts[0].get("key", "")) if isinstance(opts[0], dict) else ""
        if field_type == "checkboxV2":
            opts = field_info.get("options", [])
            raw_list = raw if isinstance(raw, list) else ([raw] if raw else [])
            resolved = []
            for r in raw_list:
                r_str = str(r).strip()
                if not r_str:
                    continue
                for opt in (opts or []):
                    if isinstance(opt, dict):
                        ov = opt.get("value") or opt.get("key", "")
                        ot = str(opt.get("text", ""))
                        if r_str == ov or r_str == ot or r_str in (ov, ot):
                            resolved.append(ov or r_str)
                            break
                        if r_str in ot or ot in r_str:
                            resolved.append(ov or r_str)
                            break
            raw = resolved
        # fieldList 无 sub_fields 时使用配置的 fallback（如采购费用明细）
        if field_type == "fieldList" and not (field_info.get("sub_fields")):
            fallback_subs = (FIELDLIST_SUBFIELDS_FALLBACK.get(approval_type) or {}).get(logical_key)
            if fallback_subs:
                field_info = {**field_info, "sub_fields": fallback_subs}
        value = _format_field_value(logical_key, raw, field_type, field_info)
        ftype = field_type if field_type in ("input", "textarea", "date", "number", "amount", "radioV2", "fieldList", "checkboxV2") else "input"
        if field_type in ("input", "textarea") and value == "":
            value = "无"
        if field_type == "amount":
            try:
                value = float(str(raw).replace(",", "").replace(" ", "")) if raw else 0.0
            except (ValueError, TypeError):
                value = 0.0
        if logical_key in DATE_FIELDS and raw:
            ftype = "date"
            value = _format_field_value(logical_key, raw, "date")

        form_list.append({"id": field_id, "type": ftype, "value": value})

    unused_texts = [str(v) for k, v in fields.items() if k not in used_keys and v]
    if unused_texts:
        for item in form_list:
            if item.get("type") == "textarea" and not item.get("value"):
                item["value"] = "；".join(unused_texts)
                break

    return form_list


def _value_to_text(val, options):
    """将 radioV2/radio/checkboxV2 的 value 转为可读的 text，支持 value/key/id 匹配及 text/label/name 显示"""
    if not options or not val:
        return val
    for opt in options:
        if isinstance(opt, dict):
            opt_val = opt.get("value") or opt.get("key") or opt.get("id", "")
            if str(opt_val) == str(val):
                return opt.get("text") or opt.get("label") or opt.get("name", val)
    return val


def _checkbox_values_to_text(vals, options):
    """将 checkboxV2 的 value 列表转为可读的 text 列表"""
    if not options or not vals:
        return vals
    result = []
    for v in (vals if isinstance(vals, list) else [vals]):
        t = _value_to_text(v, options)
        result.append(t if t else v)
    return result


def _form_summary(form_list, cached, approval_type=None):
    """根据实际提交的表单和缓存的字段名生成摘要，radioV2 显示 text 而非 value，附件显示「已上传」"""
    lines = []
    for item in form_list:
        fid = item.get("id", "")
        info = cached.get(fid, {})
        name = info.get("name", fid)
        ftype = item.get("type", "")
        if ftype == "dateInterval":
            val = item.get("value", {})
            if isinstance(val, dict):
                s = str(val.get("start", "")).split("T")[0]
                e = str(val.get("end", "")).split("T")[0]
                lines.append(f"· {name}: {s} 至 {e}")
        elif ftype in ("attach", "attachV2", "image", "imageV2", "attachmentV2", "attachment"):
            continue
        elif ftype in ("radioV2", "radio"):
            val = item.get("value", "")
            if val:
                display = _value_to_text(val, info.get("options", []))
                lines.append(f"· {name}: {display}")
        elif ftype == "fieldList":
            val = item.get("value", [])
            if val and isinstance(val, list) and isinstance(val[0], list):
                sub_fields = info.get("sub_fields", [])
                if not sub_fields and approval_type:
                    rev_fallback = {v: k for k, v in (FIELD_ID_FALLBACK.get(approval_type) or {}).items()}
                    logical_key = rev_fallback.get(fid)
                    sub_fields = (FIELDLIST_SUBFIELDS_FALLBACK.get(approval_type) or {}).get(logical_key, [])
                sf_map = {sf.get("id"): sf for sf in sub_fields if sf.get("id")} if sub_fields else {}
                approval_code = APPROVAL_CODES.get(approval_type, "") if approval_type else ""
                lines.append(f"· {name}:")
                for i, row in enumerate(val):
                    parts = []
                    for c in row:
                        if not isinstance(c, dict):
                            continue
                        cid = c.get("id", "")
                        cval = c.get("value")
                        ctype = c.get("type", "input")
                        sf_info = sf_map.get(cid, {})
                        if sf_info:
                            ctype = sf_info.get("type", ctype)
                        if ctype in ("radioV2", "radio"):
                            opts = sf_info.get("options", [])
                            if not opts and approval_type and approval_code and cid:
                                opts = get_sub_field_options(approval_type, cid, approval_code, get_token)
                            display = _value_to_text(cval, opts) if cval else ""
                        elif ctype in ("attach", "attachV2", "attachmentV2", "attachment", "image", "imageV2"):
                            display = "已上传" if cval else ""
                        else:
                            display = str(cval) if cval else ""
                        if display:
                            parts.append(display)
                    if parts:
                        lines.append(f"  {i+1}. {', '.join(parts)}")
        elif ftype == "checkboxV2":
            val = item.get("value", [])
            if val:
                display = _checkbox_values_to_text(val, info.get("options", []))
                lines.append(f"· {name}: {', '.join(str(d) for d in display)}")
        else:
            val = item.get("value", "")
            if val:
                lines.append(f"· {name}: {val}")
    return "\n".join(lines)


def _infer_purchase_type_from_cost_detail(cost_detail):
    """根据采购物品推断采购类别。返回推断的类别文本，失败返回空。"""
    if not cost_detail or not isinstance(cost_detail, list):
        return ""
    items_desc = []
    for item in cost_detail[:5]:
        if isinstance(item, dict):
            name = item.get("名称") or item.get("name") or item.get("物品") or ""
            spec = item.get("规格") or item.get("spec") or ""
            items_desc.append(f"{name} {spec}".strip() or "未知")
        else:
            items_desc.append(str(item)[:50])
    if not items_desc:
        return ""
    text = "、".join(items_desc)
    prompt = (
        f"根据采购物品推断采购类别。\n物品：{text}\n"
        f"常见类别：办公用品、设备、耗材、原材料等。"
        f"只返回一个最合适的类别词，不要其他内容。"
    )
    try:
        res = call_deepseek_with_retry([{"role": "user", "content": prompt}], timeout=10, max_retries=1, max_tokens=20)
        out = res.json()["choices"][0]["message"]["content"].strip()
        return out.split("\n")[0].strip() if out else ""
    except Exception as e:
        logger.warning("推断采购类别失败: %s", e)
        return ""


def create_approval(user_id, approval_type, fields, file_codes=None):
    approval_code = APPROVAL_CODES[approval_type]
    token = get_token()

    fields = dict(fields)
    if approval_type == "采购申请" and not fields.get("purchase_type") and fields.get("cost_detail"):
        inferred = _infer_purchase_type_from_cost_detail(fields["cost_detail"])
        if inferred:
            fields["purchase_type"] = inferred

    cached = get_form_fields(approval_type, approval_code, token)
    form_list = build_form(approval_type, fields, token, file_codes=file_codes)
    if form_list is None:
        return False, "无法构建表单，请检查审批字段配置", {}, ""

    form_data = json.dumps(form_list, ensure_ascii=False)
    logger.info("提交表单[%s]: %s", approval_type, form_data)

    summary = _form_summary(form_list, cached or {}, approval_type)

    res = httpx.post(
        "https://open.feishu.cn/open-apis/approval/v4/instances",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "approval_code": approval_code,
            "user_id": user_id,
            "form": form_data
        },
        timeout=15
    )
    data = res.json()
    logger.info("创建审批响应: %s", data)

    success = data.get("code") == 0
    msg = data.get("msg", "")

    if not success:
        invalidate_cache(approval_type)

    return success, msg, data.get("data", {}), summary


def format_fields_summary(fields, approval_type=None):
    """按工单字段顺序展示，无 FIELD_ORDER 时按 fields 原有顺序"""
    order = FIELD_ORDER.get(approval_type) if approval_type else None
    if order:
        items = [(k, fields.get(k, "")) for k in order if k in fields]
        for k, v in fields.items():
            if k not in order:
                items.append((k, v))
    else:
        items = list(fields.items())
    lines = []
    for k, v in items:
        if v == "" and k != "reason":
            continue
        label = FIELD_LABELS.get(k, k)
        if isinstance(v, list):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    parts = [f"{ik}:{iv}" for ik, iv in item.items() if iv]
                    lines.append(f"  {i+1}. {', '.join(parts)}")
                else:
                    lines.append(f"  {i+1}. {item}")
            if lines:
                lines.insert(len(lines) - len(v), f"· {label}:")
        else:
            lines.append(f"· {label}: {v}")
    return "\n".join(lines)


def _on_message_read(_data):
    """消息已读事件，无需处理，仅避免 processor not found 报错"""
    pass


PENDING_SEAL = {}
# 用印申请单：用户首次消息中已提取的字段，等收到文件后合并。结构 {open_id: {"fields": {...}, "created_at": ts}}
SEAL_INITIAL_FIELDS = {}

# 限流：open_id -> 上次消息时间
_user_last_msg = {}

# 开票申请单：需结算单+合同双附件，分步收集
PENDING_INVOICE = {}

# 工单确认：用户点击确认按钮后创建。confirm_id -> {open_id, user_id, approval_type, fields, file_codes, admin_comment, created_at}
PENDING_CONFIRM = {}
OPEN_ID_TO_CONFIRM = {}  # open_id -> confirm_id，用于汇总卡出现后用户通过对话修改字段
CONFIRM_TTL = 15 * 60  # 15 分钟

ATTACHMENT_FIELD_ID = "widget15828104903330001"

# 用印申请单选项字段（表格结构时选项在 sub_fields 内，此处作 fallback）
SEAL_OPTION_FIELDS = {
    "usage_method": "widget17334699216260001",   # 盖章形式：纸质章/电子章/外带印章
    "seal_type": "widget15754438920110001",     # 印章类型
    "document_type": "widget17334700336550001", # 文件类型
    "lawyer_reviewed": "widget17334701422160001",  # 律师是否已审核
}


# 用印「文件类型」控件「其他」选项的 value，表单选项变更时需同步更新
SEAL_DOC_TYPE_OTHER_VALUE = "mk4u3uw5-guo071d9k5m-1"


# 文件类型业务关键词 → 优先匹配的选项文本片段（结算单、合作协议等识别为业务类型，非「保密协议等特殊交办」）
SEAL_DOC_TYPE_BUSINESS_KEYWORDS = [
    ("结算单", "结算单"),
    ("对账单", "结算单"),
    ("对账", "结算单"),
    ("月结", "结算单"),
    ("合作协议", "协议"),
    ("合作框架", "框架"),
    ("合同", "合同"),
    ("框架协议", "框架"),
    ("采购合同", "合同"),
    ("服务协议", "协议"),
]


def _infer_document_type_from_name_and_reason(file_name, reason):
    """
    根据文件名和用印事由推断文件业务类型，用于兜底（当 AI 未返回 document_type 或值为 PDF/Word 等格式时）。
    返回可被 _resolve_document_type_for_seal 匹配的字符串，如「结算单」「合作协议」。
    """
    base_name = (file_name or "").rsplit(".", 1)[0]
    combined = f"{base_name} {reason or ''}"
    # 按优先级匹配：结算单/对账类优先，再协议/合同类
    for kw, _ in SEAL_DOC_TYPE_BUSINESS_KEYWORDS:
        if kw in combined:
            return kw
    return ""


def _resolve_document_type_for_seal(document_type):
    """
    将 document_type 解析为用印表单「文件类型」控件的有效选项值。
    表单选项为业务分类（如 NBBOSS代理商结算单、其他等），非文件格式。
    结算单、合作协议等识别为业务类型；保密协议等匹配「保密协议等特殊交办文件」。
    当值为 PDF/Word 等文件格式或不在选项中时，使用「其他」选项。
    """
    opts = get_sub_field_options("用印申请单", "widget17334700336550001", APPROVAL_CODES["用印申请单"], get_token)
    if not opts:
        invalidate_cache("用印申请单")  # 旧缓存可能无 sub_field options，清除后下次重试
        opts = get_sub_field_options("用印申请单", "widget17334700336550001", APPROVAL_CODES["用印申请单"], get_token)
    if not opts:
        return SEAL_DOC_TYPE_OTHER_VALUE
    raw = str(document_type or "").strip()
    for o in opts:
        if not isinstance(o, dict):
            continue
        opt_val = o.get("value") or o.get("key", "")
        opt_text = o.get("text", "")
        if raw and (raw == opt_val or raw == opt_text):
            return opt_val or opt_text
        if raw and raw in (opt_text, opt_val):
            return opt_val or opt_text
    # 业务类型模糊匹配：结算单、合作协议等 → 匹配选项中包含对应关键词的选项
    for kw_in_raw, kw_in_opt in SEAL_DOC_TYPE_BUSINESS_KEYWORDS:
        if kw_in_raw in raw:
            for o in opts:
                if not isinstance(o, dict):
                    continue
                opt_text = str(o.get("text", "") or o.get("value", ""))
                if kw_in_opt in opt_text and "保密" not in opt_text and "特殊交办" not in opt_text:
                    return o.get("value") or o.get("key", "") or opt_text
    # 保密协议等 → 匹配「保密协议等特殊交办文件」
    if "保密" in raw or "特殊交办" in raw:
        for o in opts:
            if isinstance(o, dict):
                t = str(o.get("text", "") or o.get("value", ""))
                if "保密" in t or "特殊交办" in t:
                    return o.get("value") or o.get("key", "") or t
    # 文件格式（PDF、Word 等）或无效值：使用「其他」选项
    for o in opts:
        if isinstance(o, dict):
            t = str(o.get("text", "") or o.get("value", ""))
            if "其他" in t:
                return o.get("value") or o.get("key", "") or t
    last = (opts[-1].get("value") or opts[-1].get("key", "")) if opts and isinstance(opts[-1], dict) else ""
    return last or SEAL_DOC_TYPE_OTHER_VALUE


def _resolve_radio_option_for_seal(field_id, raw_value, default_first=True):
    """
    将文本值解析为用印表单 radioV2 控件的有效 option value。
    用于 seal_type、usage_method 等 fieldList 子字段。
    """
    opts = get_sub_field_options("用印申请单", field_id, APPROVAL_CODES["用印申请单"], get_token)
    if not opts:
        invalidate_cache("用印申请单")
        opts = get_sub_field_options("用印申请单", field_id, APPROVAL_CODES["用印申请单"], get_token)
    if not opts:
        return ""
    raw = str(raw_value or "").strip()
    for o in opts:
        if not isinstance(o, dict):
            continue
        opt_val = o.get("value") or o.get("key", "")
        opt_text = o.get("text", "")
        if raw and (raw == opt_val or raw == opt_text):
            return opt_val or opt_text
        if raw and raw in (opt_text, opt_val):
            return opt_val or opt_text
    if default_first and opts and isinstance(opts[0], dict):
        return opts[0].get("value") or opts[0].get("key", "")
    return ""


def _get_seal_form_options():
    """从工单模版读取用印申请单的选项，返回 {逻辑键: [选项文本列表]}"""
    token = get_token()
    cached = get_form_fields("用印申请单", APPROVAL_CODES["用印申请单"], token)
    if not cached:
        return {}
    result = {}
    for logical_key, field_id in SEAL_OPTION_FIELDS.items():
        info = cached.get(field_id, {})
        opts = info.get("options", [])
        if isinstance(opts, str):
            try:
                opts = json.loads(opts) if opts else []
            except json.JSONDecodeError:
                opts = []
        texts = []
        for o in opts:
            if isinstance(o, dict):
                t = o.get("text") or o.get("value", "")
                if t:
                    texts.append(str(t))
        if texts:
            result[logical_key] = texts
    return result


# 开票申请单附件字段 ID（6706BE14 表单仅一个「上传开票证明文件」，结算单+合同合并上传）
INVOICE_ATTACHMENT_FIELD_ID = "widget16457795866320001"
# 开票申请单选项字段（发票类型、开票项目）
INVOICE_OPTION_FIELDS = {
    "invoice_type": "widget16457794296140001",
    "invoice_items": "widget17660282371600001",
}


def _get_invoice_form_options():
    """从工单模版读取开票申请单的发票类型、开票项目选项"""
    token = get_token()
    cached = get_form_fields("开票申请单", APPROVAL_CODES.get("开票申请单", ""), token)
    if not cached:
        return {}
    result = {}
    for logical_key, field_id in INVOICE_OPTION_FIELDS.items():
        info = cached.get(field_id, {})
        opts = info.get("options", [])
        if isinstance(opts, str):
            try:
                opts = json.loads(opts) if opts else []
            except json.JSONDecodeError:
                opts = []
        texts = []
        for o in opts:
            if isinstance(o, dict):
                t = o.get("text") or o.get("value", "")
                if t:
                    texts.append(str(t))
        if texts:
            result[logical_key] = texts
    return result


def _build_invoice_options_card(doc_fields, summary_prefix=""):
    """构建开票选项卡片：发票类型、开票项目，用印式选项按钮"""
    opts = _get_invoice_form_options()
    invoice_type_opts = opts.get("invoice_type") or ["增值税专用发票", "增值税普通发票", "普通发票"]
    invoice_items_opts = opts.get("invoice_items") or ["技术服务费", "广告费", "咨询费", "其他"]
    selected_type = str(doc_fields.get("invoice_type", "")).strip()
    selected_items = str(doc_fields.get("invoice_items", "")).strip()
    text = (
        f"{summary_prefix}\n\n请点击下方选项完成补充：\n\n"
        f"**发票类型**（必选）"
    )
    type_btns = [
        {"tag": "button", "text": {"tag": "plain_text", "content": v},
         "type": "primary" if v == selected_type else "default",
         "behaviors": [{"type": "callback", "value": {"action": "invoice_option", "field": "invoice_type", "value": v}}]}
        for v in invoice_type_opts
    ]
    items_btns = [
        {"tag": "button", "text": {"tag": "plain_text", "content": v},
         "type": "primary" if v == selected_items else "default",
         "behaviors": [{"type": "callback", "value": {"action": "invoice_option", "field": "invoice_items", "value": v}}]}
        for v in invoice_items_opts
    ]
    submit_btn = {
        "tag": "button", "text": {"tag": "plain_text", "content": "确认"},
        "type": "primary",
        "behaviors": [{"type": "callback", "value": {"action": "invoice_submit"}}],
    }
    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": text}},
            {"tag": "action", "actions": type_btns},
            {"tag": "div", "text": {"tag": "plain_text", "content": "开票项目（必选）", "lines": 1}},
            {"tag": "action", "actions": items_btns},
            {"tag": "hr"},
            {"tag": "action", "actions": [submit_btn]},
        ],
    }


def _get_invoice_attachment_field_ids():
    """从开票申请单表单缓存中查找附件字段。新表单仅一个「上传开票证明文件」，结算单+合同合并"""
    token = get_token()
    cached = get_form_fields("开票申请单", APPROVAL_CODES.get("开票申请单", ""), token)
    if not cached:
        return {"unified": INVOICE_ATTACHMENT_FIELD_ID}
    result = {}
    for fid, info in cached.items():
        if info.get("type") in ("attach", "attachV2", "attachment", "attachmentV2", "file"):
            name = info.get("name", "")
            if "结算" in name or "结算单" in name:
                result["settlement"] = fid
            elif "合同" in name and "开票证明" not in name:
                result["contract"] = fid
            elif "开票证明" in name or "上传开票" in name:
                result["unified"] = fid
    if not result:
        result["unified"] = INVOICE_ATTACHMENT_FIELD_ID
    return result


def _handle_file_message(open_id, user_id, message_id, content_json, files_list=None):
    """处理文件消息：下载文件→上传审批附件→提取文件名→合并首次消息与文件名推断→等用户补充其余字段。
    files_list: 可选，多文件时传入 [{"message_id", "content_json", "file_name"}, ...]，单文件时用 message_id/content_json
    多文件时采用排队模式：先出第1张卡，选完点「下一份」出第2张，全部选完后出「提交工单」按钮。"""
    if files_list and len(files_list) > 1:
        # 多文件排队模式：先出第1张卡，选完「下一份」出第2张，全部选完出「提交工单」
        opts = _get_seal_form_options()
        seal_opts = opts.get("seal_type", ["公章", "合同章", "法人章", "财务章"])
        extractor = get_file_extractor("用印申请单")
        with _state_lock:
            data = SEAL_INITIAL_FIELDS.pop(open_id, {})
        initial_fields = data.get("fields", data) if isinstance(data, dict) and "fields" in data else (data if isinstance(data, dict) else {})
        items = []
        first_ai = {}
        for i, f in enumerate(files_list):
            msg_id = f.get("message_id")
            cj = f.get("content_json", {})
            fname = f.get("file_name", cj.get("file_name", "未知文件"))
            fkey = cj.get("file_key") or cj.get("image_key", "")
            resource_type = f.get("resource_type", "file")
            if not fkey:
                send_message(open_id, f"无法获取文件「{fname}」，请重新发送。", use_red=True)
                return
            send_message(open_id, f"正在处理文件「{fname}」（{i+1}/{len(files_list)}），请稍候...")
            file_content, dl_err = download_message_file(msg_id, fkey, resource_type)
            if not file_content:
                send_message(open_id, f"文件「{fname}」下载失败，请重新发送。{dl_err or ''}".strip(), use_red=True)
                return
            file_code, upload_err = upload_approval_file(fname, file_content)
            if not file_code:
                send_message(open_id, f"文件「{fname}」上传失败。{upload_err or ''}".strip(), use_red=True)
                return
            doc_name = fname.rsplit(".", 1)[0] if "." in fname else fname
            ext = (fname.rsplit(".", 1)[-1] or "").lower()
            doc_type = {"docx": "Word文档", "doc": "Word文档", "pdf": "PDF"}.get(ext, ext.upper() if ext else "")
            doc_fields = {"document_name": doc_name, "document_count": "1", "document_type": doc_type}
            # 每个文件单独提取：用印公司、印章类型、用印事由、文件类型等，避免后续文件误用首文件的值
            if extractor and DEEPSEEK_API_KEY:
                ai_fields = extractor(file_content, fname, {"company": opts.get("company"), "seal_type": seal_opts}, get_token) or {}
                doc_fields.update(ai_fields)
            # 文件类型兜底：若 AI 未返回或为 PDF/Word 等格式，根据文件名和事由推断业务类型
            dt = doc_fields.get("document_type", "")
            if not dt or dt.lower() in ("pdf", "word", "word文档", "doc", "docx"):
                inferred = _infer_document_type_from_name_and_reason(fname, doc_fields.get("reason", ""))
                if inferred:
                    doc_fields["document_type"] = inferred
            for k, v in (initial_fields or {}).items():
                if v and str(v).strip() and k not in ("usage_method",):
                    doc_fields[k] = str(v).strip()
            doc_fields.pop("usage_method", None)
            items.append({"file_name": fname, "file_code": file_code, "doc_fields": doc_fields})
        with _state_lock:
            PENDING_SEAL_QUEUE[open_id] = {
                "items": items,
                "current_index": 0,
                "selections": [],
                "user_id": user_id,
                "created_at": time.time(),
            }
            if open_id in SEAL_INITIAL_FIELDS:
                del SEAL_INITIAL_FIELDS[open_id]
        send_message(open_id, f"已接收 {len(items)} 个文件，请依次为每份文件选择选项。", use_red=True)
        _send_seal_queue_card(open_id, user_id, PENDING_SEAL_QUEUE[open_id])
        return

    if files_list and len(files_list) == 1:
        f = files_list[0]
        message_id = f.get("message_id")
        content_json = f.get("content_json", {})
        file_name = f.get("file_name", content_json.get("file_name", "未知文件"))
    # 单文件处理（来自 files_list[0] 或 直接上传的 message_id/content_json）
    if not files_list:
        file_name = content_json.get("file_name", "未知文件")
    file_key = content_json.get("file_key") or content_json.get("image_key", "")
    resource_type = (files_list[0].get("resource_type", "file") if files_list and len(files_list) == 1 else "file")
    if not file_key:
        send_message(open_id, "无法获取文件，请重新发送。", use_red=True)
        return
    send_message(open_id, f"正在处理文件「{file_name}」，请稍候...")
    file_content, dl_err = download_message_file(message_id, file_key, resource_type)
    if not file_content:
        err_detail = f"（{dl_err}）" if dl_err else ""
        send_message(open_id, f"文件下载失败，请重新发送。{err_detail}".strip(), use_red=True)
        return
    logger.info("用印文件: 已下载 %s, 大小=%d bytes", file_name, len(file_content))
    file_code, upload_err = upload_approval_file(file_name, file_content)
    if not file_code:
        err_detail = f"（{upload_err}）" if upload_err else ""
        send_message(open_id, f"文件上传失败，请重新发送文件。附件上传成功后才能继续创建工单。{err_detail}", use_red=True)
        return
    file_codes = [file_code]
    doc_name = file_name.rsplit(".", 1)[0] if "." in file_name else file_name
    doc_count = "1"
    ext = (file_name.rsplit(".", 1)[-1] or "").lower()
    doc_type_map = {"docx": "Word文档", "doc": "Word文档", "pdf": "PDF"}
    doc_type = doc_type_map.get(ext, ext.upper() if ext else "")
    doc_fields = {"document_name": doc_name, "document_count": doc_count, "document_type": doc_type}

    # 以下为共用逻辑（单文件与多文件）

    opts = _get_seal_form_options()
    company_opts = opts.get("company", [])
    seal_opts = opts.get("seal_type", ["公章", "合同章", "法人章", "财务章"])
    usage_opts = opts.get("usage_method", ["纸质章", "电子章", "外带印章"])
    lawyer_opts = opts.get("lawyer_reviewed", ["是", "否"])

    # 合并：文件基础信息 + 文件内容 AI 识别（使用通用提取器，含 OCR，适用所有有附件识别需求的工单）+ 首次消息已提取字段（后者优先）
    extractor = get_file_extractor("用印申请单")
    if not extractor:
        logger.warning("用印提取: 未找到 extractor，请检查 approval_types 注册")
    if not DEEPSEEK_API_KEY:
        logger.warning("用印提取: DEEPSEEK_API_KEY 未配置，无法调用 AI 识别")
    ai_fields = extractor(file_content, file_name, {"company": company_opts or None, "seal_type": seal_opts or None}, get_token) if extractor else {}
    if ai_fields:
        logger.info("用印文件识别结果: %s", ai_fields)
    elif extractor:
        logger.warning("用印文件识别: 未识别到字段，文件名=%s", file_name)
    with _state_lock:
        data = SEAL_INITIAL_FIELDS.pop(open_id, {})
    initial_fields = data.get("fields", data) if isinstance(data, dict) and "fields" in data else (data if isinstance(data, dict) else {})
    doc_fields = {**doc_fields, **ai_fields}
    # 文件类型兜底：若 AI 未返回或为 PDF/Word 等格式，根据文件名和事由推断业务类型
    dt = doc_fields.get("document_type", "")
    if not dt or dt.lower() in ("pdf", "word", "word文档", "doc", "docx"):
        inferred = _infer_document_type_from_name_and_reason(file_name, doc_fields.get("reason", ""))
        if inferred:
            doc_fields["document_type"] = inferred
    # 仅当首次消息有有效值时才覆盖，避免空值覆盖 AI 识别结果
    # company、seal_type 若已由文件 AI 识别，则仅当首次消息提供的是有效选项值时才覆盖，防止占位符覆盖
    for k, v in initial_fields.items():
        if not v or not str(v).strip():
            continue
        v = str(v).strip()
        if k == "company" and ai_fields.get("company"):
            if company_opts and v in company_opts:
                doc_fields[k] = v
            # 否则保留 ai_fields 的识别结果，不覆盖
        elif k == "seal_type" and ai_fields.get("seal_type"):
            if seal_opts and v in seal_opts:
                doc_fields[k] = v
        elif k == "usage_method":
            pass  # 不采纳 initial_fields 中的 usage_method，需用户明确回复
        else:
            doc_fields[k] = v
    # 不在此处默认 usage_method，需用户明确选择盖章形式

    with _state_lock:
        CONVERSATIONS.setdefault(open_id, [])
        CONVERSATIONS[open_id].append({
            "role": "assistant",
            "content": f"[已接收文件] 文件名称={doc_name}"
        })

    def _valid(k, v):
        if k == "lawyer_reviewed":
            return v and str(v).strip() in lawyer_opts
        if k == "usage_method":
            return v and str(v).strip() in usage_opts
        return bool(v)
    # 表单无「用印公司」字段，不要求必填；reason 会合并到 document_name
    missing = [k for k in ["seal_type", "reason", "lawyer_reviewed", "usage_method"] if not _valid(k, doc_fields.get(k))]
    if missing and ai_fields and "seal_type" in missing:
        logger.warning("用印合并异常: AI已识别 seal_type=%r 但仍被列为缺失，initial_fields=%r",
                       ai_fields.get("seal_type"), initial_fields)
    if not missing:
        # 全部可推断，直接创建工单
        _do_create_seal(open_id, user_id, doc_fields, file_codes)
        return

    with _state_lock:
        PENDING_SEAL[open_id] = {
            "doc_fields": doc_fields,
            "file_codes": file_codes,
            "user_id": user_id,
            "file_name": file_name,
            "created_at": time.time(),
        }

    # 缺律师是否已审核或盖章形式时，发送选项卡片（点击选择，非文字回复）。需在事件订阅中勾选「接收消息卡片回调」
    if "lawyer_reviewed" in missing or "usage_method" in missing:
        doc_fields.pop("usage_method", None)  # 强制用户通过卡片点击选择
        send_seal_options_card(open_id, user_id, doc_fields, file_codes, file_name)
        other_missing = [m for m in missing if m not in ("lawyer_reviewed", "usage_method")]
        if other_missing:
            labels = {"company": "用印公司", "seal_type": "印章类型", "reason": "文件用途/用印事由"}
            hint_map = {
                "company": f"{'、'.join(company_opts) if company_opts else '请输入'}",
                "seal_type": "、".join(seal_opts),
                "reason": "（请描述）",
            }
            lines = [f"此外还缺少：{'、'.join(labels.get(m, m) for m in other_missing)}，请补充说明。"]
            for m in other_missing:
                lines.append(f"· {labels.get(m, m)}：{hint_map.get(m, '')}")
            send_message(open_id, "\n".join(lines), use_red=True)
    else:
        # 仅缺 company/seal_type/reason，无律师/盖章
        labels = {"company": "用印公司", "seal_type": "印章类型", "reason": "文件用途/用印事由"}
        hint_map = {
            "company": f"{'、'.join(company_opts) if company_opts else '请输入'}",
            "seal_type": "、".join(seal_opts),
            "reason": "（请描述）",
        }
        lines = [f"已接收文件：{file_name}", f"· 文件名称: {doc_name}", "", "请补充以下信息（一条消息说完即可）："]
        for k in missing:
            lines.append(f"· {labels.get(k, k)}：{hint_map.get(k, '')}")
        send_message(open_id, "\n".join(lines), use_red=True)


def _do_create_seal_multi(open_id, user_id, items, selections):
    """多文件排队完成后，合并为一张工单。items=[{file_name, file_code, doc_fields}, ...], selections=[{lawyer_reviewed, usage_method, document_count}, ...]"""
    if not items or len(items) != len(selections):
        send_message(open_id, "数据异常，请重新发起用印申请单。", use_red=True)
        return
    # 每行仅用该文件自身的 doc_fields + selection，避免后续文件误用首文件的值
    rows = []
    for i, (item, sel) in enumerate(zip(items, selections)):
        df = {**item["doc_fields"], **sel}
        resolved_usage = _resolve_radio_option_for_seal("widget17334699216260001", sel.get("usage_method", "纸质章"), default_first=True)
        resolved_seal = _resolve_radio_option_for_seal("widget15754438920110001", df.get("seal_type", ""), default_first=True)
        lawyer_val = "已审核" if str(sel.get("lawyer_reviewed", "")).strip() in ("是", "yes", "已审核") else "未审核"
        resolved_lawyer = _resolve_radio_option_for_seal("widget17334701422160001", lawyer_val, default_first=True)
        resolved_doc_type = _resolve_document_type_for_seal(df.get("document_type", ""))
        usage_val = resolved_usage or _resolve_radio_option_for_seal("widget17334699216260001", "纸质章", default_first=True)
        rows.append({
            "文件名称": df.get("document_name", item["file_name"].rsplit(".", 1)[0] if "." in item["file_name"] else item["file_name"]),
            "用印事由": df.get("reason", ""),
            "文件类型": resolved_doc_type,
            "印章类型": resolved_seal,
            "盖章形式": usage_val or "纸质章",
            "文件数量": sel.get("document_count", "1"),
            "律师是否已审核": resolved_lawyer or lawyer_val,
            "上传用章文件": [item["file_code"]],
        })
    all_fields = {"seal_detail": rows}
    all_fields.setdefault("document_name", "、".join(r["文件名称"] for r in rows[:3]) + (" 等" if len(rows) > 3 else ""))
    # 工单级字段（若表单需要）：用首文件的 company
    if items[0]["doc_fields"].get("company"):
        all_fields["company"] = items[0]["doc_fields"]["company"]
    # 附件已在每行 上传用章文件 中，无需单独传 file_codes
    success, msg, resp_data, summary = create_approval(user_id, "用印申请单", all_fields, file_codes=None)
    with _state_lock:
        if open_id in PENDING_SEAL_QUEUE:
            del PENDING_SEAL_QUEUE[open_id]
        if open_id in CONVERSATIONS:
            CONVERSATIONS[open_id] = []
    if success:
        instance_code = resp_data.get("instance_code", "")
        if instance_code:
            link = f"https://applink.feishu.cn/client/approval?instanceCode={instance_code}"
            send_card_message(open_id, f"工单已创建（共 {len(items)} 份文件），点击下方按钮查看：", link, "查看工单", use_desktop_link=True)
        else:
            send_message(open_id, f"· 用印申请单：✅ 已提交\n{summary}")
    else:
        send_message(open_id, f"提交失败：{msg}", use_red=True)


def _do_create_seal(open_id, user_id, all_fields, file_codes=None, direct_create=False):
    """用印申请单字段齐全时，发送确认卡片，用户点击确认后创建工单。direct_create=True 时直接创建不弹确认卡。表单为 fieldList 表格结构。"""
    all_fields = dict(all_fields)
    all_fields.setdefault("usage_method", "纸质章")
    all_fields.setdefault("document_count", "1")
    codes = file_codes if isinstance(file_codes, list) else ([file_codes] if file_codes else [])
    # 盖章形式、印章类型：需解析为表单 option value（飞书要求传 value 非 text）
    seal_form_type = _resolve_radio_option_for_seal(
        "widget17334699216260001", all_fields.get("usage_method", "纸质章"), default_first=True
    )
    if not seal_form_type:
        seal_form_type = _resolve_radio_option_for_seal("widget17334699216260001", "纸质章", default_first=True)
    resolved_seal_type = _resolve_radio_option_for_seal("widget15754438920110001", all_fields.get("seal_type", ""), default_first=True)
    lawyer_raw = str(all_fields.get("lawyer_reviewed", "")).strip()
    lawyer_val = "已审核" if lawyer_raw in ("是", "yes", "已审核") else "未审核"
    resolved_lawyer = _resolve_radio_option_for_seal("widget17334701422160001", lawyer_val, default_first=True)
    resolved_doc_type = _resolve_document_type_for_seal(all_fields.get("document_type", ""))
    all_fields["seal_detail"] = [{
        "文件名称": all_fields.get("document_name", ""),
        "用印事由": all_fields.get("reason", ""),
        "文件类型": resolved_doc_type,
        "印章类型": resolved_seal_type,
        "盖章形式": seal_form_type,
        "文件数量": all_fields.get("document_count", "1"),
        "律师是否已审核": resolved_lawyer or lawyer_val,
        "上传用章文件": codes,
    }]
    with _state_lock:
        if open_id in PENDING_SEAL:
            del PENDING_SEAL[open_id]

    if direct_create:
        fc = {"widget15828104903330001": codes} if codes else {}
        pre_check = run_pre_check("用印申请单", all_fields, fc, get_token)
        success, msg, resp_data, summary = create_approval(user_id, "用印申请单", all_fields, file_codes=codes)
        if success:
            with _state_lock:
                if open_id in CONVERSATIONS:
                    CONVERSATIONS[open_id] = []
            instance_code = resp_data.get("instance_code", "")
            if instance_code:
                compliant, pre_comment, pre_risks = pre_check
                set_pre_check_result(instance_code, compliant, pre_comment, pre_risks)
                if compliant:
                    add_approval_comment(instance_code, "AI已审核通过", get_token, user_id=user_id)
                elif pre_comment or pre_risks:
                    fail_comment = pre_comment
                    if pre_risks:
                        fail_comment = (fail_comment + "\n风险点：" + "；".join(pre_risks[:5])).strip()
                    add_approval_comment(instance_code, fail_comment, get_token, user_id=user_id)
                link = f"https://applink.feishu.cn/client/approval?instanceCode={instance_code}"
                send_card_message(open_id, "工单已创建，点击下方按钮查看：", link, "查看工单", use_desktop_link=True)
            else:
                send_message(open_id, f"· 用印申请单：✅ 已提交\n{summary}")
        else:
            send_message(open_id, f"提交失败：{msg}", use_red=True)
        return

    fc = {"widget15828104903330001": codes} if codes else {}
    admin_comment = get_admin_comment("用印申请单", all_fields)
    summary = format_fields_summary(all_fields, "用印申请单")
    pre_check = run_pre_check("用印申请单", all_fields, fc, get_token)
    send_confirm_card(open_id, "用印申请单", summary, admin_comment, user_id, all_fields, file_codes=fc, pre_check_result=pre_check)


def _try_complete_seal(open_id, user_id, text):
    """用户发送补充信息后，合并文件字段+用户字段，创建用印申请单"""
    with _state_lock:
        pending = PENDING_SEAL.get(open_id)
    if not pending:
        return False

    if _is_cancel_intent(text):
        with _state_lock:
            if open_id in PENDING_SEAL:
                del PENDING_SEAL[open_id]
        send_message(open_id, "已取消用印申请单，如需办理请重新发起。", use_red=True)
        return True

    doc_fields = pending["doc_fields"]
    opts = _get_seal_form_options()
    lawyer_opts = opts.get("lawyer_reviewed", ["是", "否"])
    usage_opts = opts.get("usage_method", ["纸质章", "电子章", "外带印章"])

    def _valid(k, v):
        if k == "lawyer_reviewed":
            return v and str(v).strip() in lawyer_opts
        if k == "usage_method":
            return v and str(v).strip() in usage_opts
        return bool(v)

    missing_before = [k for k in ["seal_type", "reason", "lawyer_reviewed", "usage_method"] if not _valid(k, doc_fields.get(k))]
    # 缺律师或盖章形式时，直接发送选项卡片，请用户点击选择（非文字回复）
    if "lawyer_reviewed" in missing_before or "usage_method" in missing_before:
        file_name = pending.get("file_name") or doc_fields.get("document_name", "文件")
        send_seal_options_card(open_id, user_id, doc_fields, pending.get("file_codes") or [], file_name)
        return True

    company_hint = f"（选项：{'/'.join(opts.get('company', []))}）" if opts.get("company") else ""
    seal_hint = f"（选项：{'/'.join(opts.get('seal_type', ['公章','合同章','法人章','财务章']))}）"
    usage_hint = f"（选项：{'/'.join(opts.get('usage_method', ['纸质章','电子章','外带印章']))}，默认纸质章）"
    lawyer_hint = f"（选项：{'/'.join(opts.get('lawyer_reviewed', ['是','否']))}，必填，用户必须明确选择）"

    prompt = (
        f"用户为用印申请单补充了以下信息：\n{text}\n\n"
        f"请提取并返回JSON，包含：\n"
        f"- company: 用印公司{company_hint}（用户未提及则不返回，保留文件识别结果）\n"
        f"- seal_type: 印章类型{seal_hint}（用户未提及则不返回，保留文件识别结果）\n"
        f"- reason: 文件用途/用印事由（用户未提及则不返回）\n"
        f"- usage_method: 盖章形式{usage_hint}\n"
        f"- lawyer_reviewed: 律师是否已审核{lawyer_hint}，若用户未明确则不要返回（切勿返回「缺失」等占位符）\n"
        f"- remarks: 备注(如果有)\n"
        f"只返回JSON。company、seal_type、reason 若用户未明确提及，不要返回或返回空。lawyer_reviewed 必须用户明确选择，否则不返回。"
    )
    try:
        res = call_deepseek_with_retry([{"role": "user", "content": prompt}], response_format={"type": "json_object"}, timeout=30)
        content = res.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        user_fields = json.loads(content)
    except Exception as e:
        logger.warning("解析用印补充信息失败: %s", e)
        with _state_lock:
            entry = PENDING_SEAL.get(open_id)
            if not entry:
                return True  # 已被清理，静默退出
            retry_count = entry.get("retry_count", 0) + 1
            entry["retry_count"] = retry_count
        hint = "无法理解您的输入，请重新描述用印公司、印章类型和用印事由。"
        if retry_count >= 3:
            hint += "\n（若需放弃，可回复「取消」）"
        send_message(open_id, hint, use_red=True)
        return True

    # 合并：文件识别结果优先，用户补充的有效值可覆盖；避免 AI 返回空值覆盖文件识别结果
    all_fields = dict(doc_fields)
    company_opts = opts.get("company", [])
    seal_opts = opts.get("seal_type", ["公章", "合同章", "法人章", "财务章"])
    for k, v in user_fields.items():
        if not v or not str(v).strip():
            continue
        v = str(v).strip()
        if k == "company":
            if doc_fields.get("company") and (not company_opts or v not in company_opts):
                continue  # 文件已识别且用户值无效，保留文件结果
            all_fields[k] = v
        elif k == "seal_type":
            if doc_fields.get("seal_type") and (not seal_opts or v not in seal_opts):
                continue  # 文件已识别且用户值无效，保留文件结果
            all_fields[k] = v
        elif k == "lawyer_reviewed":
            vv = "是" if str(v) in ("1", "是", "yes") else ("否" if str(v) in ("2", "否", "no") else v)
            if vv in opts.get("lawyer_reviewed", ["是", "否"]):
                all_fields[k] = vv
        elif k == "usage_method":
            if v in opts.get("usage_method", ["纸质章", "电子章", "外带印章"]):
                all_fields[k] = v
        else:
            all_fields[k] = v
    all_fields.setdefault("document_count", "1")
    missing = [k for k in ["seal_type", "reason", "lawyer_reviewed", "usage_method"] if not _valid(k, all_fields.get(k))]

    if missing:
        with _state_lock:
            entry = PENDING_SEAL.get(open_id)
            if not entry:
                return True  # 已被清理，静默退出
            retry_count = entry.get("retry_count", 0) + 1
            entry["retry_count"] = retry_count
            entry["doc_fields"] = all_fields
        if "lawyer_reviewed" in missing or "usage_method" in missing:
            all_fields.pop("usage_method", None)  # 强制用户通过卡片点击选择
            file_name = pending.get("file_name") or all_fields.get("document_name", "文件")
            send_seal_options_card(open_id, user_id, all_fields, pending.get("file_codes") or [], file_name)
        else:
            hint = f"还缺少：{'、'.join([FIELD_LABELS.get(m, m) for m in missing])}\n请补充。"
            if retry_count >= 3:
                hint += "\n（若需放弃，可回复「取消」）"
            send_message(open_id, hint, use_red=True)
        return True

    file_codes = pending.get("file_codes") or []
    _do_create_seal(open_id, user_id, all_fields, file_codes)
    return True


# 开票凭证类型：表单 proof_file_type 可选值
INVOICE_PROOF_TYPES = ("合同", "对账单", "有赞商城后台订单", "微信小店后台订单", "企业微信收款订单", "其他")


def _infer_invoice_proof_type(file_name, ai_fields):
    """
    根据文件名和 AI 识别内容判断开票凭证类型。
    支持：结算单/对账单、合同/协议、银行水单、订单明细、发货截图、收款截图等。
    返回表单 proof_file_type 可选值之一。
    """
    base_name = (file_name.rsplit(".", 1)[0] if "." in file_name else file_name) or ""
    # 文件名关键词
    if any(k in base_name for k in ("结算", "结算单", "对账", "对账单", "月结")):
        return "对账单"
    if any(k in base_name for k in ("合同", "协议")):
        return "合同"
    if any(k in base_name for k in ("银行", "水单", "流水", "回单")):
        return "其他"  # 银行水单
    if any(k in base_name for k in ("订单", "明细", "发货", "收款", "截图")):
        # 可进一步根据内容区分有赞/微信/企业微信
        pt = (ai_fields.get("proof_file_type") or [])
        if isinstance(pt, list) and pt:
            for p in pt:
                if p in INVOICE_PROOF_TYPES:
                    return p
        return "其他"
    # 根据内容推断
    has_amount = bool(ai_fields.get("amount"))
    has_settlement_no = bool(ai_fields.get("settlement_no"))
    has_buyer = bool(ai_fields.get("buyer_name"))
    has_tax_id = bool(ai_fields.get("tax_id"))
    has_contract_no = bool(ai_fields.get("contract_no"))
    pt = ai_fields.get("proof_file_type")
    if isinstance(pt, list) and pt and pt[0] in INVOICE_PROOF_TYPES:
        return pt[0]
    if (has_amount or has_settlement_no) and not (has_buyer or has_tax_id):
        return "对账单"
    if has_buyer or has_tax_id or has_contract_no:
        return "合同"
    return "其他" if has_amount else "合同"


def _process_invoice_upload_batch(open_id, user_id, files):
    """批量处理开票凭证文件，全部处理完后统一发送选项卡片。合同+对账单合并为一张发票，金额优先取自对账单。"""
    with _state_lock:
        pending = PENDING_INVOICE.get(open_id)
        if not pending:
            return
        doc_fields = dict(pending.get("doc_fields", {}))
        file_codes_list = list(pending.get("file_codes", []))
    # 合同+对账单组合时：金额取自对账单，购方/税号/业务类型取自合同
    amount_from_settlement = None  # 对账单/结算单的金额优先
    amount_from_contract = None
    contract_fields = {}  # 合同中的 buyer_name, tax_id, business_type 等
    for i, f in enumerate(files):
        msg_id = f.get("message_id")
        cj = f.get("content_json", {})
        file_name = cj.get("file_name", f.get("file_name", "未知文件"))
        file_key = cj.get("file_key") or cj.get("image_key", "")
        resource_type = f.get("resource_type", "file")  # "image" 时使用 type=image 下载
        if not file_key:
            continue
        send_message(open_id, f"正在处理文件「{file_name}」（{i+1}/{len(files)}），请稍候...")
        file_content, dl_err = download_message_file(msg_id, file_key, resource_type)
        if not file_content:
            send_message(open_id, f"文件「{file_name}」下载失败，请重新发送。{dl_err or ''}".strip(), use_red=True)
            continue
        file_code, upload_err = upload_approval_file(file_name, file_content)
        if not file_code:
            send_message(open_id, f"文件「{file_name}」上传失败，请重新发送。{upload_err or ''}".strip(), use_red=True)
            continue
        extractor = get_file_extractor("开票申请单")
        ai_fields = extractor(file_content, file_name, {}, get_token) if extractor else {}
        if ai_fields:
            logger.info("开票文件识别结果: %s", ai_fields)
        proof_type = _infer_invoice_proof_type(file_name, ai_fields)
        # 合并 proof_file_type
        if not doc_fields.get("proof_file_type"):
            doc_fields["proof_file_type"] = [proof_type]
        elif proof_type not in (doc_fields.get("proof_file_type") or []):
            existing = doc_fields.get("proof_file_type") or []
            doc_fields["proof_file_type"] = list(dict.fromkeys(existing + [proof_type]))
        # 金额：对账单/银行流水/订单等优先，合同次之（收集，最后统一写入）
        amt = str(ai_fields.get("amount", "")).strip() if ai_fields.get("amount") else ""
        if amt:
            if proof_type != "合同":
                amount_from_settlement = amt  # 对账单、其他、订单类
            else:
                amount_from_contract = amt
        # 合同字段：购方、税号、业务类型等
        if proof_type == "合同":
            for k in ("buyer_name", "tax_id", "business_type", "contract_no"):
                if ai_fields.get(k) and str(ai_fields[k]).strip():
                    contract_fields[k] = str(ai_fields[k]).strip()
        # 通用合并（proof_file_type、其他字段）
        for k, v in ai_fields.items():
            if k == "proof_file_type":
                new_items = v if isinstance(v, list) else ([v] if v and str(v).strip() else [])
                existing = doc_fields.get(k)
                existing = existing if isinstance(existing, list) else ([existing] if existing and str(existing).strip() else [])
                merged = list(dict.fromkeys(existing + [str(x).strip() for x in new_items if x and str(x).strip()]))
                if merged:
                    doc_fields[k] = merged
            elif k != "amount" and v and str(v).strip():
                doc_fields[k] = v
        file_codes_list.append({"file_code": file_code, "proof_type": proof_type, "file_name": file_name})
    # 合同+对账单：金额优先取自对账单
    if amount_from_settlement:
        doc_fields["amount"] = amount_from_settlement
    elif amount_from_contract:
        doc_fields["amount"] = amount_from_contract
    # 合同字段补充（购方、税号、业务类型等）
    for k, v in contract_fields.items():
        if v:
            doc_fields[k] = v
    with _state_lock:
        pending = PENDING_INVOICE.get(open_id)
        if not pending:
            return
        pending["file_codes"] = file_codes_list
        pending["doc_fields"] = doc_fields
        pending["step"] = "user_fields"
    if not file_codes_list:
        send_message(open_id, "未能成功处理任何文件，请重新上传。", use_red=True)
        return
    # 统一发送选项卡片
    def _fmt_val(v):
        if isinstance(v, list):
            return "、".join(str(x) for x in v if x)
        return v
    order = ["amount", "buyer_name", "tax_id", "business_type", "proof_file_type", "contract_no", "settlement_no"]
    lines = []
    for k in order:
        if k in doc_fields and doc_fields[k]:
            lines.append(f"· {FIELD_LABELS.get(k, k)}: {_fmt_val(doc_fields[k])}")
    for k, v in doc_fields.items():
        if k not in order and v:
            lines.append(f"· {FIELD_LABELS.get(k, k)}: {_fmt_val(v)}")
    summary = "\n".join(lines)
    proof_labels = [f.get("proof_type", "") for f in file_codes_list if f.get("proof_type")]
    proof_desc = "、".join(proof_labels) if proof_labels else "凭证"
    summary_prefix = f"已接收{proof_desc}（共 {len(file_codes_list)} 个文件）。\n\n**说明**：每次仅支持开一张发票，您上传的所有文件将合并为一张发票的凭证。\n\n已识别：\n{summary or '（无）'}"
    # 仅合同开票时提示风险，建议补充收款/流水/对账单/电商订单等
    proof_set = set(p for p in proof_labels if p)
    if proof_set == {"合同"}:
        summary_prefix += "\n\n<font color='red'>⚠️ **风险提示**：当前仅提供合同，建议补充收款记录、银行流水、盖章确认的对账单或电商平台订单等凭证，以降低开票风险。</font>"
    with _state_lock:
        p = PENDING_INVOICE.get(open_id)
        if p:
            p["summary_prefix"] = summary_prefix
    # 发卡去重：3 秒内不重复发送，避免事件重试等导致重复发卡
    now = time.time()
    with _state_lock:
        last = _invoice_card_last_sent.get(open_id, 0)
        if now - last < INVOICE_CARD_DEDUP_SEC:
            logger.info("开票选项卡片去重跳过: open_id=%s 距上次 %.1fs", open_id, now - last)
            return
    card = _build_invoice_options_card(doc_fields, summary_prefix)
    body = CreateMessageRequestBody.builder() \
        .receive_id(open_id) \
        .msg_type("interactive") \
        .content(json.dumps(card, ensure_ascii=False)) \
        .build()
    request = CreateMessageRequest.builder() \
        .receive_id_type("open_id") \
        .request_body(body) \
        .build()
    resp = client.im.v1.message.create(request)
    if resp.success():
        with _state_lock:
            _invoice_card_last_sent[open_id] = now
    else:
        logger.error("发送开票选项卡片失败: %s", resp.msg)
        send_message(open_id, f"已接收 {len(file_codes_list)} 个凭证。\n\n已识别：\n{summary or '（无）'}\n\n请补充：发票类型、开票项目。", use_red=True)


def _handle_invoice_file(open_id, user_id, message_id, content_json):
    """单文件入口（兼容旧调用），转为防抖批量流程"""
    with _state_lock:
        if open_id not in PENDING_INVOICE_UPLOAD:
            PENDING_INVOICE_UPLOAD[open_id] = {"files": [], "user_id": user_id, "created_at": time.time(), "timer": None}
        PENDING_INVOICE_UPLOAD[open_id]["files"].append({
            "message_id": message_id,
            "content_json": content_json,
            "file_name": content_json.get("file_name", "未知文件"),
        })
    _schedule_invoice_upload_process(open_id, user_id, immediate=True)


def _do_create_invoice(open_id, user_id, all_fields, file_codes_list):
    """开票申请单字段齐全时，发送确认卡片，用户点击确认后创建工单。file_codes_list: [{file_code, proof_type, file_name}, ...]"""
    all_fields = dict(all_fields)
    # 业务类型兜底：开票项目含 NBBOSS 但 AI 误识别为 Right-bot 时，以开票项目为准
    inv_items = str(all_fields.get("invoice_items", "")).upper()
    bt = str(all_fields.get("business_type", "")).strip().lower()
    if "NBBOSS" in inv_items and bt in ("right-bot", "rightbot"):
        all_fields["business_type"] = "NBBOSS"
    all_fields.setdefault("proof_file_type", ["合同"])
    all_fields.setdefault("contract_sealed", "已盖章")
    aid = _get_invoice_attachment_field_ids()
    codes = [f["file_code"] for f in (file_codes_list or []) if f.get("file_code")]
    file_codes = {}
    if codes:
        if aid.get("unified"):
            file_codes[aid["unified"]] = codes
        elif aid.get("settlement") and aid.get("contract") and len(codes) >= 2:
            file_codes[aid["settlement"]] = [codes[0]]
            file_codes[aid["contract"]] = codes[1:]
        elif aid.get("settlement"):
            file_codes[aid["settlement"]] = codes
    if not file_codes and codes:
        token = get_token()
        cached = get_form_fields("开票申请单", APPROVAL_CODES["开票申请单"], token)
        attach_ids = [fid for fid, info in (cached or {}).items()
                      if info.get("type") in ("attach", "attachV2", "attachment", "attachmentV2", "file")]
        if attach_ids:
            file_codes[attach_ids[0]] = codes

    admin_comment = get_admin_comment("开票申请单", all_fields)
    summary = format_fields_summary(all_fields, "开票申请单")
    file_tokens_with_names = [(f["file_code"], f.get("file_name", "")) for f in (file_codes_list or []) if f.get("file_code")]
    pre_check = run_pre_check("开票申请单", all_fields, file_codes or {}, get_token, file_tokens_with_names=file_tokens_with_names)
    send_confirm_card(open_id, "开票申请单", summary, admin_comment, user_id, all_fields, file_codes=file_codes or None, pre_check_result=pre_check)

    with _state_lock:
        if open_id in PENDING_INVOICE:
            del PENDING_INVOICE[open_id]
        PENDING_INVOICE_UPLOAD.pop(open_id, None)
        # 不在确认前清空 CONVERSATIONS，等用户点击确认后再清空（见 on_card_action_confirm）


def _try_complete_invoice(open_id, user_id, text):
    """用户补充发票类型、开票项目后，创建开票申请单"""
    with _state_lock:
        pending = PENDING_INVOICE.get(open_id)
    if not pending or pending.get("step") != "user_fields":
        return False

    if _is_cancel_intent(text):
        with _state_lock:
            if open_id in PENDING_INVOICE:
                del PENDING_INVOICE[open_id]
                PENDING_INVOICE_UPLOAD.pop(open_id, None)
        send_message(open_id, "已取消开票申请单，如需办理请重新发起。", use_red=True)
        return True

    doc_fields = pending.get("doc_fields", {})
    fc_list = pending.get("file_codes", [])
    if not fc_list:
        send_message(open_id, "开票申请单需要上传开票凭证（合同、对账单、银行水单、订单明细、电商截图等），请重新发起。", use_red=True)
        return True

    prompt = (
        f"用户为开票申请单补充了以下信息：\n{text}\n\n"
        "请提取并返回JSON，必须包含：\n"
        "- invoice_type: 发票类型（如增值税专用发票、普通发票等）\n"
        "- invoice_items: 开票项目（如技术服务费、广告费等）\n"
        "只返回JSON，不要其他内容。"
    )
    try:
        res = call_deepseek_with_retry([{"role": "user", "content": prompt}], response_format={"type": "json_object"}, timeout=15)
        content = res.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        user_fields = json.loads(content)
    except Exception as e:
        logger.warning("开票补充信息解析失败: %s", e)
        with _state_lock:
            retry_count = pending.get("retry_count", 0) + 1
            pending["retry_count"] = retry_count
        hint = "无法识别您补充的信息，请明确说明：发票类型、开票项目。"
        if retry_count >= 3:
            hint += "\n（若需放弃，可回复「取消」）"
        send_message(open_id, hint, use_red=True)
        return True

    all_fields = {**doc_fields, **user_fields}
    missing = [k for k in ["invoice_type", "invoice_items"] if not all_fields.get(k)]
    if missing:
        with _state_lock:
            retry_count = pending.get("retry_count", 0) + 1
            pending["retry_count"] = retry_count
        hint = f"还缺少：{'、'.join([FIELD_LABELS.get(m, m) for m in missing])}\n请补充。"
        if retry_count >= 3:
            hint += "\n（若需放弃，可回复「取消」）"
        send_message(open_id, hint, use_red=True)
        return True

    _do_create_invoice(open_id, user_id, all_fields, fc_list)
    return True


def on_message(data):
    event_id = data.header.event_id
    if _event_processed(event_id):
        return

    open_id = None
    try:
        event = data.event
        open_id = event.sender.sender_id.open_id
        user_id = event.sender.sender_id.user_id
        msg_type = event.message.message_type
        message_id = event.message.message_id
        content_json = json.loads(event.message.content)

        _clean_expired_pending(open_id)

        now = time.time()
        # 限流：文件上传在收集阶段（用印等待上传、未说明意图、开票等待上传）不限制，允许多文件快速连续上传
        with _state_lock:
            in_seal_upload = open_id in PENDING_SEAL_UPLOAD
            in_file_unclear = open_id in PENDING_FILE_UNCLEAR and PENDING_FILE_UNCLEAR.get(open_id, {}).get("files")
            in_invoice_upload = open_id in PENDING_INVOICE and PENDING_INVOICE.get(open_id, {}).get("step") != "user_fields"
            skip_rate_limit = (msg_type in ("file", "image")) and (in_seal_upload or in_file_unclear or open_id in SEAL_INITIAL_FIELDS or in_invoice_upload)
        if not skip_rate_limit:
            with _state_lock:
                last = _user_last_msg.get(open_id, 0)
                if now - last < RATE_LIMIT_SEC:
                    send_message(open_id, "操作过于频繁，请稍后再试。", use_red=True)
                    return
                _user_last_msg[open_id] = now

        if msg_type == "file":
            with _state_lock:
                is_invoice = open_id in PENDING_INVOICE
                is_seal = open_id in PENDING_SEAL
                in_seal_queue = open_id in PENDING_SEAL_QUEUE
                expects_seal = open_id in SEAL_INITIAL_FIELDS  # 用户已说用印，在等上传
            if is_invoice:
                with _state_lock:
                    pending = PENDING_INVOICE.get(open_id)
                    step = pending.get("step", "") if pending else ""
                if step == "user_fields":
                    send_message(open_id, "您已进入选项步骤，请先完成上方卡片的选项选择并点击确认。如需重新上传，请回复「取消」后重新发起。", use_red=True)
                else:
                    file_name = content_json.get("file_name", "未知文件")
                    with _state_lock:
                        if open_id not in PENDING_INVOICE_UPLOAD:
                            PENDING_INVOICE_UPLOAD[open_id] = {"files": [], "user_id": user_id, "created_at": time.time(), "timer": None}
                        PENDING_INVOICE_UPLOAD[open_id]["files"].append({
                            "message_id": message_id,
                            "content_json": content_json,
                            "file_name": file_name,
                        })
                        PENDING_INVOICE_UPLOAD[open_id]["created_at"] = time.time()
                    count = len(PENDING_INVOICE_UPLOAD[open_id]["files"])
                    send_message(open_id, f"已收到文件「{file_name}」（共 {count} 个）。正在处理，请稍候...")
                    _schedule_invoice_upload_process(open_id, user_id)
            elif is_seal or in_seal_queue:
                send_message(open_id, "您当前有用印申请单待完成，请先完成选项选择。如需重新上传多个文件，请回复「取消」后重新发起。", use_red=True)
            elif expects_seal:
                # 收集文件并防抖，多文件时批量进入排队模式（先出第1张卡，点「下一份」出第2张，最后「提交工单」）
                file_name = content_json.get("file_name", "未知文件")
                with _state_lock:
                    if open_id not in PENDING_SEAL_UPLOAD:
                        PENDING_SEAL_UPLOAD[open_id] = {"files": [], "user_id": user_id, "created_at": time.time(), "timer": None}
                    PENDING_SEAL_UPLOAD[open_id]["files"].append({
                        "message_id": message_id,
                        "content_json": content_json,
                        "file_name": file_name,
                    })
                    PENDING_SEAL_UPLOAD[open_id]["created_at"] = time.time()
                count = len(PENDING_SEAL_UPLOAD[open_id]["files"])
                send_message(open_id, f"已收到文件「{file_name}」（共 {count} 个）。正在处理，请稍候...")
                _schedule_seal_upload_process(open_id, user_id)
            else:
                # 用户先上传附件但未说明意图，先询问再处理。支持多文件，追加到列表
                file_name = content_json.get("file_name", "未知文件")
                with _state_lock:
                    if open_id not in PENDING_FILE_UNCLEAR:
                        PENDING_FILE_UNCLEAR[open_id] = {"files": [], "created_at": time.time()}
                    PENDING_FILE_UNCLEAR[open_id]["files"].append({
                        "file_key": content_json.get("file_key", ""),
                        "message_id": message_id,
                        "file_name": file_name,
                        "content_json": content_json,
                    })
                    PENDING_FILE_UNCLEAR[open_id]["created_at"] = time.time()
                count = len(PENDING_FILE_UNCLEAR[open_id]["files"])
                send_message(open_id, f"已收到文件「{file_name}」（共 {count} 个文件）。请问您需要办理：**用印申请单**（盖章）还是 **开票申请单**？请回复「用印」或「开票」。", use_red=True)
                _schedule_file_intent_card(open_id)
            return

        if msg_type == "image":
            # 开票/用印流程中收到图片时，作为凭证处理，而非发送通用指南
            with _state_lock:
                is_invoice = open_id in PENDING_INVOICE
                is_seal = open_id in PENDING_SEAL
                in_seal_queue = open_id in PENDING_SEAL_QUEUE
                expects_seal = open_id in SEAL_INITIAL_FIELDS
            if is_invoice:
                pending = PENDING_INVOICE.get(open_id, {})
                if pending.get("step") == "user_fields":
                    send_message(open_id, "您已进入选项步骤，请先完成上方卡片的选项选择并点击确认。如需重新上传，请回复「取消」后重新发起。", use_red=True)
                else:
                    image_key = content_json.get("image_key") or content_json.get("file_key", "")
                    if not image_key:
                        send_message(open_id, "无法获取图片，请重新发送或改为上传文件格式的凭证。", use_red=True)
                    else:
                        with _state_lock:
                            if open_id not in PENDING_INVOICE_UPLOAD:
                                PENDING_INVOICE_UPLOAD[open_id] = {"files": [], "user_id": user_id, "created_at": time.time(), "timer": None}
                            PENDING_INVOICE_UPLOAD[open_id]["files"].append({
                                "message_id": message_id,
                                "content_json": {"file_key": image_key, "file_name": "凭证截图.png", "image_key": image_key},
                                "file_name": "凭证截图.png",
                                "resource_type": "image",
                            })
                            PENDING_INVOICE_UPLOAD[open_id]["created_at"] = time.time()
                        count = len(PENDING_INVOICE_UPLOAD[open_id]["files"])
                        send_message(open_id, f"已收到图片（共 {count} 个）。正在处理，请稍候...")
                        _schedule_invoice_upload_process(open_id, user_id)
            elif expects_seal and not (is_seal or in_seal_queue):
                image_key = content_json.get("image_key") or content_json.get("file_key", "")
                if image_key:
                    with _state_lock:
                        if open_id not in PENDING_SEAL_UPLOAD:
                            PENDING_SEAL_UPLOAD[open_id] = {"files": [], "user_id": user_id, "created_at": time.time(), "timer": None}
                        PENDING_SEAL_UPLOAD[open_id]["files"].append({
                            "message_id": message_id,
                            "content_json": {"file_key": image_key, "file_name": "用印文件截图.png", "image_key": image_key},
                            "file_name": "用印文件截图.png",
                            "resource_type": "image",
                        })
                        PENDING_SEAL_UPLOAD[open_id]["created_at"] = time.time()
                    count = len(PENDING_SEAL_UPLOAD[open_id]["files"])
                    send_message(open_id, f"已收到图片「用印文件截图」（共 {count} 个）。正在处理，请稍候...")
                    _schedule_seal_upload_process(open_id, user_id)
                else:
                    send_message(open_id, "无法获取图片，请重新发送。", use_red=True)
            elif open_id in PENDING_FILE_UNCLEAR and PENDING_FILE_UNCLEAR.get(open_id, {}).get("files"):
                # 用户先上传了文件但未说明意图，又发了图片，将图片加入待确认列表
                image_key = content_json.get("image_key") or content_json.get("file_key", "")
                if image_key:
                    with _state_lock:
                        PENDING_FILE_UNCLEAR[open_id]["files"].append({
                            "file_key": image_key,
                            "message_id": message_id,
                            "file_name": "凭证截图.png",
                            "content_json": {"file_key": image_key, "file_name": "凭证截图.png", "image_key": image_key},
                            "resource_type": "image",
                        })
                        PENDING_FILE_UNCLEAR[open_id]["created_at"] = time.time()
                    count = len(PENDING_FILE_UNCLEAR[open_id]["files"])
                    send_message(open_id, f"已收到图片（共 {count} 个文件）。请问您需要办理：**用印申请单**（盖章）还是 **开票申请单**？请回复「用印」或「开票」。", use_red=True)
                    _schedule_file_intent_card(open_id)
                else:
                    send_approval_type_options_card(open_id)
            else:
                send_approval_type_options_card(open_id)
            return

        text = content_json.get("text", "").strip()
        if not text:
            send_approval_type_options_card(open_id)
            return

        # 自动审批开关：仅 auto_approve_user_ids 中的用户可操作
        if user_id in get_auto_approve_user_ids():
            cmd = check_switch_command(text)
            if cmd:
                action, approval_type = cmd[0], cmd[1] if len(cmd) > 1 else None
                if action == "enable":
                    set_auto_approval_enabled(True, user_id)
                    send_message(open_id, "已开启自动审批。")
                    return
                if action == "disable":
                    set_auto_approval_enabled(False, user_id)
                    send_message(open_id, "已关闭自动审批。")
                    return
                if action == "enable_all":
                    set_all_types_enabled(True, user_id)
                    send_message(open_id, "已全部开启自动审批。")
                    return
                if action == "disable_all":
                    set_auto_approval_enabled(False, user_id)
                    send_message(open_id, "已全部关闭自动审批。")
                    return
                if action == "enable_type" and approval_type:
                    set_auto_approval_enabled(True, user_id)
                    set_auto_approval_type_enabled(approval_type, True, user_id)
                    send_message(open_id, f"已开启「{approval_type}」自动审批。")
                    return
                if action == "disable_type" and approval_type:
                    set_auto_approval_type_enabled(approval_type, False, user_id)
                    send_message(open_id, f"已关闭「{approval_type}」自动审批。")
                    return
                if action == "query":
                    st = get_auto_approval_status()
                    lines = [f"总开关：{'已开启' if st['enabled'] else '已关闭'}"]
                    for t, on in st["types"].items():
                        lines.append(f"  · {t}：{'开' if on else '关'}")
                    send_message(open_id, "自动审批状态：\n" + "\n".join(lines))
                    return
                if action == "poll":
                    was_disabled = not is_auto_approval_enabled()
                    if was_disabled:
                        set_auto_approval_enabled(True, user_id)
                    try:
                        poll_and_process(get_token)
                        msg = "已执行一次轮询，处理待审批工单。"
                        if was_disabled:
                            msg = "已临时开启自动审批并执行轮询。\n\n" + msg
                        send_message(open_id, msg)
                    except Exception as e:
                        logger.exception("手动轮询异常: %s", e)
                        send_message(open_id, f"轮询执行异常：{e}", use_red=True)
                    return

        # 汇总卡出现后，用户通过对话修改字段
        if _try_modify_confirm(open_id, user_id, text):
            return

        with _state_lock:
            in_seal = open_id in PENDING_SEAL
            in_invoice = open_id in PENDING_INVOICE
            pf = PENDING_FILE_UNCLEAR.get(open_id)
            pending_file = pf if (pf and pf.get("files")) else None
        if in_seal:
            if _try_complete_seal(open_id, user_id, text):
                return
        if in_invoice:
            if _try_complete_invoice(open_id, user_id, text):
                return

        # 用户先上传了附件但未说明意图，现在用文字说明
        if pending_file:
            if _is_cancel_intent(text):
                with _state_lock:
                    entry = PENDING_FILE_UNCLEAR.pop(open_id, None)
                    if entry and entry.get("timer"):
                        try:
                            entry["timer"].cancel()
                        except Exception:
                            pass
                send_message(open_id, "已取消。如需办理用印或开票，请重新上传文件并说明用途。", use_red=True)
                return
            with _state_lock:
                CONVERSATIONS.setdefault(open_id, [])
                CONVERSATIONS[open_id].append({"role": "user", "content": text})
                if len(CONVERSATIONS[open_id]) > 10:
                    CONVERSATIONS[open_id] = CONVERSATIONS[open_id][-10:]
            text_stripped = text.strip()
            files_list = pending_file.get("files", [])
            # 1. 用户明确回复「用印」或「开票」时直接采用，避免历史对话导致 AI 仍返回两者造成死循环
            if text_stripped in ("用印", "开票"):
                needs_seal = (text_stripped == "用印")
                needs_invoice = (text_stripped == "开票")
                requests = [{"approval_type": "用印申请单" if needs_seal else "开票申请单", "fields": {}}]
            else:
                # 2. 尝试解析「第一个用印、第二个开票」等分别指定
                intents = _parse_file_intents(text_stripped, len(files_list))
                if intents is not None:
                    needs_seal = needs_invoice = False
                    for idx, intent in intents.items():
                        if intent == "用印":
                            needs_seal = True
                        elif intent == "开票":
                            needs_invoice = True
                    if needs_seal or needs_invoice:
                        # 分别处理：按意图拆分文件
                        _handle_split_file_intents(open_id, user_id, files_list, intents)
                        return
                # 3. 否则调用 AI 分析
                conv_copy = list(CONVERSATIONS[open_id])
                result = analyze_message(conv_copy)
                requests = result.get("requests", [])
                needs_seal = any(r.get("approval_type") == "用印申请单" for r in requests)
                needs_invoice = any(r.get("approval_type") == "开票申请单" for r in requests)
            if needs_seal and not needs_invoice:
                files_list = pending_file.get("files", [])
                with _state_lock:
                    entry = PENDING_FILE_UNCLEAR.pop(open_id, None)
                    if entry and entry.get("timer"):
                        try:
                            entry["timer"].cancel()
                        except Exception:
                            pass
                req_seal = next((r for r in requests if r.get("approval_type") == "用印申请单"), None)
                if req_seal and req_seal.get("fields"):
                    with _state_lock:
                        SEAL_INITIAL_FIELDS[open_id] = {"fields": req_seal["fields"], "created_at": time.time()}
                if files_list:
                    _handle_file_message(open_id, user_id, None, None, files_list=files_list)
                return
            if needs_invoice and not needs_seal:
                files_list = pending_file.get("files", [])
                with _state_lock:
                    entry = PENDING_FILE_UNCLEAR.pop(open_id, None)
                    if entry and entry.get("timer"):
                        try:
                            entry["timer"].cancel()
                        except Exception:
                            pass
                req_inv = next((r for r in requests if r.get("approval_type") == "开票申请单"), None)
                doc_fields_init = {k: v for k, v in (req_inv.get("fields") or {}).items() if v and str(v).strip()} if req_inv else {}
                with _state_lock:
                    PENDING_INVOICE[open_id] = {
                        "step": "need_files",
                        "file_codes": [],
                        "doc_fields": doc_fields_init,
                        "user_id": user_id,
                        "created_at": time.time(),
                    }
                if files_list:
                    with _state_lock:
                        if open_id not in PENDING_INVOICE_UPLOAD:
                            PENDING_INVOICE_UPLOAD[open_id] = {"files": [], "user_id": user_id, "created_at": time.time(), "timer": None}
                        for f in files_list:
                            item = {"message_id": f.get("message_id"), "content_json": f.get("content_json", {}), "file_name": f.get("file_name", "未知文件")}
                            if "resource_type" in f:
                                item["resource_type"] = f["resource_type"]
                            PENDING_INVOICE_UPLOAD[open_id]["files"].append(item)
                    _schedule_invoice_upload_process(open_id, user_id, immediate=True)
                return
            if needs_seal and needs_invoice:
                send_message(open_id, "您同时提到用印和开票。请明确：本次上传的文件是用于 **用印申请单** 还是 **开票申请单**？回复「用印」或「开票」。", use_red=True)
                return
            send_message(open_id, "未识别到用印或开票需求。请问您上传的文件是用于：**用印申请单**（盖章）还是 **开票申请单**？请回复「用印」或「开票」。", use_red=True)
            return

        with _state_lock:
            if open_id not in CONVERSATIONS:
                CONVERSATIONS[open_id] = []
            CONVERSATIONS[open_id].append({"role": "user", "content": text})
            if len(CONVERSATIONS[open_id]) > 10:
                CONVERSATIONS[open_id] = CONVERSATIONS[open_id][-10:]
            conv_copy = list(CONVERSATIONS[open_id])

        result = analyze_message(conv_copy)
        requests = result.get("requests", [])
        unclear = result.get("unclear", "")

        if not requests:
            # 不发送 unclear 红色提示，直接发工单类型选项卡（避免与按钮内容重复）
            send_approval_type_options_card(open_id)
            with _state_lock:
                if open_id in CONVERSATIONS:
                    CONVERSATIONS[open_id].append({"role": "assistant", "content": unclear or "请选择工单类型"})
            return

        # 第一阶段：处理需特殊路由的类型（用印/开票），收集剩余待处理
        remaining_requests = []
        with _state_lock:
            needs_seal = any(r.get("approval_type") == "用印申请单" for r in requests) and open_id not in PENDING_SEAL
            needs_invoice = any(r.get("approval_type") == "开票申请单" for r in requests) and open_id not in PENDING_INVOICE

        if needs_seal and needs_invoice:
            req_seal = next(r for r in requests if r.get("approval_type") == "用印申请单")
            initial = req_seal.get("fields", {})
            if initial:
                with _state_lock:
                    SEAL_INITIAL_FIELDS[open_id] = {"fields": initial, "created_at": time.time()}
            send_message(open_id, "您同时发起了用印申请单和开票申请单。请先完成用印申请单（上传需要盖章的文件），完成后再发送「开票申请单」。\n\n"
                         "请上传需要盖章的文件（Word/PDF/图片均可），我会自动识别内容。", use_red=True)
            with _state_lock:
                if open_id in CONVERSATIONS:
                    CONVERSATIONS[open_id].append({"role": "assistant", "content": "请先完成用印申请单"})
            remaining_requests = [r for r in requests if r.get("approval_type") not in ("用印申请单", "开票申请单")]
            if not remaining_requests:
                return
        else:
            for req in requests:
                at = req.get("approval_type", "")
                miss = req.get("missing", [])
                fields_check = req.get("fields", {})
                if at == "用印申请单" and open_id not in PENDING_SEAL:
                    initial = req.get("fields", {}) or {}
                    with _state_lock:
                        SEAL_INITIAL_FIELDS[open_id] = {"fields": initial, "created_at": time.time()}
                    send_message(open_id, "请补充以下信息：\n"
                                 f"用印申请单还缺少：上传用章文件\n"
                                 f"请先上传需要盖章的文件（Word/PDF/图片均可），我会自动识别内容。", use_red=True)
                    with _state_lock:
                        if open_id in CONVERSATIONS:
                            CONVERSATIONS[open_id].append({"role": "assistant", "content": "请上传需要盖章的文件"})
                    continue
                if at == "开票申请单" and open_id not in PENDING_INVOICE:
                    initial = req.get("fields", {})
                    doc_fields_init = {k: v for k, v in initial.items() if v and str(v).strip()} if initial else {}
                    with _state_lock:
                        PENDING_INVOICE[open_id] = {
                            "step": "need_files",
                            "file_codes": [],
                            "doc_fields": doc_fields_init,
                            "user_id": user_id,
                            "created_at": time.time(),
                        }
                    send_message(open_id, "请补充以下信息：\n"
                                 f"开票申请单需要：上传开票凭证\n"
                                 f"请上传凭证（结算单+合同、合同+银行水单、合同+订单明细、电商发货/收款截图等，Word/PDF/图片均可），我会自动识别类型。\n\n"
                                 f"**说明**：每次仅支持开一张发票，您上传的所有文件将合并为一张发票的凭证。", use_red=True)
                    with _state_lock:
                        if open_id in CONVERSATIONS:
                            CONVERSATIONS[open_id].append({"role": "assistant", "content": "请上传结算单"})
                    continue
                if at == "采购申请":
                    cd = fields_check.get("cost_detail")
                    if not cd or (isinstance(cd, list) and len(cd) == 0) or cd == "":
                        if "cost_detail" not in miss:
                            miss.append("cost_detail")
                            req["missing"] = miss
                if at == "招待/团建物资领用":
                    idetail = fields_check.get("item_detail")
                    if not idetail or (isinstance(idetail, list) and len(idetail) == 0) or idetail == "":
                        if "item_detail" not in miss:
                            miss.append("item_detail")
                            req["missing"] = miss
                remaining_requests.append(req)

        complete = [r for r in remaining_requests if not r.get("missing")]
        incomplete = [(r["approval_type"], r.get("missing", [])) for r in remaining_requests if r.get("missing")]

        replies = []
        for req in complete:
            approval_type = req.get("approval_type")
            fields = req.get("fields", {})
            if not approval_type:
                continue
            admin_comment = get_admin_comment(approval_type, fields)
            summary = format_fields_summary(fields, approval_type)

            if approval_type in LINK_ONLY_TYPES:
                approval_code = APPROVAL_CODES[approval_type]
                # 飞书 AppLink：员工发起工单，需在飞书客户端内点击（浏览器打开会显示「此页面无效」）
                link = f"https://applink.feishu.cn/client/approval?tab=create&definitionCode={approval_code}"
                tip = (
                    f"【{approval_type}】\n{summary}\n\n"
                    f"请点击下方按钮发起工单（需在飞书客户端内打开）。"
                    f"若链接无效，请到 飞书 → 审批 → 发起审批 → 选择「{approval_type}」手动填写。"
                )
                send_card_message(open_id, tip, link, f"打开{approval_type}审批表单")
                replies.append(f"· {approval_type}：已整理，请点击按钮提交")
            else:
                # 预检：报备单(无审批节点) API 不支持，直接走链接流程
                approval_code = APPROVAL_CODES[approval_type]
                token = get_token()
                if is_free_process(approval_code, token):
                    link = f"https://applink.feishu.cn/client/approval?tab=create&definitionCode={approval_code}"
                    tip = (
                        f"【{approval_type}】\n{summary}\n\n"
                        f"该类型暂不支持自动创建，请点击下方按钮在飞书中发起（需在飞书客户端内打开）："
                    )
                    send_card_message(open_id, tip, link, f"打开{approval_type}审批表单")
                    replies.append(f"· {approval_type}：已整理，请点击按钮提交")
                else:
                    pre_check = run_pre_check(approval_type, fields, None, get_token)
                    send_confirm_card(open_id, approval_type, format_fields_summary(fields, approval_type), admin_comment, user_id, fields, pre_check_result=pre_check)
                    replies.append(f"· {approval_type}：请确认信息后点击卡片按钮提交")

        if incomplete:
            parts = [f"{at}还缺少：{'、'.join([FIELD_LABELS.get(m, m) for m in miss])}" for at, miss in incomplete]
            replies.append("请补充以下信息：\n" + "\n".join(parts))

        if not complete:
            body = "\n".join(replies)
            if body.strip():
                send_message(open_id, body, use_red=True)
            with _state_lock:
                if open_id in CONVERSATIONS:
                    CONVERSATIONS[open_id].append({"role": "assistant", "content": "请补充信息"})
            return

        header = f"✅ 已处理 {len(complete)} 个申请：\n\n" if len(complete) > 1 else ""
        body = header + "\n\n".join(replies)
        if body.strip():
            send_message(open_id, body, use_red=True)
        with _state_lock:
            if not incomplete and open_id in CONVERSATIONS:
                CONVERSATIONS[open_id] = []

    except Exception as e:
        logger.exception("处理消息出错: %s", e)
        if open_id:
            send_message(open_id, "系统出现异常，请稍后再试。", use_red=True)


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/debug-extract":
            from approval_types import get_file_extractor, FILE_EXTRACTORS
            diag = {
                "extractor_registered": get_file_extractor("用印申请单") is not None,
                "invoice_extractor": get_file_extractor("开票申请单") is not None,
                "file_extractors": list(FILE_EXTRACTORS.keys()),
                "DEEPSEEK_API_KEY_set": bool(os.environ.get("DEEPSEEK_API_KEY")),
                "DEEPSEEK_API_KEY_len": len(os.environ.get("DEEPSEEK_API_KEY", "")),
                "hint": "若 extractor_registered 为 false 或 DEEPSEEK_API_KEY_set 为 false，则无法识别",
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(diag, ensure_ascii=False, indent=2).encode("utf-8"))
        elif path == "/debug-instances-query":
            from urllib.parse import parse_qs
            qs = parse_qs((self.path.split("?") + ["?"])[1])
            if SECRET_TOKEN:
                token_param = (qs.get("token") or [""])[0]
                if token_param != SECRET_TOKEN:
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(b"Forbidden: invalid or missing token")
                    return
            at = (qs.get("type") or [""])[0] or "开票申请单"
            try:
                code = APPROVAL_CODES.get(at, "")
                if not code:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": f"未知类型: {at}"}, ensure_ascii=False).encode("utf-8"))
                    return
                token = get_token()
                end_ts = int(time.time() * 1000)
                start_ts = int((time.time() - 7 * 24 * 3600) * 1000)
                body = {
                    "approval_code": code,
                    "instance_start_time_from": str(start_ts),
                    "instance_start_time_to": str(end_ts),
                    "instance_status": "PENDING",
                }
                res = httpx.post(
                    "https://open.feishu.cn/open-apis/approval/v4/instances/query",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    params={"user_id_type": "user_id"},
                    json=body,
                    timeout=10,
                )
                data = res.json()
                page_data = data.get("data", {})
                codes = page_data.get("instance_code_list", [])
                if not codes and page_data.get("instance_list"):
                    codes = [item.get("instance", {}).get("code") for item in page_data["instance_list"] if item.get("instance", {}).get("code")]
                out = {
                    "approval_type": at,
                    "approval_code": code,
                    "request_body": body,
                    "time_range_human": {
                        "from": time.strftime("%Y-%m-%d %H:%M", time.localtime(start_ts / 1000)),
                        "to": time.strftime("%Y-%m-%d %H:%M", time.localtime(end_ts / 1000)),
                    },
                    "http_status": res.status_code,
                    "response": data,
                    "instance_count": len(codes),
                }
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(out, ensure_ascii=False, indent=2).encode("utf-8"))
            except Exception as e:
                import traceback
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e), "traceback": traceback.format_exc()}, ensure_ascii=False).encode("utf-8"))
        elif path == "/debug-form":
            from urllib.parse import parse_qs
            qs = parse_qs((self.path.split("?") + ["?"])[1])
            if SECRET_TOKEN:
                token = (qs.get("token") or [""])[0]
                if token != SECRET_TOKEN:
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(b"Forbidden: invalid or missing token")
                    return
            at = (qs.get("type") or [""])[0] or "采购申请"
            try:
                code = APPROVAL_CODES.get(at, "")
                token = get_token()
                res = httpx.get(
                    f"https://open.feishu.cn/open-apis/approval/v4/approvals/{code}",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10
                )
                data = res.json()
                form_str = data.get("data", {}).get("form", "[]")
                form = json.loads(form_str) if isinstance(form_str, str) else form_str
                out = {"approval": at, "fields": []}
                for item in form:
                    fid = item.get("id")
                    fname = item.get("name", "")
                    ftype = item.get("type", "")
                    out["fields"].append({"id": fid, "name": fname, "type": ftype})
                    if ftype == "fieldList":
                        out["fields"][-1]["raw_item"] = item
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(out, ensure_ascii=False, indent=2).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"))
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def _start_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    logger.info("健康检查服务已启动 :%s", port)
    server.serve_forever()


def _start_auto_approval_polling():
    """定时轮询待审批任务并执行自动审批。启动时立即执行一次，再按间隔轮询"""
    from approval_auto import poll_and_process, is_auto_approval_enabled
    interval = int(os.environ.get("AUTO_APPROVAL_POLL_INTERVAL", 300))
    first_run = True
    while True:
        try:
            if not first_run:
                time.sleep(interval)
            first_run = False
            if is_auto_approval_enabled():
                poll_and_process(get_token)
        except Exception as e:
            logger.exception("自动审批轮询异常: %s", e)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _validate_env()
    threading.Thread(target=_start_health_server, daemon=True).start()
    threading.Thread(target=_start_auto_approval_polling, daemon=True).start()

    # 飞书卡片回调以 CARD 消息类型推送，需当作 EVENT 处理以触发 on_card_action_confirm。
    # 猴子补丁：将 _handle_data_frame 收到的 CARD 帧的 type 改为 EVENT，使 EventDispatcherHandler 路由到 on_card_action_confirm。
    # 依赖：lark_oapi.ws.client.Client._handle_data_frame 的签名与实现。SDK 版本变更可能导致补丁失效。
    # 固定版本见 requirements.txt：lark-oapi==1.4.24
    import lark_oapi.ws.client as _ws_client
    from lark_oapi.ws.enum import MessageType as _MT
    _orig_hdf = _ws_client.Client._handle_data_frame

    async def _patched_handle_data(self, frame):
        hs = frame.headers
        type_val = _ws_client._get_by_key(hs, _ws_client.HEADER_TYPE)
        if type_val == _MT.CARD.value:
            for h in hs:
                if h.key == _ws_client.HEADER_TYPE:
                    h.value = _MT.EVENT.value
                    break
        return await _orig_hdf(self, frame)

    _ws_client.Client._handle_data_frame = _patched_handle_data

    handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(on_message) \
        .register_p2_im_message_message_read_v1(_on_message_read) \
        .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(_on_message_read) \
        .register_p2_card_action_trigger(on_card_action_confirm) \
        .build()
    ws_client = lark.ws.Client(
        FEISHU_APP_ID,
        FEISHU_APP_SECRET,
        event_handler=handler,
        log_level=lark.LogLevel.INFO
    )
    logger.info("飞书审批机器人已启动...")
    ws_client.start()