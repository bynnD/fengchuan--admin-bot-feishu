import os
import json
import httpx
import lark_oapi as lark
import time
import importlib

def _resolve_class(paths):
    for mod, cls in paths:
        try:
            m = importlib.import_module(mod)
            c = getattr(m, cls, None)
            if c:
                return c
        except Exception:
            continue
    return None

# 兼容不同版本 lark-oapi 的导出路径
CreateMessageRequestBody = _resolve_class([
    ("lark_oapi.api.im.v1", "CreateMessageRequestBody"),
    ("lark_oapi.api.im.v1.model.message", "CreateMessageRequestBody"),
    ("lark_oapi.api.im.v1.message", "CreateMessageRequestBody"),
])
CreateMessageRequest = _resolve_class([
    ("lark_oapi.api.im.v1", "CreateMessageRequest"),
    ("lark_oapi.api.im.v1.model.message", "CreateMessageRequest"),
    ("lark_oapi.api.im.v1.message", "CreateMessageRequest"),
])
CreateInstanceRequestBody = _resolve_class([
    ("lark_oapi.api.approval.v4", "CreateInstanceRequestBody"),
    ("lark_oapi.api.approval.v4.model.instance", "CreateInstanceRequestBody"),
    ("lark_oapi.api.approval.v4.instance", "CreateInstanceRequestBody"),
])
CreateInstanceRequest = _resolve_class([
    ("lark_oapi.api.approval.v4", "CreateInstanceRequest"),
    ("lark_oapi.api.approval.v4.model.instance", "CreateInstanceRequest"),
    ("lark_oapi.api.approval.v4.instance", "CreateInstanceRequest"),
])
from approval_config import APPROVAL_CODES, APPROVAL_FIELDS, FIELD_LABELS, APPROVAL_FIELD_HINTS
from rules_config import validate_approval

# 配置环境变量
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

# 内存去重：防止一条消息回复两次
PROCESSED_EVENTS = set()

# 会话上下文：{open_id: [{"role": "user", "content": "..."}, ...]}
CONVERSATIONS = {}
MAX_HISTORY_LEN = 10  # 保留最近10条记录

TENANT_ACCESS_TOKEN = None
TENANT_ACCESS_TOKEN_EXPIRES_AT = 0
APPROVAL_DEFINITION_CACHE = {}
APPROVAL_DEFINITION_TTL = 1800

client = lark.Client.builder() \
    .app_id(FEISHU_APP_ID) \
    .app_secret(FEISHU_APP_SECRET) \
    .build()

def send_message(open_id, text):
    if CreateMessageRequestBody and CreateMessageRequest:
        body = CreateMessageRequestBody.builder() \
            .receive_id(open_id) \
            .msg_type("text") \
            .content(json.dumps({"text": text})) \
            .build()
        request = CreateMessageRequest.builder() \
            .receive_id_type("open_id") \
            .request_body(body) \
            .build()
        resp = client.im.v1.message.create(request)
        if not resp.success():
            print(f"发送消息失败: {resp.msg}")
        return text
    token = get_tenant_access_token()
    if not token:
        print("发送消息失败: 未获取到租户令牌")
        return text
    try:
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
        payload = {
            "receive_id": open_id,
            "content": json.dumps({"text": text}),
            "msg_type": "text"
        }
        res = httpx.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=10)
        res.raise_for_status()
    except Exception as e:
        print(f"发送消息失败: {e}")
    return text

def send_link_message(open_id, text, link_url):
    content = {
        "zh_cn": {
            "title": "请点击链接办理",
            "content": [
                [
                    {"tag": "text", "text": text + " "},
                    {"tag": "a", "text": "点击这里前往办理", "href": link_url}
                ]
            ]
        }
    }
    if CreateMessageRequestBody and CreateMessageRequest:
        body = CreateMessageRequestBody.builder() \
            .receive_id(open_id) \
            .msg_type("post") \
            .content(json.dumps(content)) \
            .build()
        request = CreateMessageRequest.builder() \
            .receive_id_type("open_id") \
            .request_body(body) \
            .build()
        client.im.v1.message.create(request)
        return text
    token = get_tenant_access_token()
    if not token:
        print("发送链接消息失败: 未获取到租户令牌")
        return text
    try:
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
        payload = {
            "receive_id": open_id,
            "content": json.dumps(content),
            "msg_type": "post"
        }
        res = httpx.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=10)
        res.raise_for_status()
    except Exception as e:
        print(f"发送链接消息失败: {e}")
    return text

def get_tenant_access_token():
    global TENANT_ACCESS_TOKEN, TENANT_ACCESS_TOKEN_EXPIRES_AT
    now = time.time()
    if TENANT_ACCESS_TOKEN and now < TENANT_ACCESS_TOKEN_EXPIRES_AT - 60:
        return TENANT_ACCESS_TOKEN
    try:
        res = httpx.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal/",
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
            timeout=10
        )
        res.raise_for_status()
        data = res.json()
        if data.get("code") == 0:
            TENANT_ACCESS_TOKEN = data.get("tenant_access_token")
            TENANT_ACCESS_TOKEN_EXPIRES_AT = now + int(data.get("expire", 0))
            return TENANT_ACCESS_TOKEN
        print(f"获取 tenant_access_token 失败: {data}")
        return None
    except Exception as e:
        print(f"获取 tenant_access_token 异常: {e}")
        return None

def fetch_approval_definition(approval_code):
    now = time.time()
    cached = APPROVAL_DEFINITION_CACHE.get(approval_code)
    if cached and now - cached["ts"] < APPROVAL_DEFINITION_TTL:
        return cached["data"]
    token = get_tenant_access_token()
    if not token:
        return None
    try:
        url = f"https://open.feishu.cn/open-apis/approval/v4/approvals/{approval_code}"
        res = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        res.raise_for_status()
        payload = res.json()
        if payload.get("code") == 0:
            data = payload.get("data")
            APPROVAL_DEFINITION_CACHE[approval_code] = {"data": data, "ts": now}
            return data
        print(f"获取审批定义失败: {payload}")
        return None
    except Exception as e:
        print(f"获取审批定义异常: {e}")
        return None

def extract_required_attachment_fields(data):
    found = []
    def to_bool(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value == 1
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "y")
        return False
    def visit(obj):
        if isinstance(obj, dict):
            t = str(obj.get("type", "")).lower()
            if t and ("attachment" in t or t in ("file", "files", "image", "imagev2")):
                required = obj.get("required")
                if required is None:
                    required = obj.get("is_required")
                if required is None:
                    required = obj.get("require")
                if to_bool(required):
                    name = obj.get("name") or obj.get("title") or obj.get("label") or obj.get("id")
                    found.append(name or t)
            for v in obj.values():
                visit(v)
        elif isinstance(obj, list):
            for item in obj:
                visit(item)
    visit(data)
    unique = []
    seen = set()
    for item in found:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique

def get_required_attachment_fields(approval_code):
    data = fetch_approval_definition(approval_code)
    if not data:
        return []
    return extract_required_attachment_fields(data)

def build_approval_link(approval_code):
    return f"https://applink.feishu.cn/client/approval/instance/create?approval_code={approval_code}"

def validate_env():
    missing = []
    if not FEISHU_APP_ID:
        missing.append("FEISHU_APP_ID")
    if not FEISHU_APP_SECRET:
        missing.append("FEISHU_APP_SECRET")
    if not DEEPSEEK_API_KEY:
        missing.append("DEEPSEEK_API_KEY")
    if missing:
        print(f"缺少环境变量: {', '.join(missing)}")
        return False
    return True

def analyze_message(history):
    approval_list = "\n".join([f"- {k}" for k in APPROVAL_CODES.keys()])
    field_hints = "\n".join([f"{k}: {v}" for k, v in APPROVAL_FIELD_HINTS.items()])
    
    system_prompt = (
        f"你是一个行政助理，负责帮员工提交审批申请。\n"
        f"可以处理的审批类型：\n{approval_list}\n\n"
        f"各类型需要的字段：\n{field_hints}\n\n"
        f"请分析对话历史，返回JSON：\n"
        f"- approval_type: 审批类型（从列表选，无法判断填null）\n"
        f"- fields: 综合对话历史已提取到的字段键值对\n"
        f"- missing: 缺少的字段名列表\n"
        f"- unclear: 无法判断类型时，用中文说明需要用户补充什么\n\n"
        f"只返回JSON，不要其他内容。"
    )
    
    messages = [{"role": "system", "content": system_prompt}] + history

    try:
        res = httpx.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
            json={
                "model": "deepseek-chat",
                "messages": messages,
                "response_format": {"type": "json_object"}
            },
            timeout=30
        )
        res.raise_for_status()
        content = res.json()["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as e:
        print(f"AI分析失败: {e}")
        return {"approval_type": None, "unclear": "AI 助手暂时无法响应，请稍后再试。"}

def create_approval(user_id, approval_type, fields, admin_comment):
    approval_code = APPROVAL_CODES[approval_type]
    fields["admin_comment"] = admin_comment
    form_data = json.dumps([
        {"id": k, "type": "input", "value": str(v)}
        for k, v in fields.items()
    ])
    if CreateInstanceRequestBody and CreateInstanceRequest:
        body = CreateInstanceRequestBody.builder() \
            .approval_code(approval_code) \
            .user_id(user_id) \
            .form(form_data) \
            .build()
        request = CreateInstanceRequest.builder() \
            .request_body(body) \
            .build()
        return client.approval.v4.instance.create(request)
    token = get_tenant_access_token()
    if not token:
        class Resp: 
            def success(self): return False
            @property
            def msg(self): return "未获取到租户令牌"
        return Resp()
    try:
        url = "https://open.feishu.cn/open-apis/approval/v4/instances/create"
        payload = {
            "approval_code": approval_code,
            "user_id": user_id,
            "form": form_data
        }
        res = httpx.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=10)
        data = res.json()
        class Resp:
            def __init__(self, ok, msg): self._ok=ok; self._msg=msg
            def success(self): return self._ok
            @property
            def msg(self): return self._msg
        ok = res.status_code == 200 and data.get("code") == 0
        msg = "" if ok else data.get("msg", str(data))
        return Resp(ok, msg)
    except Exception as e:
        err_msg = str(e)
        class Resp: 
            def success(self): return False
            @property
            def msg(self): return err_msg
        return Resp()

def format_success_message(approval_type, fields, admin_comment):
    lines = [f"✅ 已为你提交{approval_type}申请!"]
    for k, v in fields.items():
        if k != "admin_comment":
            label = FIELD_LABELS.get(k, k)
            lines.append(f"📍 {label}: {v}")
    lines.append(f"\n💡 行政意见: {admin_comment}")
    lines.append("📢 等待主管审批即可。")
    return "\n".join(lines)

def on_message(data):
    # 去重逻辑
    event_id = data.header.event_id
    if event_id in PROCESSED_EVENTS:
        return
    PROCESSED_EVENTS.add(event_id)

    open_id = None
    try:
        event = data.event
        open_id = event.sender.sender_id.open_id
        user_id = event.sender.sender_id.user_id 
        content = json.loads(event.message.content)
        text = content.get("text", "").strip()

        # 1. 获取并更新历史记录
        if open_id not in CONVERSATIONS:
            CONVERSATIONS[open_id] = []
        
        # 追加用户消息
        CONVERSATIONS[open_id].append({"role": "user", "content": text})
        # 保持长度
        if len(CONVERSATIONS[open_id]) > MAX_HISTORY_LEN:
            CONVERSATIONS[open_id] = CONVERSATIONS[open_id][-MAX_HISTORY_LEN:]

        # 2. 调用 AI 分析
        result = analyze_message(CONVERSATIONS[open_id])
        approval_type = result.get("approval_type")
        fields = result.get("fields", {})
        missing = result.get("missing", [])
        unclear = result.get("unclear", "")

        bot_response = ""

        # 3. 场景处理
        if not approval_type:
            types = "、".join(APPROVAL_CODES.keys())
            bot_response = unclear if unclear else f"你好！我可以帮你提交以下审批：\n{types}\n\n请告诉我你需要办理哪种？"
            send_message(open_id, bot_response)
            # 记录回复
            CONVERSATIONS[open_id].append({"role": "assistant", "content": bot_response})
            return

        approval_code = APPROVAL_CODES.get(approval_type, "")
        required_attachments = get_required_attachment_fields(approval_code) if approval_code else []
        if required_attachments:
            fields_text = "、".join(required_attachments)
            tip = "该审批表单中附件为必填"
            if fields_text:
                tip = f"{tip}（{fields_text}）"
            tip = f"{tip}，请点击链接前往飞书原生审批页面上传并提交。"
            bot_response = send_link_message(open_id, tip, build_approval_link(approval_code))
            CONVERSATIONS[open_id].append({"role": "assistant", "content": bot_response})
            return

        if missing:
            missing_text = "、".join([FIELD_LABELS.get(m, m) for m in missing])
            bot_response = f"📝 还需要以下信息才能提交{approval_type}申请：\n{missing_text}"
            send_message(open_id, bot_response)
            CONVERSATIONS[open_id].append({"role": "assistant", "content": bot_response})
            return

        # 4. 规则校验
        status, message = validate_approval(approval_type, fields)

        if status == "BLOCK":
            # 阻断提交 (如格式错误)
            bot_response = f"❌ 无法提交：{message}"
            send_message(open_id, bot_response)
            CONVERSATIONS[open_id].append({"role": "assistant", "content": bot_response})
            return

        # 5. 提交审批 (PASS 或 WARN)
        # 无论 PASS 还是 WARN，都尝试提交，只是 comment 不同
        resp = create_approval(user_id, approval_type, fields, message)
        
        if resp.success():
            bot_response = format_success_message(approval_type, fields, message)
            send_message(open_id, bot_response)
            # 成功后清空该用户的会话上下文，避免干扰下一次
            CONVERSATIONS[open_id] = []
        else:
            print(f"创建审批失败: {resp.msg}")
            bot_response = f"❌ 提交失败：{resp.msg}"
            send_message(open_id, bot_response)
            CONVERSATIONS[open_id].append({"role": "assistant", "content": bot_response})

    except Exception as e:
        print(f"处理消息出错: {e}")
        if open_id:
            send_message(open_id, "⚠️ 系统出现异常，请检查配置或稍后再试。")

def on_message_read(data):
    return

def on_chat_access_event(data):
    return

def on_reaction_created(data):
    return

def register_if_available(builder, method_name, func):
    method = getattr(builder, method_name, None)
    if method:
        return method(func)
    return builder

def register_any(builder, method_names, func):
    current = builder
    for name in method_names:
        current = register_if_available(current, name, func)
    return current

def register_event_type(builder, method_names, event_type, func):
    current = builder
    for name in method_names:
        method = getattr(current, name, None)
        if not method:
            continue
        try:
            current = method(event_type, func)
            continue
        except TypeError:
            try:
                current = method(event_type=event_type, func=func)
                continue
            except Exception:
                try:
                    current = method(func)
                    continue
                except Exception:
                    continue
        except Exception:
            continue
    return current

if __name__ == "__main__":
    if not validate_env():
        raise SystemExit(1)
    # 注册处理器
    handler_builder = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(on_message)

    handler_builder = register_any(
        handler_builder,
        ["register_p2_im_message_read_v1", "register_im_message_read_v1", "register_im_message_message_read_v1"],
        on_message_read
    )
    handler_builder = register_any(
        handler_builder,
        ["register_p2_im_chat_access_event_bot_p2p_chat_entered_v1", "register_im_chat_access_event_bot_p2p_chat_entered_v1"],
        on_chat_access_event
    )
    handler_builder = register_any(
        handler_builder,
        ["register_p2_im_message_reaction_created_v1", "register_im_message_reaction_created_v1"],
        on_reaction_created
    )

    handler_builder = register_event_type(
        handler_builder,
        ["register_event_callback", "register_event_handler", "register_callback", "register_event"],
        "im.message.message_read_v1",
        on_message_read
    )

    handler = handler_builder.build()

    # 启动客户端
    ws_client = lark.ws.Client(
        FEISHU_APP_ID,
        FEISHU_APP_SECRET,
        event_handler=handler,
        log_level=lark.LogLevel.INFO
    )
    print("🚀 飞书审批机器人已启动...")
    ws_client.start()
