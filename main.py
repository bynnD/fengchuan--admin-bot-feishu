import os
import re
import json
import httpx
import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
from approval_types import (
    APPROVAL_CODES, FIELD_LABELS, APPROVAL_FIELD_HINTS,
    LINK_ONLY_TYPES, FIELD_ID_FALLBACK, FIELD_ORDER, DATE_FIELDS, FIELD_LABELS_REVERSE,
    IMAGE_SUPPORT_TYPES, FIELDLIST_SUBFIELDS_FALLBACK, get_admin_comment
)
from approval_types.purchase import COST_DETAIL_SKIP_SUBFIELDS
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


def download_message_file(message_id, file_key, file_type="file"):
    """从飞书消息下载文件，返回二进制内容"""
    try:
        token = get_token()
        res = httpx.get(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}",
            params={"type": file_type},
            headers={"Authorization": f"Bearer {token}"},
            timeout=30
        )
        if res.status_code == 200:
            return res.content
        print(f"下载文件失败: status={res.status_code}")
    except Exception as e:
        print(f"下载文件异常: {e}")
    return None


def upload_approval_file(file_name, file_content):
    """上传文件到飞书审批，返回 (file_code, None) 成功，(None, 错误信息) 失败"""
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
            print(f"文件上传响应非JSON: status={res.status_code}, body前200字: {raw_text[:200]}")
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
            print(f"文件上传成功: {file_name} -> {file_code}")
            if not file_code:
                print(f"警告: API 返回成功但无 file code，完整 data: {d}")
                return None, "接口返回成功但未返回文件标识，请重试"
            return file_code, None
        err_msg = data.get("msg", "未知错误")
        err_code = data.get("code", "")
        print(f"文件上传失败: code={err_code}, msg={err_msg}")
        return None, err_msg
    except Exception as e:
        print(f"文件上传异常: {e}")
        return None, str(e)


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


def send_card_message(open_id, text, url, btn_label, use_desktop_link=False):
    """发送卡片消息。use_desktop_link=True 时，PC 端用 feishu.cn 链接在客户端内嵌打开"""
    if use_desktop_link and "instanceCode=" in url:
        m = re.search(r"instanceCode=([^&]+)", url)
        ic = m.group(1) if m else ""
        https_url = url.replace("lark://", "https://", 1) if url.startswith("lark://") else url
        feishu_url = f"https://www.feishu.cn/approval/detail/{ic}" if ic else https_url
        btn_config = {"tag": "button", "text": {"tag": "plain_text", "content": btn_label}, "type": "primary", "multi_url": {"url": https_url, "pc_url": feishu_url}}
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
        f"6. 采购的cost_detail是费用明细列表(必填)，每项必须含名称、规格、数量、金额。"
        f"「是否有库存」由审批人填写，发起人不填，不要提取。"
        f"格式为[{{\"名称\":\"笔记本电脑\",\"规格\":\"ThinkPad X1\",\"数量\":\"1\",\"金额\":\"8000\"}}]。"
        f"缺少名称/规格/数量/金额任一项就把cost_detail列入missing。purchase_reason可从物品信息推断(如'采购笔记本电脑')。"
        f"purchase_type(采购类别)可根据采购物品自动推断，如办公电脑、办公桌→办公用品，设备、机器→设备类等。\n"
        f"7. 用印申请：识别到用印需求时，只提取对话中能得到的字段(company/seal_type/reason等)，"
        f"document_name/document_type不需要用户说，会从上传文件自动获取。"
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


def _format_field_value(logical_key, raw_value, field_type, field_info=None, approval_type=None):
    """根据控件类型格式化值。fieldList 需传二维数组 [[{id,type,value},...]]。"""
    if field_type == "fieldList":
        sub_fields = (field_info or {}).get("sub_fields", [])
        skip_ids = COST_DETAIL_SKIP_SUBFIELDS if (approval_type == "采购申请" and logical_key == "cost_detail") else set()
        if isinstance(raw_value, list) and raw_value:
            if sub_fields:
                rows = []
                for item in raw_value:
                    if isinstance(item, dict):
                        row = []
                        for sf in sub_fields:
                            sf_id = sf.get("id") or sf.get("widget_id") or ""
                            val = "" if sf_id in skip_ids else _match_sub_field(sf.get("name", ""), item)
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
        print(f"无法获取{approval_type}的字段结构")
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
        value = _format_field_value(logical_key, raw, field_type, field_info, approval_type=approval_type)
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
        res = httpx.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 20
            },
            timeout=10
        )
        res.raise_for_status()
        out = res.json()["choices"][0]["message"]["content"].strip()
        return out.split("\n")[0].strip() if out else ""
    except Exception as e:
        print(f"推断采购类别失败: {e}")
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
# 用印申请：用户首次消息中已提取的字段（如「盖公章」），等收到文件后合并
SEAL_INITIAL_FIELDS = {}

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


def _extract_seal_from_filename(file_name, company_opts, seal_opts):
    """根据文件名用 AI 推断用印公司、印章类型、用印事由。返回 dict，仅包含能推断的字段。"""
    base_name = file_name.rsplit(".", 1)[0] if "." in file_name else file_name
    if not base_name or len(base_name) < 2:
        return {}
    company_str = "、".join(company_opts) if company_opts else "无"
    seal_str = "、".join(seal_opts) if seal_opts else "公章、合同章、法人章、财务章"
    prompt = (
        f"根据文件名推断用印申请信息。\n"
        f"文件名：{base_name}\n"
        f"可选用印公司：{company_str}\n"
        f"可选印章类型：{seal_str}\n"
        f"请返回JSON，只包含能推断的字段，无法推断的不要写：\n"
        f"- company: 从文件名中的公司名匹配上述选项（如「扇贝&风船」可推断风船等）\n"
        f"- seal_type: 合同类文件通常用公章或合同章\n"
        f"- reason: 文件用途/用印事由（如「流量广告合作协议」）\n"
        f"只返回JSON，不要其他内容。"
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
            timeout=15
        )
        res.raise_for_status()
        content = res.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        out = json.loads(content)
        return {k: v for k, v in out.items() if v and k in ("company", "seal_type", "reason")}
    except Exception as e:
        print(f"从文件名推断用印信息失败: {e}")
        return {}


def _handle_file_message(open_id, user_id, message_id, content_json):
    """处理文件消息：下载文件→上传审批附件→提取文件名→合并首次消息与文件名推断→等用户补充其余字段"""
    file_key = content_json.get("file_key", "")
    file_name = content_json.get("file_name", "未知文件")
    if not file_key:
        send_message(open_id, "无法获取文件，请重新发送。")
        return

    send_message(open_id, f"正在处理文件「{file_name}」，请稍候...")

    file_content = download_message_file(message_id, file_key, "file")
    if not file_content:
        send_message(open_id, "文件下载失败，请重新发送文件。")
        return

    file_code, upload_err = upload_approval_file(file_name, file_content)
    if not file_code:
        err_detail = f"（{upload_err}）" if upload_err else ""
        send_message(open_id, f"文件上传失败，请重新发送文件。附件上传成功后才能继续创建工单。{err_detail}")
        return

    doc_name = file_name.rsplit(".", 1)[0] if "." in file_name else file_name

    doc_fields = {
        "document_name": doc_name,
        "document_count": "1",
    }

    opts = _get_seal_form_options()
    company_opts = opts.get("company", [])
    seal_opts = opts.get("seal_type", ["公章", "合同章", "法人章", "财务章"])
    usage_opts = opts.get("usage_method", ["盖章", "外带"])
    lawyer_opts = opts.get("lawyer_reviewed", ["是", "否"])

    # 合并：文件基础信息 + 文件名 AI 推断 + 首次消息已提取字段（后者优先）
    ai_fields = _extract_seal_from_filename(file_name, company_opts, seal_opts)
    initial_fields = SEAL_INITIAL_FIELDS.pop(open_id, {})
    doc_fields = {**doc_fields, **ai_fields, **initial_fields}
    doc_fields.setdefault("usage_method", "盖章")
    doc_fields.setdefault("lawyer_reviewed", "否")

    CONVERSATIONS.setdefault(open_id, [])
    CONVERSATIONS[open_id].append({
        "role": "assistant",
        "content": f"[已接收文件] 文件名称={doc_name}"
    })

    missing = [k for k in ["company", "seal_type", "reason"] if not doc_fields.get(k)]
    if not missing:
        # 全部可推断，直接创建工单
        _do_create_seal(open_id, user_id, doc_fields, file_code)
        return

    PENDING_SEAL[open_id] = {
        "doc_fields": doc_fields,
        "file_code": file_code,
        "user_id": user_id,
    }

    # 只列出缺失项
    labels = {"company": "用印公司", "seal_type": "印章类型", "reason": "文件用途/用印事由"}
    hint_map = {
        "company": f"{'、'.join(company_opts) if company_opts else '请输入'}",
        "seal_type": "、".join(seal_opts),
        "reason": "（请描述）",
    }
    lines = [
        f"已接收文件：{file_name}",
        f"· 文件名称: {doc_name}",
        "",
        "请补充以下信息（一条消息说完即可）：",
    ]
    for i, k in enumerate(missing, 1):
        lines.append(f"{i}. {labels[k]}：{hint_map.get(k, '')}")
    lines.extend([
        f"盖章还是外带：{'、'.join(usage_opts)}（默认盖章）",
        f"律师是否已审核：{'、'.join(lawyer_opts)}（默认否）",
    ])
    send_message(open_id, "\n".join(lines))


def _do_create_seal(open_id, user_id, all_fields, file_code):
    """用印申请字段齐全时，直接创建工单"""
    all_fields = dict(all_fields)
    all_fields.setdefault("usage_method", "盖章")
    all_fields.setdefault("document_count", "1")
    all_fields.setdefault("lawyer_reviewed", "否")

    file_codes = {ATTACHMENT_FIELD_ID: [file_code]} if file_code else {}
    admin_comment = get_admin_comment("用印申请", all_fields)
    success, msg, resp_data, summary = create_approval(user_id, "用印申请", all_fields, file_codes=file_codes)

    if open_id in PENDING_SEAL:
        del PENDING_SEAL[open_id]
    if open_id in CONVERSATIONS:
        CONVERSATIONS[open_id] = []

    if success:
        instance_code = resp_data.get("instance_code", "")
        if instance_code:
            link = f"https://applink.feishu.cn/client/approval?instanceCode={instance_code}"
            card_content = f"【用印申请】\n{summary}\n\n行政意见: {admin_comment}\n\n工单已创建，点击下方按钮查看："
            send_card_message(open_id, card_content, link, "查看工单", use_desktop_link=True)
        else:
            send_message(open_id, f"· 用印申请：✅ 已提交\n{summary}\n行政意见: {admin_comment}")
    else:
        send_message(open_id, f"· 用印申请：❌ 提交失败 - {msg}")


def _try_complete_seal(open_id, user_id, text):
    """用户发送补充信息后，合并文件字段+用户字段，创建用印申请"""
    pending = PENDING_SEAL.get(open_id)
    if not pending:
        return False

    doc_fields = pending["doc_fields"]
    file_code = pending.get("file_code")

    opts = _get_seal_form_options()
    company_hint = f"（选项：{'/'.join(opts.get('company', []))}）" if opts.get("company") else ""
    seal_hint = f"（选项：{'/'.join(opts.get('seal_type', ['公章','合同章','法人章','财务章']))}）"
    usage_hint = f"（选项：{'/'.join(opts.get('usage_method', ['盖章','外带']))}，默认盖章）"
    lawyer_hint = f"（选项：{'/'.join(opts.get('lawyer_reviewed', ['是','否']))}，默认否）"

    prompt = (
        f"用户为用印申请补充了以下信息：\n{text}\n\n"
        f"请提取并返回JSON，包含：\n"
        f"- company: 用印公司{company_hint}\n"
        f"- seal_type: 印章类型{seal_hint}\n"
        f"- reason: 文件用途/用印事由\n"
        f"- usage_method: 盖章或外带{usage_hint}\n"
        f"- lawyer_reviewed: 律师是否已审核{lawyer_hint}\n"
        f"- remarks: 备注(如果有)\n"
        f"只返回JSON。若用户未提及某选项字段，使用默认值。"
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

    if missing:
        send_message(open_id, f"还缺少：{'、'.join(missing)}\n请补充。")
        PENDING_SEAL[open_id]["doc_fields"] = all_fields
        return True

    _do_create_seal(open_id, user_id, all_fields, file_code)
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

        if msg_type == "file":
            _handle_file_message(open_id, user_id, message_id, content_json)
            return

        if msg_type == "image":
            send_message(open_id, "如需用印申请，请发送原始文件（Word/PDF），而非图片截图。\n"
                         "其他审批请用文字描述。")
            return

        text = content_json.get("text", "").strip()
        if not text:
            send_message(open_id, "请发送文字消息描述您的审批需求。\n如需用印，请先上传需要盖章的文件。")
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

        for req in requests:
            at = req.get("approval_type", "")
            miss = req.get("missing", [])
            fields_check = req.get("fields", {})
            if at == "用印申请" and open_id not in PENDING_SEAL:
                # 保存首次消息中已提取的字段（如「盖公章」），收到文件后合并
                initial = req.get("fields", {})
                if initial:
                    SEAL_INITIAL_FIELDS[open_id] = initial
                send_message(open_id, "请补充以下信息：\n"
                             f"用印申请还缺少：上传用章文件\n"
                             f"请先上传需要盖章的文件（Word/PDF），我会自动提取文件名称。")
                CONVERSATIONS[open_id].append({"role": "assistant", "content": "请上传需要盖章的文件"})
                requests = [r for r in requests if r.get("approval_type") != "用印申请"]
                if not requests:
                    return
            if at == "采购申请":
                cd = fields_check.get("cost_detail")
                if not cd or (isinstance(cd, list) and len(cd) == 0) or cd == "":
                    if "cost_detail" not in miss:
                        miss.append("cost_detail")
                        req["missing"] = miss

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
                        if instance_code:
                            link = f"https://applink.feishu.cn/client/approval?instanceCode={instance_code}"
                            card_content = f"【{approval_type}】\n{form_summary}\n\n行政意见: {admin_comment}\n\n工单已创建，点击下方按钮查看："
                            send_card_message(open_id, card_content, link, "查看工单", use_desktop_link=True)
                            # 已发卡片，不再重复发送文字详情
                        else:
                            replies.append(f"· {approval_type}：✅ 已提交\n{form_summary}\n行政意见: {admin_comment}")
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
        body = header + "\n\n".join(replies)
        if body.strip():
            send_message(open_id, body)
        if not incomplete:
            CONVERSATIONS[open_id] = []

    except Exception as e:
        print(f"处理消息出错: {e}")
        traceback.print_exc()
        if open_id:
            send_message(open_id, "系统出现异常，请稍后再试。")


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/debug-form":
            from urllib.parse import parse_qs
            qs = parse_qs((self.path.split("?") + ["?"])[1])
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