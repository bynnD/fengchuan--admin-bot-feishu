import os
import json
import httpx
import lark_oapi as lark
from lark_oapi.api.im.v1 import *
from lark_oapi.api.approval.v4 import *
from approval_config import APPROVAL_CODES, FIELD_LABELS, APPROVAL_FIELD_HINTS
from rules_config import get_admin_comment

FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

PROCESSED_EVENTS = set()
CONVERSATIONS = {}

client = lark.Client.builder() \
    .app_id(FEISHU_APP_ID) \
    .app_secret(FEISHU_APP_SECRET) \
    .build()

def send_message(open_id, text):
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
        print(f"发送消息失败: {resp.msg}")

def analyze_message(history):
    approval_list = "\n".join([f"- {k}" for k in APPROVAL_CODES.keys()])
    field_hints = "\n".join([f"{k}: {v}" for k, v in APPROVAL_FIELD_HINTS.items()])
    system_prompt = (
        f"你是一个行政助理，帮员工提交审批申请。今天是{__import__('datetime').date.today()}。\n"
        f"可处理的审批类型：\n{approval_list}\n\n"
        f"各类型需要的字段：\n{field_hints}\n\n"
        f"重要规则：\n"
        f"1. 尽量从用户消息中推算字段，不要轻易列为missing\n"
        f"2. '明天'、'后天'、'下周一'等要换算成具体日期(YYYY-MM-DD)\n"
        f"3. '两个小时'、'半天'等时长，days填0.5或按实际换算，start_date和end_date填同一天\n"
        f"4. '去看病'、'身体不舒服'等明显是病假，leave_type直接填'病假'\n"
        f"5. 只有真的无法推断的字段才放入missing\n"
        f"6. reason如果用户没说可以根据上下文推断，实在没有才列为missing\n\n"
        f"分析对话历史，返回JSON：\n"
        f"- approval_type: 审批类型（从列表选，无法判断填null）\n"
        f"- fields: 综合对话历史已提取的字段键值对\n"
        f"- missing: 真正缺少且无法推断的字段名列表\n"
        f"- unclear: 无法判断类型时用中文说明需要补充什么\n"
        f"只返回JSON。"
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
        data = res.json()
        print(f"DeepSeek响应: {data}")
        return json.loads(data["choices"][0]["message"]["content"])
    except Exception as e:
        print(f"AI分析失败: {e}")
        return {"approval_type": None, "unclear": "AI助手暂时无法响应，请稍后再试。"}

def create_approval(user_id, approval_type, fields, admin_comment):
    approval_code = APPROVAL_CODES[approval_type]
    all_fields = dict(fields)
    all_fields["admin_comment"] = admin_comment
    form_data = json.dumps(
        [{"id": k, "type": "input", "value": str(v)} for k, v in all_fields.items()],
        ensure_ascii=False
    )
    body = CreateInstanceRequestBody.builder() \
        .approval_code(approval_code) \
        .user_id(user_id) \
        .form(form_data) \
        .build()
    request = CreateInstanceRequest.builder() \
        .request_body(body) \
        .build()
    return client.approval.v4.instance.create(request)

def format_success_message(approval_type, fields, admin_comment):
    lines = [f"✅ 已为你提交{approval_type}申请！"]
    for k, v in fields.items():
        label = FIELD_LABELS.get(k, k)
        lines.append(f"· {label}: {v}")
    lines.append(f"\n💡 行政意见: {admin_comment}")
    lines.append("等待主管审批即可。")
    return "\n".join(lines)

def on_message(data):
    event_id = data.header.event_id
    if event_id in PROCESSED_EVENTS:
        return
    PROCESSED_EVENTS.add(event_id)

    open_id = None
    try:
        event = data.event
        open_id = event.sender.sender_id.open_id
        user_id = event.sender.sender_id.user_id
        text = json.loads(event.message.content).get("text", "").strip()

        if open_id not in CONVERSATIONS:
            CONVERSATIONS[open_id] = []
        CONVERSATIONS[open_id].append({"role": "user", "content": text})
        if len(CONVERSATIONS[open_id]) > 10:
            CONVERSATIONS[open_id] = CONVERSATIONS[open_id][-10:]

        result = analyze_message(CONVERSATIONS[open_id])
        approval_type = result.get("approval_type")
        fields = result.get("fields", {})
        missing = result.get("missing", [])
        unclear = result.get("unclear", "")

        if not approval_type:
            types = "、".join(APPROVAL_CODES.keys())
            reply = unclear if unclear else f"你好！我可以帮你提交以下审批：\n{types}\n\n请告诉我你需要办理哪种？"
            send_message(open_id, reply)
            CONVERSATIONS[open_id].append({"role": "assistant", "content": reply})
            return

        if missing:
            missing_text = "、".join([FIELD_LABELS.get(m, m) for m in missing])
            reply = f"还需要以下信息才能提交{approval_type}申请：\n{missing_text}"
            send_message(open_id, reply)
            CONVERSATIONS[open_id].append({"role": "assistant", "content": reply})
            return

        admin_comment = get_admin_comment(approval_type, fields)
        resp = create_approval(user_id, approval_type, fields, admin_comment)
        if resp.success():
            reply = format_success_message(approval_type, fields, admin_comment)
            send_message(open_id, reply)
            CONVERSATIONS[open_id] = []
        else:
            print(f"创建审批失败: {resp.msg}")
            send_message(open_id, f"提交失败，错误信息：{resp.msg}")

    except Exception as e:
        print(f"处理消息出错: {e}")
        if open_id:
            send_message(open_id, "系统出现异常，请稍后再试。")

if __name__ == "__main__":
    handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(on_message) \
        .build()
    ws_client = lark.ws.Client(
        FEISHU_APP_ID,
        FEISHU_APP_SECRET,
        event_handler=handler,
        log_level=lark.LogLevel.INFO
    )
    print("🚀 飞书审批机器人已启动...")
    ws_client.start()
