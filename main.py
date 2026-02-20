import os
import json
import httpx
import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
from approval_config import (
    APPROVAL_CODES, FIELD_LABELS, APPROVAL_FIELD_HINTS,
    LINK_ONLY_TYPES, FIELD_ID_FALLBACK, DATE_FIELDS
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


def _build_leave_form(fields, approval_code, token):
    """
    构建请假表单。leaveGroupV2 的 value 是嵌套 map：
    每个子字段的值本身也是一个 {id, type, value} 对象。
    """
    cached_fields = get_form_fields("请假", approval_code, token)
    leave_field_id = "widgetLeaveGroupV2"
    if cached_fields:
        for fid, finfo in cached_fields.items():
            if finfo.get("type") == "leaveGroupV2":
                leave_field_id = fid
                break

    start_date = fields.get("start_date", "")
    end_date = fields.get("end_date", start_date)
    days = fields.get("days", 1)
    leave_type = fields.get("leave_type", "事假")
    reason = fields.get("reason", "")

    try:
        is_half_day = float(days) <= 0.5
    except (ValueError, TypeError):
        is_half_day = False

    if is_half_day:
        start_time = f"{start_date}T12:00:00+08:00"
        end_time = f"{end_date}T18:00:00+08:00"
    else:
        start_time = f"{start_date}T00:00:00+08:00"
        end_time = f"{end_date}T23:59:00+08:00"

    # 每个子字段值是一个 map，包含 id/type/value
    value_map = {
        "widgetLeaveGroupType": {
            "id": "widgetLeaveGroupType",
            "type": "radioV2",
            "value": leave_type
        },
        "widgetLeaveGroupStartTime": {
            "id": "widgetLeaveGroupStartTime",
            "type": "date",
            "value": start_time
        },
        "widgetLeaveGroupEndTime": {
            "id": "widgetLeaveGroupEndTime",
            "type": "date",
            "value": end_time
        },
        "widgetLeaveGroupInterval": {
            "id": "widgetLeaveGroupInterval",
            "type": "radioV2",
            "value": str(days)
        },
        "widgetLeaveGroupUnit": {
            "id": "widgetLeaveGroupUnit",
            "type": "radioV2",
            "value": "DAY"
        },
        "widgetLeaveGroupReason": {
            "id": "widgetLeaveGroupReason",
            "type": "textarea",
            "value": reason
        },
        "widgetLeaveGroupFeedingArrivingLate": {
            "id": "widgetLeaveGroupFeedingArrivingLate",
            "type": "radioV2",
            "value": "0"
        }
    }

    print(f"请假表单 value_map: {json.dumps(value_map, ensure_ascii=False)}")

    return [{
        "id": leave_field_id,
        "type": "leaveGroupV2",
        "value": value_map
    }]


def _build_out_form(fields, approval_code, token):
    """构建外出表单。outGroup 使用类似的嵌套 map 格式。"""
    cached_fields = get_form_fields("外出", approval_code, token)
    out_field_id = "widgetOutGroup"
    if cached_fields:
        for fid, finfo in cached_fields.items():
            if finfo.get("type") == "outGroup":
                out_field_id = fid
                break

    start = fields.get("start_date", "")
    end = fields.get("end_date", start)
    destination = fields.get("destination", "")
    reason = fields.get("reason", "")

    value_map = {
        "start": f"{start}T00:00:00+08:00",
        "end": f"{end}T00:00:00+08:00",
        "reason": f"{destination} {reason}".strip()
    }

    return [{
        "id": out_field_id,
        "type": "outGroup",
        "value": value_map
    }]


def build_form(approval_type, fields, token):
    """根据审批类型构建表单数据。"""
    approval_code = APPROVAL_CODES[approval_type]

    if approval_type == "请假":
        return _build_leave_form(fields, approval_code, token)

    if approval_type == "外出":
        return _build_out_form(fields, approval_code, token)

    # 通用类型：优先用兜底字段映射（已验证的字段ID），其次用缓存的字段结构
    fallback = FIELD_ID_FALLBACK.get(approval_type, {})
    if fallback:
        cached = get_form_fields(approval_type, approval_code, token)
        form_list = []
        for logical_key, real_id in fallback.items():
            raw = fields.get(logical_key, "")
            if not raw and logical_key != "reason":
                continue
            # 若缓存中有更准确的字段ID（按名称匹配），优先使用
            field_id = real_id
            if cached:
                for fid, finfo in cached.items():
                    if finfo.get("name") in (logical_key, FIELD_LABELS.get(logical_key, "")):
                        field_id = fid
                        break
            if logical_key in DATE_FIELDS and raw:
                value = f"{raw}T00:00:00+08:00" if "T" not in str(raw) else str(raw)
                ftype = "date"
            else:
                value = str(raw)
                ftype = "input"
            form_list.append({"id": field_id, "type": ftype, "value": value})
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
            # 使用飞书 applink 协议，在飞书客户端内直接打开审批发起页面
            link = f"https://applink.feishu.cn/client/approval?tab=create&definitionCode={approval_code}"
            tip = (
                f"已为你整理好{approval_type}信息：\n{summary}\n\n"
                f"行政意见: {admin_comment}\n\n"
                f"请点击下方按钮，在飞书中打开审批表单并提交："
            )
            send_card_message(open_id, tip, link, f"打开{approval_type}审批表单")
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