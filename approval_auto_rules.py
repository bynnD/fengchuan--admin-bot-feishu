"""
开票申请单、用印申请单的自动审批规则
- 开票：AI 分析附件，仅合同时添加风险提示并转人工审批，其他情况自动通过
- 用印：两点判断（合法合规、无风险点），满足则自动通过
便于后期维护和修改规则。
"""

import json
import logging

logger = logging.getLogger(__name__)


# =============================================================================
# 公共工具
# =============================================================================

def collect_file_tokens_from_form(form_list):
    """
    从审批表单中收集所有附件 file_token/code 及文件名。
    返回 [(token, name), ...]，name 为表单中的文件名，无则空字符串。
    """
    result = []

    def _extract_from_val(val):
        if isinstance(val, list):
            for v in val:
                if isinstance(v, dict):
                    tok = v.get("file_token") or v.get("code") or v.get("file_code") or v.get("url")
                    if tok:
                        name = v.get("name") or v.get("file_name") or ""
                        result.append((tok, str(name).strip() if name else ""))
                elif isinstance(v, str) and v.strip():
                    result.append((v.strip(), ""))
        elif val:
            result.append((str(val), ""))

    for item in form_list:
        if item.get("type") in ("attach", "attachV2", "attachment", "attachmentV2", "file"):
            _extract_from_val(item.get("value", []))
        elif item.get("type") == "fieldList":
            val = item.get("value", [])
            if isinstance(val, list):
                for row in val:
                    if isinstance(row, list):
                        for cell in row:
                            if isinstance(cell, dict) and cell.get("type") in (
                                "attach", "attachV2", "attachment", "attachmentV2", "file"
                            ):
                                _extract_from_val(cell.get("value", []))
    return result


# =============================================================================
# 开票申请单
# =============================================================================

def check_invoice_attachments_with_ai(file_contents_with_names, get_token):
    """
    开票申请附件分析：判断附件是否仅有合同。
    若仅有合同，添加风险提示并转人工审批，由审批人决定是否通过。
    返回 (only_contract: bool, comment: str)
    - only_contract=True: 附件中仅有合同，添加风险提示并转人工审批
    - only_contract=False: 其他情况，可自动通过
    """
    from file_extraction import extract_text_from_file
    from deepseek_client import call_deepseek_with_retry

    combined_parts = []
    all_no_content = True
    for i, (content, fname) in enumerate(file_contents_with_names):
        file_text = ""
        if content and isinstance(content, bytes) and len(content) > 10:
            file_text = extract_text_from_file(content, fname, get_token)
        has_content = bool(file_text and len(file_text.strip()) > 10)
        if has_content:
            all_no_content = False
        part = f"--- 附件{i+1}: {fname} ---\n"
        part += f"文件内容摘要：\n{file_text[:4000]}" if has_content else "（内容无法提取，请根据文件名推断类型）"
        combined_parts.append(part)

    combined = "\n\n".join(combined_parts) if combined_parts else "（无附件内容）"
    filename_hint = ""
    if all_no_content and file_contents_with_names:
        fnames = [f for _, f in file_contents_with_names]
        filename_hint = f"\n\n【重要】以上附件内容均无法提取，请根据文件名推断：{fnames}\n文件名含「凭证」「截图」「结算」「对账」「水单」「流水」「订单」等通常非仅合同；含「合同」「协议」可能为合同类。"
    prompt = f"""你是开票审批助手。请分析以下开票申请的附件内容，判断附件类型。
{filename_hint}

{combined}

附件类型包括：合同/协议、对账单/结算单、付款证明（客户付款凭证/银行流水/转账截图等）、收款证明（我司收款凭证/到账截图等）、订单、其他。

请返回 JSON：
1. only_contract: 若所有附件都仅是合同/协议类（无付款证明、收款证明、对账单、结算单等），则为 true；否则为 false
2. attachment_types: 识别到的附件类型列表，如 ["合同", "对账单"]，无法确定时可写 ["根据文件名推断"]
3. comment: 简短说明，仅合同时写「附件中仅有合同」；其他可自动通过时可不写或简短说明

返回格式示例：
{{"only_contract": false, "attachment_types": ["合同", "对账单"], "comment": ""}}
{{"only_contract": false, "attachment_types": ["根据文件名推断"], "comment": ""}}
{{"only_contract": true, "attachment_types": ["合同"], "comment": "附件中仅有合同。"}}

只返回 JSON，不要其他内容。"""

    try:
        res = call_deepseek_with_retry(
            [{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            timeout=30,
            max_retries=2,
        )
        content = res.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        out = json.loads(content)
        only_contract = bool(out.get("only_contract", False))
        comment = out.get("comment", "")
        if only_contract:
            comment = "附件中仅有合同。"
        return only_contract, comment
    except Exception as e:
        logger.exception("开票附件 AI 分析失败: %s", e)
        return False, f"AI 分析异常：{e}"


# =============================================================================
# 用印申请单
# =============================================================================

def check_seal_with_ai(file_content, file_name, seal_type, get_token):
    """
    用印 AI 分析：两点判断（不判断用印类型）
    1. 文件内容是否合法合规
    2. 文件内容存在哪些风险点
    file_content: bytes 或 None，为 None 时仅根据文件名/类型推断
    返回 (can_auto: bool, comment: str, risk_points: list)
    """
    from file_extraction import extract_text_from_file
    from deepseek_client import call_deepseek_with_retry

    file_text = ""
    if file_content and isinstance(file_content, bytes) and len(file_content) > 10:
        file_text = extract_text_from_file(file_content, file_name, get_token)
    has_content = bool(file_text and len(file_text.strip()) > 10)
    combined = f"文件名：{file_name}\n\n" + (
        f"文件内容摘要：\n{file_text[:6000]}" if has_content else "（文件内容无法提取，仅根据文件名和类型推断）"
    )

    prompt = f"""你是一个用印合规审核助手。请对以下文件进行两点分析：

{combined}

请严格按以下两点分析，并返回 JSON：
1. legal_compliant: 文件内容是否合法合规（true/false）
2. risk_points: 具体不合规项/风险点列表，每项简短（如「缺少关键条款」「金额异常」「律师未审核」），如无则 []
3. comment: 合规时为「文件合法合规」；不合规时必须列出具体问题（与 risk_points 一致，用；分隔），不要笼统表述

返回格式示例：
{{"legal_compliant": true, "risk_points": [], "comment": "文件合法合规。"}}
不合规示例：{{"legal_compliant": false, "risk_points": ["缺少关键条款", "金额异常"], "comment": "缺少关键条款；金额异常"}}

只返回 JSON，不要其他内容。"""

    try:
        res = call_deepseek_with_retry(
            [{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            timeout=30,
            max_retries=2,
        )
        content = res.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        out = json.loads(content)
        legal = out.get("legal_compliant", False)
        risks = out.get("risk_points") or []
        if not isinstance(risks, list):
            risks = [str(risks)] if risks else []
        comment = out.get("comment", "")

        can_auto = legal and len(risks) == 0
        if not can_auto:
            # 具体问题用 risk_points 列出，comment 与之一致
            comment = "；".join(risks[:5]) if risks else (comment or "存在合规问题")
        return can_auto, comment, risks
    except Exception as e:
        logger.exception("用印 AI 分析失败: %s", e)
        return False, f"AI 分析异常：{e}", ["分析失败"]
