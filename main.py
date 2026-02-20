import os
import json
import httpx
import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
from approval_config import (
    APPROVAL_CODES, FIELD_LABELS, APPROVAL_FIELD_HINTS,
    LINK_ONLY_TYPES, PURCHASE_FIELD_MAP, SEAL_FIELD_MAP
)
from rules_config import get_admin_comment
import datetime
import time

FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

PROCESSED_EVENTS = set()
CONVERSATIONS = {}
_token_cache = {"token": None, "expires_at": 0}

client = lark.Client.builder() \
    .app_id(FEISHU_APP_ID) \
    .app_secret(FEISHU_APP_SECRET) \
    .build()


def get_token():
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]
    res = httpx.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        timeout=10
    )
    data = res.json()
    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expires_at"] = now + data.get("expire", 7200)
    return _token_cache["token"]


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


def send_card_message(open_id, text, url, approval_type):
    card = {
        "config": {"wide_screen_mode": True},
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": text}
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": f"前往提交{approval_type}申请"},
                        "type": "primary",
                        "url": url
                    }
                ]
            }
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
        print(f"发送卡片消息失败: {resp.msg}")


def build_approval_link(approval_code):
    return f"https://www.feishu.cn/approval/newinstance?approval_code={approval_code}&from=bot"


def analyze_message(history):
    approval_list = "\n".join([f"- {k}" for k in APPROVAL_CODES.keys()])
    field_hints = "\n".join([f"{k}: {v}" for k, v in APPROVAL_FIELD_HINTS.items()])
    today = datetime.date.today()
    system_prompt = (
        f"你是一个行政助理，帮员工提交审批申请。今天是{today}。\n"
        f"可处理的审批类型：\n{approval_list}\n\n"
        f"各类型需要的字段：\n{field_hints}\n\n"
        f"重要规则：\n"
        f"1. 尽量从用户消息中推算字段，不要轻易列为missing\n"
        f"2. 明天、后天、下周一等换算成具体日期(YYYY-MM-DD)\n"
        f"3. 两个小时、半天等时长，days填0.5，start_date和end_date填同一天\n"
        f"4. 去看病、身体不舒服等明显是病假，leave_type直接填病假\n"
        f"5. 只有真的无法推断的字段才放入missing\n"
        f"6. reason可根据上下文推断，实在没有才列为missing\n\n"
        f"返回JSON：\n"
        f"- approval_type: 审批类型（从列表选，无法判断填null）\n"
        f"- fields: 已提取的字段键值对\n"
        f"- missing: 真正缺少的字段名列表\n"
        f"- unclear: 无法判断类型时用中文说明\n"
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
        return json.loads(res.json()["choices"][0]["message"]["content"])
    except Exception as e:
        print(f"AI分析失败: {e}")
        return {"approval_type": None, "unclear": "AI助手暂时无法响应，请稍后再试。"}


def create_approval_api(user_id, approval_type, fields, admin_comment):
    approval_code = APPROVAL_CODES[approval_type]

    if approval_type == "请假":
        import calendar
        start = fields.get("start_date", "")
        end = fields.get("end_date", start)
        days = str(fields.get("days", "1"))
        leave_type = fields.get("leave_type", "事假")
        reason = fields.get("reason", "")
        form_list = [{
            "id": "widgetLeaveGroupV2",
            "type": "group",
            "value": {
                "end": f"{end}T00:00:00+08:00",
                "start": f"{start}T00:00:00+08:00",
                "interval": days,
                "name": leave_type,
                "reason": reason
            }
        }]

    elif approval_type == "外出":
        start = fields.get("start_date", "")
        end = fields.get("end_date", start)
        reason = fields.get("reason", "")
        destination = fields.get("destination", "")
        form_list = [{
            "id": "widgetOutGroup",
            "type": "group",
            "value": {
                "end": f"{end}T00:00:00+08:00",
                "start": f"{start}T00:00:00+08:00",
                "reason": f"{destination} {reason}".strip()
            }
        }]

    elif approval_type == "采购申请":
        field_map = PURCHASE_FIELD_MAP
        form_list = []
        for logical_key, real_id in field_map.items():
            value = str(fields.get(logical_key, ""))
            form_list.append({"id": real_id, "type": "input", "value": value})

    elif approval_type == "用印申请":
        field_map = SEAL_FIELD_MAP
        form_list = []
        for logical_key, real_id in field_map.items():
            value = str(fields.get(logical_key, ""))
            form_list.append({"id": real_id, "type": "input", "value": value})

    else:
        return False, "不支持API提交", {}

    form_data = json.dumps(form_list, ensure_ascii=False)
    print(f"提交表单[{approval_type}]: {form_data}")

    token = get_token()
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
    print(f"创建审批响应: {data}")
    return data.get("code") == 0, data.get("msg", ""), data.get("data", {})


def format_fields_summary(fields):
    lines = []
    for k, v in fields.items():
        label = FIELD_LABELS.get(k, k)
        lines.append(f"· {label}: {v}")
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
        summary = format_fields_summary(fields)

        if approval_type in LINK_ONLY_TYPES:
            approval_code = APPROVAL_CODES[approval_type]
            link = build_approval_link(approval_code)
            tip = (
                f"已为你整理好{approval_type}信息：\n{summary}\n\n"
                f"行政意见: {admin_comment}\n\n"
                f"请点击下方按钮，在飞书客户端中打开审批表单完成提交："
            )
            send_card_message(open_id, tip, link, approval_type)
            CONVERSATIONS[open_id] = []
        else:
            success, msg, _ = create_approval_api(user_id, approval_type, fields, admin_comment)
            if success:
                reply = (
                    f"已为你提交{approval_type}申请！\n{summary}\n\n"
                    f"行政意见: {admin_comment}\n"
                    f"等待主管审批即可。"
                )
                send_message(open_id, reply)
                CONVERSATIONS[open_id] = []
            else:
                print(f"创建审批失败: {msg}")
                send_message(open_id, f"提交失败：{msg}")

    except Exception as e:
        print(f"处理消息出错: {e}")
        if open_id:
            send_message(open_id, "系统出现异常，请稍后再试。")


if __name__ == "__main__":
    # 启动时读取已有请假审批实例的表单数据
    try:
        token = get_token()
        r = httpx.get(
            "https://open.feishu.cn/open-apis/approval/v4/instances/4C9BA264-1169-4821-80AF-397AED46D5E2",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10
        )
        print("请假实例数据:", json.dumps(r.json(), ensure_ascii=False))
    except Exception as e:
        print(f"读取实例失败: {e}")
    handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(on_message) \
        .build()
    ws_client = lark.ws.Client(
        FEISHU_APP_ID,
        FEISHU_APP_SECRET,
        event_handler=handler,
        log_level=lark.LogLevel.INFO
    )
    print("飞书审批机器人已启动...")
    ws_client.start()
