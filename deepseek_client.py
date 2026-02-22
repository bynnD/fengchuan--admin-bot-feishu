"""
DeepSeek API 客户端，提供带重试的调用。
供 main.py 及 approval_types 中所有 AI 调用统一使用。
"""

import os
import time
import httpx

DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"


def _is_retryable_error(msg: str) -> bool:
    """判断是否为可重试的错误（网络/超时等）"""
    if not msg:
        return True
    msg_lower = msg.lower()
    return any(k in msg_lower for k in ("timeout", "网络", "连接", "超时", "稍后", "retry"))


def call_deepseek_with_retry(
    messages,
    response_format=None,
    timeout=30,
    max_retries=2,
    api_key=None,
):
    """
    带指数退避的 DeepSeek API 调用。
    api_key 为空时从环境变量 DEEPSEEK_API_KEY 读取。
    """
    key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
    }
    if response_format:
        payload["response_format"] = response_format
    for attempt in range(max_retries + 1):
        try:
            res = httpx.post(
                DEEPSEEK_API_URL,
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
                timeout=timeout,
            )
            res.raise_for_status()
            return res
        except Exception as e:
            err_msg = str(e)
            if attempt == max_retries or not _is_retryable_error(err_msg):
                raise
            time.sleep(2**attempt)
    return None  # unreachable
