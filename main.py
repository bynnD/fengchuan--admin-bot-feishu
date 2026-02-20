import os
import json
import httpx
import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
from approval_types import (
    APPROVAL_CODES, FIELD_LABELS, APPROVAL_FIELD_HINTS,
    LINK_ONLY_TYPES, FIELD_ID_FALLBACK, FIELD_ORDER, DATE_FIELDS, FIELD_LABELS_REVERSE,
    IMAGE_SUPPORT_TYPES, get_admin_comment
)
import base64
from field_cache import get_form_fields, invalidate_cache, is_free_process, mark_free_process
import datetime
import time
import traceback
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

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


def download_image(message_id, image_key):
    """从飞书下载图片，返回 base64 编码"""
    try:
        token = get_token()
        res = httpx.get(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{image_key}",
            params={"type": "image"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15
        )
        if res.status_code == 200:
            return base64.b64encode(res.content).decode("utf-8")
        print(f"下载图片失败: status={res.status_code}")
    except Exception as e:
        print(f"下载图片异常: {e}")
    return None


def ocr_image(image_base64):
    """调用飞书 OCR 识别图片文字"""
    try:
        token = get_token()
        res = httpx.post(
            "https://open.feishu.cn/open-apis/optical_char_recognition/v1/image/basic_recognize",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"image": image_base64},
            timeout=15
        )
        data = res.json()
        if data.get("code") == 0:
            lines = data.get("data", {}).get("text_list", [])
            return "\n".join(lines)
        print(f"OCR 失败: {data.get('msg')}")
    except Exception as e:
        print(f"OCR 异常: {e}")
    return None


def analyze_document_for_seal(ocr_text):
    """根据 OCR 文字提取文档相关字段（文件名称、文件类型、文件数量）"""
    doc_type_options = (
        "主营业务相关(流量服务协议、顾问合同等), "
        "非业务相关(知识产权、项目申报、认证等), "
        "资产服务类(采购合同、租赁合同、承包合同等), "
        "工商财务类(银行材料、审计材料、签章等), "
        "人事类(收入证明、在职证明等)"
    )
    prompt = (
        f"你是行政助理，根据以下文档OCR文字，提取文件信息。\n\n"
        f"文档内容：\n{ocr_text[:2000]}\n\n"
        f"请返回JSON，包含：\n"
        f"- document_name: 文件名称/标题（从文档首页识别）\n"
        f"- document_type: 文件类型，从以下选项中选最匹配的：{doc_type_options}\n"
        f"- document_count: 文件份数（默认\"1\"）\n"
        f"只返回JSON。"
    )
    try:
        res = httpx.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"}
            },
            timeout=30
        )
        content = res.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return json.loads(content)
    except Exception as e:
        print(f"文档分析失败: {e}")
        return {}


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
        f"【关键】分析用户最新消息，可能包含一个或多个审批需求，分别识别并提取。"
        f"例如「我要采购笔记本，还要给合同盖章」= 采购申请 + 用印申请。"
        f"每个需求单独列出，每个需求的 fields 和 missing 独立。\n\n"
        f"重要规则：\n"
        f"1. 尽量从用户消息中推算字段，不要轻易列为missing\n"
        f"2. 明天、后天、下周一等换算成具体日期(YYYY-MM-DD)\n"
        f"3. 只有真的无法推断的字段才放入missing\n"
        f"4. reason可根据上下文推断，实在没有才列为missing\n"
        f"5. 采购：purchase_reason可包含具体物品，expected_date为期望交付时间\n"
        f"6. 采购的cost_detail是费用明细列表(必填)，每项必须含名称、规格、数量、金额、是否有库存(是/否)。"
        f"多个物品就多项，格式为[{{\"名称\":\"笔记本电脑\",\"规格\":\"ThinkPad X1\",\"数量\":\"1\",\"金额\":\"8000\",\"是否有库存\":\"否\"}}]。"
        f"缺少任何一项信息就把cost_detail列入missing。purchase_reason可从物品信息推断(如'采购笔记本电脑')\n\n"
        f"返回JSON：\n"
        f"- requests: 数组，每项含 approval_type、fields、missing\n"
        f"  若只有1个需求，数组长度为1；若无法识别任何需求，返回空数组\n"
        f"- unclear: 无法判断时用中文说明（requests为空时必填）\n"
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
        content = res.json()["choices"][0]["message"]["content"]
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        raw = json.loads(content)
        if "requests" in raw:
            return raw
        if raw.get("approval_type"):
            return {"requests": [{"approval_type": raw["approval_type"], "fields": raw.get("fields", {}), "missing": raw.get("missing", [])}], "unclear": raw.get("unclear", "")}
        return {"requests": [], "unclear": raw.get("unclear", "无法识别审批类型。")}
    except Exception as e:
        print(f"AI分析失败: {e}")
        traceback.print_exc()
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


def build_form(approval_type, fields, token):
    """根据审批类型构建表单数据。按缓存中的表单顺序，使用真实控件类型。"""
    approval_code = APPROVAL_CODES[approval_type]
    cached = get_form_fields(approval_type, approval_code, token)
    if not cached:
        print(f"无法获取{approval_type}的字段结构")
        return None

    fallback = FIELD_ID_FALLBACK.get(approval_type, {})
    name_to_key = {v: k for k, v in FIELD_LABELS.items()}
    name_to_key.update({k: k for k in FIELD_LABELS})

    used_keys = set()
    form_list = []
    for field_id, field_info in cached.items():
        field_type = field_info.get("type", "input")
        field_name = field_info.get("name", "")
        if field_type in ("attach", "attachV2", "image", "imageV2", "description", "attachmentV2"):
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
        value = _format_field_value(logical_key, raw, field_type, field_info)
        ftype = field_type if field_type in ("input", "textarea", "date", "number", "radioV2", "fieldList", "checkboxV2") else "input"
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


def _form_summary(form_list, cached):
    """根据实际提交的表单和缓存的字段名生成摘要"""
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


def create_approval(user_id, approval_type, fields):
    approval_code = APPROVAL_CODES[approval_type]
    token = get_token()

    cached = get_form_fields(approval_type, approval_code, token)
    form_list = build_form(approval_type, fields, token)
    if form_list is None:
        return False, "无法构建表单，请检查审批字段配置", {}, ""

    form_data = json.dumps(form_list, ensure_ascii=False)
    print(f"提交表单[{approval_type}]: {form_data}")

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
    print(f"创建审批响应: {data}")

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


def _handle_image_message(open_id, user_id, message_id, content_json):
    """处理图片消息：下载→OCR→提取文件字段→等用户补充其余字段"""
    image_key = content_json.get("image_key", "")
    if not image_key:
        send_message(open_id, "无法获取图片，请重新发送。")
        return

    send_message(open_id, "正在识别文档内容，请稍候...")

    image_b64 = download_image(message_id, image_key)
    if not image_b64:
        send_message(open_id, "图片下载失败，请重试或直接用文字描述您的用印需求。")
        return

    ocr_text = ocr_image(image_b64)
    if not ocr_text:
        send_message(open_id, "文档识别失败，请直接用文字描述您的用印需求，例如：\n"
                     "「帮我给XX合同盖公章，微驰公司」")
        return

    print(f"OCR结果: {ocr_text[:300]}...")

    doc_fields = analyze_document_for_seal(ocr_text)
    print(f"文档分析结果: {doc_fields}")

    doc_fields.setdefault("document_count", "1")

    file_summary_parts = []
    if doc_fields.get("document_name"):
        file_summary_parts.append(f"· 文件名称: {doc_fields['document_name']}")
    if doc_fields.get("document_type"):
        file_summary_parts.append(f"· 文件类型: {doc_fields['document_type']}")
    if doc_fields.get("document_count"):
        file_summary_parts.append(f"· 文件数量: {doc_fields['document_count']}")

    PENDING_SEAL[open_id] = {"doc_fields": doc_fields, "user_id": user_id}

    CONVERSATIONS.setdefault(open_id, [])
    CONVERSATIONS[open_id].append({
        "role": "assistant",
        "content": f"[已识别文档] document_name={doc_fields.get('document_name','')}, "
                   f"document_type={doc_fields.get('document_type','')}"
    })

    file_info = "\n".join(file_summary_parts) if file_summary_parts else "（未能识别文件信息）"
    msg = (
        f"已从文档中识别到：\n{file_info}\n\n"
        f"请补充以下信息（一条消息说完即可）：\n"
        f"1. 用印公司\n"
        f"2. 印章类型（公章/合同章/法人章/财务章）\n"
        f"3. 用印事由\n"
        f"4. 盖章还是外带（默认盖章）\n"
        f"5. 律师是否已审核（默认否）"
    )
    send_message(open_id, msg)


def _try_complete_seal(open_id, user_id, text):
    """用户发送补充信息后，合并文档字段+用户字段，创建用印申请"""
    pending = PENDING_SEAL.get(open_id)
    if not pending:
        return False

    doc_fields = pending["doc_fields"]

    prompt = (
        f"用户为用印申请补充了以下信息：\n{text}\n\n"
        f"请提取并返回JSON，包含：\n"
        f"- company: 用印公司\n"
        f"- seal_type: 印章类型(公章/合同章/法人章/财务章)\n"
        f"- reason: 用印事由\n"
        f"- usage_method: 盖章或外带(默认盖章)\n"
        f"- lawyer_reviewed: 律师是否已审核(是或否,默认否)\n"
        f"- remarks: 备注(如果有)\n"
        f"只返回JSON。"
    )
    try:
        res = httpx.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"}
            },
            timeout=30
        )
        content = res.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        user_fields = json.loads(content)
    except Exception as e:
        print(f"解析用印补充信息失败: {e}")
        send_message(open_id, "无法理解您的输入，请重新描述用印公司、印章类型和用印事由。")
        return True

    all_fields = {**doc_fields, **user_fields}
    all_fields.setdefault("usage_method", "盖章")
    all_fields.setdefault("document_count", "1")
    all_fields.setdefault("lawyer_reviewed", "否")

    missing = []
    for key in ["company", "seal_type", "reason"]:
        if not all_fields.get(key):
            missing.append(FIELD_LABELS.get(key, key))
    if not all_fields.get("document_name"):
        missing.append("文件名称")

    if missing:
        send_message(open_id, f"还缺少：{'、'.join(missing)}\n请补充。")
        PENDING_SEAL[open_id]["doc_fields"] = all_fields
        return True

    admin_comment = get_admin_comment("用印申请", all_fields)
    success, msg, resp_data, summary = create_approval(user_id, "用印申请", all_fields)

    if success:
        instance_code = resp_data.get("instance_code", "")
        if instance_code:
            link = f"https://applink.feishu.cn/client/approval?instanceCode={instance_code}"
            card_content = f"【用印申请】\n{summary}\n\n行政意见: {admin_comment}\n\n工单已创建，点击下方按钮查看："
            send_card_message(open_id, card_content, link, "查看工单")
        send_message(open_id, f"· 用印申请：✅ 已提交\n{summary}\n行政意见: {admin_comment}")
    else:
        send_message(open_id, f"· 用印申请：❌ 提交失败 - {msg}")

    del PENDING_SEAL[open_id]
    CONVERSATIONS[open_id] = []
    return True


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
        msg_type = event.message.message_type
        message_id = event.message.message_id
        content_json = json.loads(event.message.content)

        if msg_type == "image":
            _handle_image_message(open_id, user_id, message_id, content_json)
            return

        text = content_json.get("text", "").strip()
        if not text:
            send_message(open_id, "请发送文字消息描述您的审批需求，或发送文档图片进行用印申请。")
            return

        if open_id in PENDING_SEAL:
            if _try_complete_seal(open_id, user_id, text):
                return

        if open_id not in CONVERSATIONS:
            CONVERSATIONS[open_id] = []
        CONVERSATIONS[open_id].append({"role": "user", "content": text})
        if len(CONVERSATIONS[open_id]) > 10:
            CONVERSATIONS[open_id] = CONVERSATIONS[open_id][-10:]

        result = analyze_message(CONVERSATIONS[open_id])
        requests = result.get("requests", [])
        unclear = result.get("unclear", "")

        if not requests:
            types = "、".join(APPROVAL_CODES.keys())
            reply = unclear if unclear else f"你好！我可以帮你提交以下审批：\n{types}\n\n请告诉我你需要办理哪种？"
            send_message(open_id, reply)
            CONVERSATIONS[open_id].append({"role": "assistant", "content": reply})
            return

        complete = [r for r in requests if not r.get("missing")]
        incomplete = [(r["approval_type"], r.get("missing", [])) for r in requests if r.get("missing")]

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
                    success, msg, resp_data, form_summary = create_approval(user_id, approval_type, fields)
                    if success:
                        instance_code = resp_data.get("instance_code", "")
                        replies.append(f"· {approval_type}：✅ 已提交\n{form_summary}\n行政意见: {admin_comment}")
                        if instance_code:
                            link = f"https://applink.feishu.cn/client/approval?instanceCode={instance_code}"
                            card_content = f"【{approval_type}】\n{form_summary}\n\n行政意见: {admin_comment}\n\n工单已创建，点击下方按钮查看："
                            send_card_message(open_id, card_content, link, "查看工单")
                    else:
                        print(f"创建审批失败[{approval_type}]: {msg}")
                        if "free process" in msg.lower() or "unsupported approval" in msg.lower():
                            mark_free_process(approval_code)
                            link = f"https://applink.feishu.cn/client/approval?tab=create&definitionCode={approval_code}"
                            send_card_message(open_id, f"【{approval_type}】\n{summary}\n\n该类型暂不支持自动创建。请点击下方按钮在飞书中发起：", link, f"打开{approval_type}审批表单")
                            replies.append(f"· {approval_type}：已整理信息，请点击卡片按钮发起")
                        else:
                            replies.append(f"· {approval_type}：❌ 提交失败 - {msg}")

        if incomplete:
            parts = [f"{at}还缺少：{'、'.join([FIELD_LABELS.get(m, m) for m in miss])}" for at, miss in incomplete]
            replies.append("请补充以下信息：\n" + "\n".join(parts))

        if not complete:
            send_message(open_id, "\n".join(replies))
            CONVERSATIONS[open_id].append({"role": "assistant", "content": "请补充信息"})
            return

        header = f"✅ 已处理 {len(complete)} 个申请：\n\n" if len(complete) > 1 else ""
        send_message(open_id, header + "\n\n".join(replies))
        if not incomplete:
            CONVERSATIONS[open_id] = []

    except Exception as e:
        print(f"处理消息出错: {e}")
        traceback.print_exc()
        if open_id:
            send_message(open_id, "系统出现异常，请稍后再试。")


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def _start_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    print(f"健康检查服务已启动 :{port}")
    server.serve_forever()


if __name__ == "__main__":
    threading.Thread(target=_start_health_server, daemon=True).start()

    handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(on_message) \
        .register_p2_im_message_message_read_v1(_on_message_read) \
        .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(_on_message_read) \
        .build()
    ws_client = lark.ws.Client(
        FEISHU_APP_ID,
        FEISHU_APP_SECRET,
        event_handler=handler,
        log_level=lark.LogLevel.INFO
    )
    print("飞书审批机器人已启动...")
    ws_client.start()