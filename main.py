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

def send_link_message(open_id, text, url, approval_type):
    card = {
        "config": {"wide_screen_mode": True},
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": text}
            },
            {
                "tag": "action",
                "actions": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": f"前往提交{approval_type}申请"},
                    "type": "primary",
                    "url": url
                }]
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
    return f"https://applink.feishu.cn/client/approval/newinstance?approval_code={approval_code}"

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
        f"2. '明天'、'后天'、'下周一'等换算成具体日期(YYYY-MM-DD)\n"
        f"3. '两个小时'、'半天'等时长，days填0.5，start_date和end_date填同一天\n"
        f"4. '去看病'、'身体不舒服'等明显是病假，leave_type直接填'病假'\n"
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

    if approval_type == "采购申请":
        field_map = PURCHASE_FIELD_MAP
    elif approval_type == "用印申请":
        field_map = SEAL_FIELD_MAP
    else:
        return False, "不支持API提交", {}

    form_list = []
    for logical_key, real_id in field_map.items(
