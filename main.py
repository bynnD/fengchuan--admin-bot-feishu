import os
import json
import httpx
import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
from approval_config import (
    APPROVAL_CODES, FIELD_LABELS, APPROVAL_FIELD_HINTS,
    LINK_ONLY_TYPES, FIELD_ID_FALLBACK
)
from rules_config import get_admin_comment
from field_cache import get_form_fields, invalidate_cache
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


def send_card_message(open_id, text, url, btn_label):
    card = {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": text}},
            {"tag": "action", "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": btn_label},
                "type": "primary",
                "url": url
            }]}
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


def build_form(approval_type, fields, token):
    """
    根据缓存的字段结构构建表单。
    请假/外出使用特殊控件格式（从真实实例验证的格式）。
    其他类型用通用字段映射。
    """
    approval_code = APPROVAL_CODES[approval_type]

    if approval_type == "请假":
        # 先获取真实的字段结构，找到 leaveGroupV2 控件的真实 ID
        cached_fields = get_form_fields(approval_type, approval_code, token)
        leave_field_id = "widgetLeaveGroupV2"
        if cached_fields:
            # 查找 leaveGroupV2 类型的字段
            for field_id, field_info in cached_fields.items():
                if field_info.get("type") == "leaveGroupV2":
                    leave_field_id = field_id
                    print(f"找到请假控件ID: {leave_field_id}")
                    break
        
        start_date = fields.get("start_date", "")
        end_date = fields.get("end_date", start_date)
        days = fields.get("days", 1)
        days_str = str(days)
        leave_type = fields.get("leave_type", "事假")
        reason = fields.get("reason", "")
        
        # 半天假从中午12点开始，整天从00:00开始
        try:
            is_half_day = float(days) <= 0.5
        except:
            is_half_day = False
        
        # 根据真实字段结构，leaveGroupV2 的 value 应该是一个对象，包含所有子字段的值
        # 子字段包括：widgetLeaveGroupType, widgetLeaveGroupStartTime, widgetLeaveGroupEndTime,
        # widgetLeaveGroupInterval, widgetLeaveGroupUnit, widgetLeaveGroupReason
        
        # 时间格式：根据字段定义是 "YYYY-MM-DD hh:mm"，但实际提交可能需要 ISO 8601 格式
        # 先尝试 ISO 8601 格式
        if is_half_day:
            start_time = f"{start_date}T12:00:00+08:00"
            end_time = f"{end_date}T00:00:00+08:00"
        else:
            start_time = f"{start_date}T00:00:00+08:00"
            end_time = f"{end_date}T23:59:59+08:00"
        
        # 构建 value 对象，键是子字段的 ID
        value_obj = {
            "widgetLeaveGroupType": leave_type,  # 假期类型（radioV2 的值）
            "widgetLeaveGroupStartTime": start_time,  # 开始时间
            "widgetLeaveGroupEndTime": end_time,  # 结束时间
            "widgetLeaveGroupInterval": days_str,  # 时长（radioV2 的值）
            "widgetLeaveGroupUnit": "DAY",  # 请假单位：DAY 或 HOUR（radioV2 的值）
            "widgetLeaveGroupReason": reason  # 请假事由（textarea 的值）
        }
        
        return [{
            "id": leave_field_id,
            "type": "leaveGroupV2",
            "value": value_obj  # value 必须是对象（map），键是子字段的 ID
        }]

    if approval_type == "外出":
        start = fields.get("start_date", "")
        end = fields.get("end_date", start)
        destination = fields.get("destination", "")
        reason = fields.get("reason", "")
        # value格式来自真实审批实例
        return [{
            "id": "widgetOutGroup",
            "type": "outGroup",
            "value": {
                "end": f"{end}T00:00:00+08:00",
                "start": f"{start}T00:00:00+08:00",
                "reason": f"{destination} {reason}".strip()
            }
        }]

    # 通用类型：优先用兜底字段映射（已验证的字段ID），其次用缓存的字段结构
    fallback = FIELD_ID_FALLBACK.get(approval_type, {})
    if fallback:
        form_list = []
        for logical_key, real_id in fallback.items():
            value = str(fields.get(logical_key, ""))
            form_list.append({"id": real_id, "type": "input", "value": value})
        return form_list

    # 没有兜底映射时，从缓存获取字段结构自动匹配
    cached_fields = get_form_fields(approval_type, approval_code, token)
    if not cached_fields:
        print(f"无法获取{approval_type}的字段结构")
        return None

    form_list = []
    for field_id, field_info in cached_fields.items():
        field_type = field_info.get("type", "input")
        field_name = field_info.get("name", "")
        # 跳过附件、图片、说明类字段
        if field_type in ("attach", "attachV2", "image", "imageV2", "description", "attachmentV2"):
            continue
        # 在 fields 里按 field_id 或 field_name 匹配值
        value = fields.get(field_id) or fields.get(field_name) or ""
        form_list.append({
            "id": field_id,
            "type": field_type if field_type in ("input", "textarea", "date", "number") else "input",
            "value": str(value)
        })

    return form_list


def create_approval(user_id, approval_type, fields):
    approval_code = APPROVAL_CODES[approval_type]
    token = get_token()

    form_list = build_form(approval_type, fields, token)
    if form_list is None:
        return False, "无法构建表单，请检查审批字段配置", {}

    form_data = json.dumps(form_list, ensure_ascii=False)
    print(f"提交表单[{approval_type}]: {form_data}")

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

    success = data.get("code") == 0
    msg = data.get("msg", "")

    # 失败时清除缓存，下次重新获取
    if not success:
        invalidate_cache(approval_type)

    return success, msg, data.get("data", {})


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
            link = f"https://www.feishu.cn/approval/newinstance?approval_code={approval_code}"
            tip = (
                f"已为你整理好{approval_type}信息：\n{summary}\n\n"
                f"行政意见: {admin_comment}\n\n"
                f"请点击下方按钮前往飞书审批页面完成提交："
            )
            send_card_message(open_id, tip, link, f"前往提交{approval_type}申请")
            CONVERSATIONS[open_id] = []
            return

        success, msg, resp_data = create_approval(user_id, approval_type, fields)
        if success:
            instance_code = resp_data.get("instance_code", "")
            reply = (
                f"✅ 已为你提交{approval_type}申请！\n{summary}\n\n"
                f"💡 行政意见: {admin_comment}\n"
                f"等待主管审批即可。"
            )
            send_message(open_id, reply)
            if instance_code:
                link = f"https://www.feishu.cn/approval/instance/detail?instance_code={instance_code}"
                send_card_message(open_id, "点击查看审批详情：", link, "查看审批详情")
            CONVERSATIONS[open_id] = []
        else:
            print(f"创建审批失败: {msg}")
            send_message(open_id, f"提交失败：{msg}\n请稍后重试，或联系行政人员。")

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
    print("飞书审批机器人已启动...")
    ws_client.start()