import os
import json
import httpx
import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
from approval_config import (
    APPROVAL_CODES, FIELD_LABELS, APPROVAL_FIELD_HINTS,
    LINK_ONLY_TYPES, PURCHASE_FIELD_MAP, SEAL_FIELD_MAP,
    APPROVAL_GROUP_WIDGET_IDS, APPROVAL_GROUP_MAPPING_RULES, APPROVAL_FLAT_MAPPING_RULES
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
_approval_definition_cache = {"data": {}, "expires_at": {}}

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


def _fetch_approval_definition(approval_code):
    now = time.time()
    cached = _approval_definition_cache["data"].get(approval_code)
    expires_at = _approval_definition_cache["expires_at"].get(approval_code, 0)
    if cached is not None and now < expires_at:
        return cached
    try:
        token = get_token()
        res = httpx.get(
            f"https://open.feishu.cn/open-apis/approval/v4/approvals/{approval_code}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10
        )
        data = res.json()
        if data.get("code") != 0:
            return None
        payload = data.get("data", {})
        approval = payload.get("approval", payload)
        form_schema = approval.get("form") or payload.get("form")
        if not form_schema:
            return None
        if isinstance(form_schema, str):
            form_schema = json.loads(form_schema)
        _approval_definition_cache["data"][approval_code] = form_schema
        _approval_definition_cache["expires_at"][approval_code] = now + 600
        return form_schema
    except Exception:
        return None


def _normalize_text(value):
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ["text", "content", "name", "label", "title", "value"]:
            if key in value:
                return _normalize_text(value[key])
        return ""
    if isinstance(value, list):
        return " ".join([_normalize_text(v) for v in value if _normalize_text(v)])
    return str(value)


def _collect_widgets(node, widgets):
    if isinstance(node, dict):
        if "id" in node and "type" in node:
            widgets.append(node)
        for v in node.values():
            _collect_widgets(v, widgets)
    elif isinstance(node, list):
        for item in node:
            _collect_widgets(item, widgets)


def _extract_widget_label(widget):
    for key in ["name", "label", "title", "text", "display_name", "desc", "placeholder"]:
        if key in widget:
            label = _normalize_text(widget.get(key)).strip()
            if label:
                return label
    return ""


def _get_child_widgets(group_widget):
    for key in ["children", "sub_widgets", "widgets", "items", "fields", "controls", "elements"]:
        val = group_widget.get(key)
        if isinstance(val, list) and any(isinstance(i, dict) and "id" in i for i in val):
            return val
    widgets = []
    _collect_widgets(group_widget, widgets)
    group_id = group_widget.get("id")
    return [w for w in widgets if w is not group_widget and w.get("id") != group_id]


def _build_label_mapping(child_widgets, mapping_rules):
    mapping = {}
    for widget in child_widgets:
        label = _extract_widget_label(widget)
        if not label:
            continue
        for logical_key, keywords in mapping_rules.items():
            if logical_key in mapping:
                continue
            if any(k in label for k in keywords):
                mapping[logical_key] = widget
    return mapping


def _extract_options(widget):
    for key in ["options", "option", "select_options", "values", "items"]:
        val = widget.get(key)
        if isinstance(val, list) and val and isinstance(val[0], dict):
            return val
    return []


def _match_option_id(options, desired):
    for opt in options:
        label = _normalize_text(
            opt.get("name") or opt.get("label") or opt.get("text") or opt.get("value") or opt.get("title")
        )
        if not label:
            continue
        if desired == label or desired in label:
            return opt.get("id") or opt.get("value") or opt.get("key")
    return None


def _format_widget_value(widget, logical_key, value):
    if value is None:
        return ""
    widget_type = str(widget.get("type", "")).lower()
    if logical_key in ["start_date", "end_date"] or logical_key.endswith("_date") or widget_type in ["date", "datetime", "date_time"]:
        return f"{value}T00:00:00+08:00"
    if logical_key == "days":
        return str(value)
    options = _extract_options(widget)
    option_id = _match_option_id(options, str(value)) if options else None
    if option_id is not None:
        return option_id
    return str(value)


def _build_group_value_from_definition(approval_code, group_widget_id, field_values, mapping_rules, approval_type):
    form_schema = _fetch_approval_definition(approval_code)
    if not form_schema:
        return None
    widgets = []
    _collect_widgets(form_schema, widgets)
    group_widget = next((w for w in widgets if w.get("id") == group_widget_id), None)
    if not group_widget:
        return None
    child_widgets = _get_child_widgets(group_widget)
    if not child_widgets:
        return None
    mapping = _build_label_mapping(child_widgets, mapping_rules)
    if not mapping:
        return None
    if approval_type == "外出" and "destination" not in mapping and "reason" in mapping:
        combined = f"{field_values.get('destination', '')} {field_values.get('reason', '')}".strip()
        field_values = dict(field_values)
        field_values["reason"] = combined
    group_value = {}
    for logical_key, widget in mapping.items():
        if logical_key not in field_values:
            continue
        value = field_values.get(logical_key, "")
        if value == "" and logical_key not in ["reason"]:
            continue
        widget_id = widget.get("id")
        if not widget_id:
            continue
        group_value[widget_id] = _format_widget_value(widget, logical_key, value)
    return group_value or None


def _build_flat_form_from_definition(approval_code, field_values, mapping_rules):
    form_schema = _fetch_approval_definition(approval_code)
    if not form_schema:
        return None
    widgets = []
    _collect_widgets(form_schema, widgets)
    leaf_widgets = []
    for widget in widgets:
        if not widget.get("id") or not widget.get("type"):
            continue
        if _get_child_widgets(widget):
            continue
        leaf_widgets.append(widget)
    mapping = _build_label_mapping(leaf_widgets, mapping_rules)
    if not mapping:
        return None
    form_list = []
    for logical_key, widget in mapping.items():
        if logical_key not in field_values:
            continue
        value = field_values.get(logical_key, "")
        if value == "" and logical_key not in ["reason"]:
            continue
        widget_id = widget.get("id")
        widget_type = widget.get("type", "input")
        form_list.append({
            "id": widget_id,
            "type": widget_type,
            "value": _format_widget_value(widget, logical_key, value)
        })
    return form_list or None


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
        leave_field_values = {
            "start_date": start,
            "end_date": end,
            "days": days,
            "leave_type": leave_type,
            "reason": reason
        }
        leave_mapping_rules = APPROVAL_GROUP_MAPPING_RULES.get(approval_type, {})
        group_widget_id = APPROVAL_GROUP_WIDGET_IDS.get(approval_type, "widgetLeaveGroupV2")
        group_value = _build_group_value_from_definition(
            approval_code,
            group_widget_id,
            leave_field_values,
            leave_mapping_rules,
            approval_type
        )
        form_list = [{
            "id": group_widget_id,
            "type": "leaveGroupV2",
            "value": group_value if group_value else {
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
        out_field_values = {
            "start_date": start,
            "end_date": end,
            "destination": destination,
            "reason": reason
        }
        out_mapping_rules = APPROVAL_GROUP_MAPPING_RULES.get(approval_type, {})
        group_widget_id = APPROVAL_GROUP_WIDGET_IDS.get(approval_type, "widgetOutGroup")
        group_value = _build_group_value_from_definition(
            approval_code,
            group_widget_id,
            out_field_values,
            out_mapping_rules,
            approval_type
        )
        form_list = [{
            "id": group_widget_id,
            "type": "outGroup",
            "value": group_value if group_value else {
                "end": f"{end}T00:00:00+08:00",
                "start": f"{start}T00:00:00+08:00",
                "reason": f"{destination} {reason}".strip()
            }
        }]

    elif approval_type == "采购申请":
        purchase_mapping_rules = APPROVAL_FLAT_MAPPING_RULES.get(approval_type, {})
        purchase_form_list = _build_flat_form_from_definition(
            approval_code,
            fields,
            purchase_mapping_rules
        )
        if purchase_form_list:
            form_list = purchase_form_list
        else:
            field_map = PURCHASE_FIELD_MAP
            form_list = []
            for logical_key, real_id in field_map.items():
                value = str(fields.get(logical_key, ""))
                form_list.append({"id": real_id, "type": "input", "value": value})

    elif approval_type == "用印申请":
        seal_mapping_rules = APPROVAL_FLAT_MAPPING_RULES.get(approval_type, {})
        seal_form_list = _build_flat_form_from_definition(
            approval_code,
            fields,
            seal_mapping_rules
        )
        if seal_form_list:
            form_list = seal_form_list
        else:
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
