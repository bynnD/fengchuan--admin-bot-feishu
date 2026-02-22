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
    APPROVAL_CODES, FIELD_LABELS, APPROVAL_FIELD_HINTS,
    LINK_ONLY_TYPES, FIELD_ID_FALLBACK, FIELD_ORDER, DATE_FIELDS, FIELD_LABELS_REVERSE,
    IMAGE_SUPPORT_TYPES, FIELDLIST_SUBFIELDS_FALLBACK, get_admin_comment, get_file_extractor
)
from field_cache import get_form_fields, invalidate_cache, is_free_process, mark_free_process
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
PENDING_TTL = 30 * 60  # 30 分钟
SEAL_INITIAL_TTL = 30 * 60  # 30 分钟

# 限流：每用户最小间隔（秒）
RATE_LIMIT_SEC = 2

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
                to_notify.append((oid, "用印申请已超时，请重新发起。"))
        for oid in list(PENDING_INVOICE.keys()) if open_id is None else ([open_id] if open_id in PENDING_INVOICE else []):
            if oid in PENDING_INVOICE and now - PENDING_INVOICE[oid].get("created_at", 0) > PENDING_TTL:
                del PENDING_INVOICE[oid]
                to_notify.append((oid, "开票申请已超时，请重新发起。"))
        for oid in list(SEAL_INITIAL_FIELDS.keys()) if open_id is None else ([open_id] if open_id in SEAL_INITIAL_FIELDS else []):
            data = SEAL_INITIAL_FIELDS.get(oid)
            created = data.get("created_at", 0) if isinstance(data, dict) else 0
            if created and now - created > SEAL_INITIAL_TTL:
                del SEAL_INITIAL_FIELDS[oid]
        for cid in list(PENDING_CONFIRM.keys()):
            if now - PENDING_CONFIRM[cid].get("created_at", 0) > CONFIRM_TTL:
                del PENDING_CONFIRM[cid]
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


def send_message(open_id, text):
    text = _sanitize_message_text(text)
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


def send_confirm_card(open_id, approval_type, summary, admin_comment, user_id, fields, file_codes=None):
    """发送工单确认卡片，用户点击「确认」后创建工单"""
    confirm_id = str(uuid.uuid4())
    with _state_lock:
        PENDING_CONFIRM[confirm_id] = {
            "open_id": open_id,
            "user_id": user_id,
            "approval_type": approval_type,
            "fields": dict(fields),
            "file_codes": dict(file_codes) if file_codes else None,
            "admin_comment": admin_comment,
            "created_at": time.time(),
        }
    text = f"【{approval_type}】\n\n{summary}\n\n行政意见: {admin_comment}\n\n请确认以上信息无误后，点击下方按钮提交工单。"
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


def on_card_action_confirm(data):
    """处理用户点击确认按钮的回调，创建工单"""
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
        confirm_id = value.get("confirm_id", "")
        if not confirm_id:
            return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": "参数无效"}})
        with _state_lock:
            pending = PENDING_CONFIRM.pop(confirm_id, None)
        if not pending:
            return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": "确认已过期，请重新发起"}})
        if time.time() - pending.get("created_at", 0) > CONFIRM_TTL:
            return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": "确认已超时，请重新发起"}})
        approval_type = pending["approval_type"]
        fields = pending["fields"]
        file_codes = pending.get("file_codes")
        admin_comment = pending.get("admin_comment", "")
        success, msg, resp_data, summary = create_approval(user_id, approval_type, fields, file_codes=file_codes)
        if success:
            with _state_lock:
                if open_id in CONVERSATIONS:
                    CONVERSATIONS[open_id] = []
            instance_code = resp_data.get("instance_code", "")
            if instance_code:
                link = f"https://applink.feishu.cn/client/approval?instanceCode={instance_code}"
                send_card_message(open_id, f"【{approval_type}】\n{summary}\n\n行政意见: {admin_comment}\n\n工单已创建，点击下方按钮查看：", link, "查看工单", use_desktop_link=True)
            else:
                send_message(open_id, f"· {approval_type}：✅ 已提交\n{summary}\n行政意见: {admin_comment}")
            return P2CardActionTriggerResponse(d={"toast": {"type": "success", "content": "工单已创建"}})
        return P2CardActionTriggerResponse(d={"toast": {"type": "error", "content": f"提交失败：{msg}"}})
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
        f"例如「我要采购笔记本，还要给合同盖章」= 采购申请 + 用印申请。"
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
        f"7. 用印申请：识别到用印需求时，只提取对话中能得到的字段(company/seal_type/reason等)，"
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
                            val = _match_sub_field(sf.get("name", ""), item)
                            row.append({"id": sf["id"], "type": sf.get("type", "input"), "value": val})
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
                # 用印申请等：传入的 file_codes 可能用固定 ID，实际表单的附件字段 ID 可能不同
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
        # 开票申请：客户/开票名称、税务登记证号/社会统一信用代码 等表单字段名兜底
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
                matched = False
                for opt in opts:
                    if isinstance(opt, dict):
                        if opt.get("value") == raw_str or opt.get("text") == raw_str:
                            raw = opt.get("value", raw_str)
                            matched = True
                            break
                        if raw_str and raw_str in (opt.get("text", ""), opt.get("value", "")):
                            raw = opt.get("value", raw_str)
                            matched = True
                            break
                if not matched:
                    raw = opts[0].get("value", "") if isinstance(opts[0], dict) else ""
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
    """将 radioV2/radio 的 value 转为可读的 text"""
    if not options or not val:
        return val
    for opt in options:
        if isinstance(opt, dict) and opt.get("value") == val:
            return opt.get("text", val)
    return val


def _form_summary(form_list, cached):
    """根据实际提交的表单和缓存的字段名生成摘要，radioV2 显示 text 而非 value"""
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
        elif ftype in ("attach", "attachV2", "image", "imageV2"):
            continue
        elif ftype in ("radioV2", "radio"):
            val = item.get("value", "")
            if val:
                display = _value_to_text(val, info.get("options", []))
                lines.append(f"· {name}: {display}")
        elif ftype == "fieldList":
            val = item.get("value", [])
            if val and isinstance(val, list) and isinstance(val[0], list):
                lines.append(f"· {name}:")
                for i, row in enumerate(val):
                    parts = [f"{c.get('value','')}" for c in row if c.get("value")]
                    if parts:
                        lines.append(f"  {i+1}. {', '.join(parts)}")
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

    summary = _form_summary(form_list, cached or {})

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
# 用印申请：用户首次消息中已提取的字段，等收到文件后合并。结构 {open_id: {"fields": {...}, "created_at": ts}}
SEAL_INITIAL_FIELDS = {}

# 限流：open_id -> 上次消息时间
_user_last_msg = {}

# 开票申请：需结算单+合同双附件，分步收集
PENDING_INVOICE = {}

# 工单确认：用户点击确认按钮后创建。confirm_id -> {open_id, user_id, approval_type, fields, file_codes, admin_comment, created_at}
PENDING_CONFIRM = {}
CONFIRM_TTL = 15 * 60  # 15 分钟

ATTACHMENT_FIELD_ID = "widget15828104903330001"

# 用印申请需从模版读取选项的字段
SEAL_OPTION_FIELDS = {
    "company": "widget17375357884790001",
    "usage_method": "widget17375347703620001",
    "seal_type": "widget15754438920110001",
    "lawyer_reviewed": "widget17375349618880001",
}


def _get_seal_form_options():
    """从工单模版读取用印申请的选项，返回 {逻辑键: [选项文本列表]}"""
    token = get_token()
    cached = get_form_fields("用印申请", APPROVAL_CODES["用印申请"], token)
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


def _get_invoice_attachment_field_ids():
    """从开票申请表单缓存中按名称查找结算单、合同附件字段 ID"""
    token = get_token()
    cached = get_form_fields("开票申请", APPROVAL_CODES.get("开票申请", ""), token)
    if not cached:
        return {}
    result = {}
    for fid, info in cached.items():
        if info.get("type") in ("attach", "attachV2", "attachment", "attachmentV2", "file"):
            name = info.get("name", "")
            if "结算" in name or "结算单" in name:
                result["settlement"] = fid
            elif "合同" in name:
                result["contract"] = fid
    return result


def _handle_file_message(open_id, user_id, message_id, content_json):
    """处理文件消息：下载文件→上传审批附件→提取文件名→合并首次消息与文件名推断→等用户补充其余字段"""
    file_key = content_json.get("file_key", "")
    file_name = content_json.get("file_name", "未知文件")
    if not file_key:
        send_message(open_id, "无法获取文件，请重新发送。")
        return

    send_message(open_id, f"正在处理文件「{file_name}」，请稍候...")

    file_content, dl_err = download_message_file(message_id, file_key, "file")
    if not file_content:
        err_detail = f"（{dl_err}）" if dl_err else ""
        send_message(open_id, f"文件下载失败，请重新发送。{err_detail}".strip())
        return
    logger.info("用印文件: 已下载 %s, 大小=%d bytes", file_name, len(file_content))

    file_code, upload_err = upload_approval_file(file_name, file_content)
    if not file_code:
        err_detail = f"（{upload_err}）" if upload_err else ""
        send_message(open_id, f"文件上传失败，请重新发送文件。附件上传成功后才能继续创建工单。{err_detail}")
        return

    doc_name = file_name.rsplit(".", 1)[0] if "." in file_name else file_name
    ext = (file_name.rsplit(".", 1)[-1] or "").lower()
    doc_type_map = {"docx": "Word文档", "doc": "Word文档", "pdf": "PDF"}
    doc_type = doc_type_map.get(ext, ext.upper() if ext else "")

    doc_fields = {
        "document_name": doc_name,
        "document_count": "1",
        "document_type": doc_type,
    }

    opts = _get_seal_form_options()
    company_opts = opts.get("company", [])
    seal_opts = opts.get("seal_type", ["公章", "合同章", "法人章", "财务章"])
    usage_opts = opts.get("usage_method", ["盖章", "外带"])
    lawyer_opts = opts.get("lawyer_reviewed", ["是", "否"])

    # 合并：文件基础信息 + 文件内容 AI 识别（使用通用提取器，含 OCR，适用所有有附件识别需求的工单）+ 首次消息已提取字段（后者优先）
    extractor = get_file_extractor("用印申请")
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
    # 仅当首次消息有有效值时才覆盖，避免空值覆盖 AI 识别结果
    for k, v in initial_fields.items():
        if v and str(v).strip():
            doc_fields[k] = v
    doc_fields.setdefault("usage_method", "盖章")

    with _state_lock:
        CONVERSATIONS.setdefault(open_id, [])
        CONVERSATIONS[open_id].append({
            "role": "assistant",
            "content": f"[已接收文件] 文件名称={doc_name}"
        })

    missing = [k for k in ["company", "seal_type", "reason", "lawyer_reviewed"] if not doc_fields.get(k)]
    if missing and ai_fields:
        logger.debug("用印合并后仍缺失 %s，doc_fields: company=%r, seal_type=%r, reason=%r",
                     missing, doc_fields.get('company'), doc_fields.get('seal_type'), doc_fields.get('reason'))
    if not missing:
        # 全部可推断，直接创建工单
        _do_create_seal(open_id, user_id, doc_fields, file_code)
        return

    with _state_lock:
        PENDING_SEAL[open_id] = {
            "doc_fields": doc_fields,
            "file_code": file_code,
            "user_id": user_id,
            "created_at": time.time(),
        }

    # 只列出缺失项
    labels = {"company": "用印公司", "seal_type": "印章类型", "reason": "文件用途/用印事由", "lawyer_reviewed": "律师是否已审核"}
    hint_map = {
        "company": f"{'、'.join(company_opts) if company_opts else '请输入'}",
        "seal_type": "、".join(seal_opts),
        "reason": "（请描述）",
        "lawyer_reviewed": f"{'、'.join(lawyer_opts)}（必填，请明确选择）",
    }
    lines = [
        f"已接收文件：{file_name}",
        f"· 文件名称: {doc_name}",
        "",
        "请补充以下信息（一条消息说完即可）：",
    ]
    for i, k in enumerate(missing, 1):
        lines.append(f"{i}. {labels[k]}：{hint_map.get(k, '')}")
    lines.append(f"盖章还是外带：{'、'.join(usage_opts)}（默认盖章）")
    send_message(open_id, "\n".join(lines))


def _do_create_seal(open_id, user_id, all_fields, file_code):
    """用印申请字段齐全时，发送确认卡片，用户点击确认后创建工单"""
    all_fields = dict(all_fields)
    all_fields.setdefault("usage_method", "盖章")
    all_fields.setdefault("document_count", "1")

    file_codes = {ATTACHMENT_FIELD_ID: [file_code]} if file_code else {}
    admin_comment = get_admin_comment("用印申请", all_fields)
    summary = format_fields_summary(all_fields, "用印申请")
    send_confirm_card(open_id, "用印申请", summary, admin_comment, user_id, all_fields, file_codes=file_codes)

    with _state_lock:
        if open_id in PENDING_SEAL:
            del PENDING_SEAL[open_id]
        # 不在确认前清空 CONVERSATIONS，等用户点击确认后再清空（见 on_card_action_confirm）

    send_message(open_id, "请确认工单信息无误后，点击卡片上的「确认提交」按钮。")


def _try_complete_seal(open_id, user_id, text):
    """用户发送补充信息后，合并文件字段+用户字段，创建用印申请"""
    with _state_lock:
        pending = PENDING_SEAL.get(open_id)
    if not pending:
        return False

    if _is_cancel_intent(text):
        with _state_lock:
            if open_id in PENDING_SEAL:
                del PENDING_SEAL[open_id]
        send_message(open_id, "已取消用印申请，如需办理请重新发起。")
        return True

    doc_fields = pending["doc_fields"]
    file_code = pending.get("file_code")

    opts = _get_seal_form_options()
    company_hint = f"（选项：{'/'.join(opts.get('company', []))}）" if opts.get("company") else ""
    seal_hint = f"（选项：{'/'.join(opts.get('seal_type', ['公章','合同章','法人章','财务章']))}）"
    usage_hint = f"（选项：{'/'.join(opts.get('usage_method', ['盖章','外带']))}，默认盖章）"
    lawyer_hint = f"（选项：{'/'.join(opts.get('lawyer_reviewed', ['是','否']))}，必填，用户必须明确选择）"

    prompt = (
        f"用户为用印申请补充了以下信息：\n{text}\n\n"
        f"请提取并返回JSON，包含：\n"
        f"- company: 用印公司{company_hint}\n"
        f"- seal_type: 印章类型{seal_hint}\n"
        f"- reason: 文件用途/用印事由\n"
        f"- usage_method: 盖章或外带{usage_hint}\n"
        f"- lawyer_reviewed: 律师是否已审核{lawyer_hint}，若用户未提及则必须列为缺失\n"
        f"- remarks: 备注(如果有)\n"
        f"只返回JSON。若用户未提及某选项字段，使用默认值。"
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
        send_message(open_id, hint)
        return True

    all_fields = {**doc_fields, **user_fields}
    all_fields.setdefault("usage_method", "盖章")
    all_fields.setdefault("document_count", "1")

    missing = []
    for key in ["company", "seal_type", "reason", "lawyer_reviewed"]:
        if not all_fields.get(key):
            missing.append(FIELD_LABELS.get(key, key))

    if missing:
        with _state_lock:
            entry = PENDING_SEAL.get(open_id)
            if not entry:
                return True  # 已被清理，静默退出
            retry_count = entry.get("retry_count", 0) + 1
            entry["retry_count"] = retry_count
            entry["doc_fields"] = all_fields
        hint = f"还缺少：{'、'.join(missing)}\n请补充。"
        if retry_count >= 3:
            hint += "\n（若需放弃，可回复「取消」）"
        send_message(open_id, hint)
        return True

    _do_create_seal(open_id, user_id, all_fields, file_code)
    return True


def _infer_invoice_file_type(file_name, ai_fields):
    """
    根据文件名和 AI 识别内容判断是结算单还是合同。
    返回 "settlement" | "contract"
    """
    base_name = (file_name.rsplit(".", 1)[0] if "." in file_name else file_name) or ""
    # 文件名关键词
    if any(k in base_name for k in ("结算", "结算单", "对账", "对账单", "月结")):
        return "settlement"
    if any(k in base_name for k in ("合同", "协议")):
        return "contract"
    # 根据内容推断：结算单通常有金额、结算单编号；合同通常有购方、税号
    has_amount = bool(ai_fields.get("amount"))
    has_settlement_no = bool(ai_fields.get("settlement_no"))
    has_buyer = bool(ai_fields.get("buyer_name"))
    has_tax_id = bool(ai_fields.get("tax_id"))
    has_contract_no = bool(ai_fields.get("contract_no"))
    if (has_amount or has_settlement_no) and not (has_buyer or has_tax_id):
        return "settlement"
    if (has_buyer or has_tax_id or has_contract_no):
        return "contract"
    # 默认：有金额倾向结算单，否则倾向合同
    return "settlement" if has_amount else "contract"


def _handle_invoice_file(open_id, user_id, message_id, content_json):
    """处理开票申请的文件上传：根据文件名和内容自动识别是结算单还是合同，支持任意上传顺序"""
    with _state_lock:
        pending = PENDING_INVOICE.get(open_id)
        if not pending:
            return
        step = pending.get("step", "")
        doc_fields = dict(pending.get("doc_fields", {}))
        has_settlement = bool(pending.get("settlement_file_code"))
        has_contract = bool(pending.get("contract_file_code"))
    if step == "user_fields":
        send_message(open_id, "已收到结算单和合同，请补充发票类型和开票项目（如：增值税专用发票 广告费）。")
        return
    file_key = content_json.get("file_key", "")
    file_name = content_json.get("file_name", "未知文件")
    if not file_key:
        send_message(open_id, "无法获取文件，请重新发送。")
        return

    send_message(open_id, f"正在处理文件「{file_name}」，请稍候...")
    file_content, dl_err = download_message_file(message_id, file_key, "file")
    if not file_content:
        err_detail = f"（{dl_err}）" if dl_err else ""
        send_message(open_id, f"文件下载失败，请重新发送。{err_detail}".strip())
        return

    file_code, upload_err = upload_approval_file(file_name, file_content)
    if not file_code:
        err_detail = f"（{upload_err}）" if upload_err else ""
        send_message(open_id, f"文件上传失败，请重新发送。{err_detail}")
        return

    extractor = get_file_extractor("开票申请")
    ai_fields = extractor(file_content, file_name, {}, get_token) if extractor else {}
    if ai_fields:
        logger.info("开票文件识别结果: %s", ai_fields)

    for k, v in ai_fields.items():
        if v and str(v).strip():
            doc_fields[k] = v

    file_type = _infer_invoice_file_type(file_name, ai_fields)
    with _state_lock:
        pending = PENDING_INVOICE.get(open_id)
        if not pending:
            return
        if file_type == "settlement":
            pending["settlement_file_code"] = file_code
        else:
            pending["contract_file_code"] = file_code
        pending["doc_fields"] = doc_fields
        has_settlement = bool(pending.get("settlement_file_code"))
        has_contract = bool(pending.get("contract_file_code"))
        if has_settlement and has_contract:
            pending["step"] = "user_fields"

    type_label = "结算单" if file_type == "settlement" else "合同"
    if has_settlement and has_contract:
        with _state_lock:
            pending = PENDING_INVOICE.get(open_id)
            doc_fields_final = dict(pending.get("doc_fields", {})) if pending else dict(doc_fields)
            settlement_code = pending.get("settlement_file_code") if pending else None
            contract_code = pending.get("contract_file_code") if pending else None
            user_id = pending.get("user_id", "") if pending else user_id
        has_invoice_type = bool(doc_fields_final.get("invoice_type"))
        has_invoice_items = bool(doc_fields_final.get("invoice_items"))
        if has_invoice_type and has_invoice_items:
            _do_create_invoice(open_id, user_id, doc_fields_final, settlement_code, contract_code)
        else:
            summary = "\n".join([f"· {FIELD_LABELS.get(k, k)}: {v}" for k, v in doc_fields.items() if v])
            send_message(open_id, f"已接收{type_label}：{file_name}\n\n已识别：\n{summary or '（无）'}\n\n"
                         "请补充以下必填项（一条消息说完即可）：\n"
                         "1. 发票类型（如：增值税专用发票、普通发票等）\n"
                         "2. 开票项目（如：技术服务费、广告费等）")
    else:
        need = "合同" if not has_contract else "结算单"
        send_message(open_id, f"已接收{type_label}：{file_name}\n请继续上传{need}（Word/PDF/图片均可）。")


def _do_create_invoice(open_id, user_id, all_fields, settlement_code, contract_code):
    """开票申请字段齐全时，发送确认卡片，用户点击确认后创建工单"""
    aid = _get_invoice_attachment_field_ids()
    file_codes = {}
    if aid.get("settlement") and settlement_code:
        file_codes[aid["settlement"]] = [settlement_code]
    if aid.get("contract") and contract_code:
        file_codes[aid["contract"]] = [contract_code]
    if not aid and (settlement_code or contract_code):
        token = get_token()
        cached = get_form_fields("开票申请", APPROVAL_CODES["开票申请"], token)
        attach_ids = [fid for fid, info in (cached or {}).items()
                      if info.get("type") in ("attach", "attachV2", "attachment", "attachmentV2", "file")]
        codes = [c for c in [settlement_code, contract_code] if c]
        for i, fid in enumerate(attach_ids[:2]):
            if i < len(codes):
                file_codes[fid] = [codes[i]]

    admin_comment = get_admin_comment("开票申请", all_fields)
    summary = format_fields_summary(all_fields, "开票申请")
    send_confirm_card(open_id, "开票申请", summary, admin_comment, user_id, all_fields, file_codes=file_codes or None)

    with _state_lock:
        if open_id in PENDING_INVOICE:
            del PENDING_INVOICE[open_id]
        # 不在确认前清空 CONVERSATIONS，等用户点击确认后再清空（见 on_card_action_confirm）

    send_message(open_id, "请确认工单信息无误后，点击卡片上的「确认提交」按钮。")


def _try_complete_invoice(open_id, user_id, text):
    """用户补充发票类型、开票项目后，创建开票申请"""
    with _state_lock:
        pending = PENDING_INVOICE.get(open_id)
    if not pending or pending.get("step") != "user_fields":
        return False

    if _is_cancel_intent(text):
        with _state_lock:
            if open_id in PENDING_INVOICE:
                del PENDING_INVOICE[open_id]
        send_message(open_id, "已取消开票申请，如需办理请重新发起。")
        return True

    doc_fields = pending.get("doc_fields", {})
    settlement_code = pending.get("settlement_file_code")
    contract_code = pending.get("contract_file_code")
    if not settlement_code or not contract_code:
        send_message(open_id, "开票申请需要结算单和合同两个附件，请重新发起并分别上传。")
        return True

    prompt = (
        f"用户为开票申请补充了以下信息：\n{text}\n\n"
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
        send_message(open_id, hint)
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
        send_message(open_id, hint)
        return True

    _do_create_invoice(open_id, user_id, all_fields, settlement_code, contract_code)
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
        with _state_lock:
            last = _user_last_msg.get(open_id, 0)
            if now - last < RATE_LIMIT_SEC:
                send_message(open_id, "操作过于频繁，请稍后再试。")
                return
            _user_last_msg[open_id] = now

        if msg_type == "file":
            with _state_lock:
                is_invoice = open_id in PENDING_INVOICE
            if is_invoice:
                _handle_invoice_file(open_id, user_id, message_id, content_json)
            else:
                _handle_file_message(open_id, user_id, message_id, content_json)
            return

        if msg_type == "image":
            send_message(open_id, "如需用印申请，请发送需要盖章的文件（Word/PDF/图片均可，图片和扫描件将自动识别文字）。\n"
                         "开票申请请先发送文字「开票申请」，再按提示上传结算单和合同。\n"
                         "其他审批请用文字描述。")
            return

        text = content_json.get("text", "").strip()
        if not text:
            send_message(open_id, "请发送文字消息描述您的审批需求。\n如需用印，请先上传需要盖章的文件。")
            return

        with _state_lock:
            in_seal = open_id in PENDING_SEAL
            in_invoice = open_id in PENDING_INVOICE
        if in_seal:
            if _try_complete_seal(open_id, user_id, text):
                return
        if in_invoice:
            if _try_complete_invoice(open_id, user_id, text):
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
            types = "、".join(APPROVAL_CODES.keys())
            reply = unclear if unclear else f"你好！我可以帮你提交以下审批：\n{types}\n\n请告诉我你需要办理哪种？"
            send_message(open_id, reply)
            with _state_lock:
                if open_id in CONVERSATIONS:
                    CONVERSATIONS[open_id].append({"role": "assistant", "content": reply})
            return

        # 第一阶段：处理需特殊路由的类型（用印/开票），收集剩余待处理
        remaining_requests = []
        with _state_lock:
            needs_seal = any(r.get("approval_type") == "用印申请" for r in requests) and open_id not in PENDING_SEAL
            needs_invoice = any(r.get("approval_type") == "开票申请" for r in requests) and open_id not in PENDING_INVOICE

        if needs_seal and needs_invoice:
            req_seal = next(r for r in requests if r.get("approval_type") == "用印申请")
            initial = req_seal.get("fields", {})
            if initial:
                with _state_lock:
                    SEAL_INITIAL_FIELDS[open_id] = {"fields": initial, "created_at": time.time()}
            send_message(open_id, "您同时发起了用印申请和开票申请。请先完成用印申请（上传需要盖章的文件），完成后再发送「开票申请」。\n\n"
                         "请上传需要盖章的文件（Word/PDF/图片均可），我会自动识别内容。")
            with _state_lock:
                if open_id in CONVERSATIONS:
                    CONVERSATIONS[open_id].append({"role": "assistant", "content": "请先完成用印申请"})
            remaining_requests = [r for r in requests if r.get("approval_type") not in ("用印申请", "开票申请")]
            if not remaining_requests:
                return
        else:
            for req in requests:
                at = req.get("approval_type", "")
                miss = req.get("missing", [])
                fields_check = req.get("fields", {})
                if at == "用印申请" and open_id not in PENDING_SEAL:
                    initial = req.get("fields", {})
                    if initial:
                        with _state_lock:
                            SEAL_INITIAL_FIELDS[open_id] = {"fields": initial, "created_at": time.time()}
                    send_message(open_id, "请补充以下信息：\n"
                                 f"用印申请还缺少：上传用章文件\n"
                                 f"请先上传需要盖章的文件（Word/PDF/图片均可），我会自动识别内容。")
                    with _state_lock:
                        if open_id in CONVERSATIONS:
                            CONVERSATIONS[open_id].append({"role": "assistant", "content": "请上传需要盖章的文件"})
                    continue
                if at == "开票申请" and open_id not in PENDING_INVOICE:
                    initial = req.get("fields", {})
                    doc_fields_init = {k: v for k, v in initial.items() if v and str(v).strip()} if initial else {}
                    with _state_lock:
                        PENDING_INVOICE[open_id] = {
                            "step": "need_settlement",
                            "settlement_file_code": None,
                            "contract_file_code": None,
                            "doc_fields": doc_fields_init,
                            "user_id": user_id,
                            "created_at": time.time(),
                        }
                    send_message(open_id, "请补充以下信息：\n"
                                 f"开票申请需要：上传结算单和合同\n"
                                 f"请上传结算单和合同（可任意顺序，Word/PDF/图片均可），我会根据文件名和内容自动识别。")
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
                    f"行政意见: {admin_comment}\n\n"
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
                        f"行政意见: {admin_comment}\n\n"
                        f"该类型暂不支持自动创建，请点击下方按钮在飞书中发起（需在飞书客户端内打开）："
                    )
                    send_card_message(open_id, tip, link, f"打开{approval_type}审批表单")
                    replies.append(f"· {approval_type}：已整理，请点击按钮提交")
                else:
                    send_confirm_card(open_id, approval_type, format_fields_summary(fields, approval_type), admin_comment, user_id, fields)
                    replies.append(f"· {approval_type}：请确认信息后点击卡片按钮提交")

        if incomplete:
            parts = [f"{at}还缺少：{'、'.join([FIELD_LABELS.get(m, m) for m in miss])}" for at, miss in incomplete]
            replies.append("请补充以下信息：\n" + "\n".join(parts))

        if not complete:
            send_message(open_id, "\n".join(replies))
            with _state_lock:
                if open_id in CONVERSATIONS:
                    CONVERSATIONS[open_id].append({"role": "assistant", "content": "请补充信息"})
            return

        header = f"✅ 已处理 {len(complete)} 个申请：\n\n" if len(complete) > 1 else ""
        body = header + "\n\n".join(replies)
        if body.strip():
            send_message(open_id, body)
        with _state_lock:
            if not incomplete and open_id in CONVERSATIONS:
                CONVERSATIONS[open_id] = []

    except Exception as e:
        logger.exception("处理消息出错: %s", e)
        if open_id:
            send_message(open_id, "系统出现异常，请稍后再试。")


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/debug-extract":
            from approval_types import get_file_extractor, FILE_EXTRACTORS
            diag = {
                "extractor_registered": get_file_extractor("用印申请") is not None,
                "invoice_extractor": get_file_extractor("开票申请") is not None,
                "file_extractors": list(FILE_EXTRACTORS.keys()),
                "DEEPSEEK_API_KEY_set": bool(os.environ.get("DEEPSEEK_API_KEY")),
                "DEEPSEEK_API_KEY_len": len(os.environ.get("DEEPSEEK_API_KEY", "")),
                "hint": "若 extractor_registered 为 false 或 DEEPSEEK_API_KEY_set 为 false，则无法识别",
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(diag, ensure_ascii=False, indent=2).encode("utf-8"))
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


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _validate_env()
    threading.Thread(target=_start_health_server, daemon=True).start()

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