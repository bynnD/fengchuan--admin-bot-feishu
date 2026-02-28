"""
通用文件内容提取模块 - 供所有有附件识别读取需求的工单类型使用。
支持 Word/PDF/Excel 文本提取，图片和扫描件通过飞书 OCR 识别。
"""

import base64
import logging
import time
import httpx

logger = logging.getLogger(__name__)

# 飞书 OCR 频率限制时重试：最多 3 次，退避 2s / 4s / 8s
OCR_RETRY_MAX = 3
OCR_RETRY_DELAYS = (2, 4, 8)


def feishu_ocr(image_content, get_token):
    """调用飞书 OCR 识别图片/扫描件中的文字。返回拼接后的文本。"""
    if not image_content:
        return ""
    token = get_token()
    b64 = base64.b64encode(image_content).decode("utf-8")
    for attempt in range(OCR_RETRY_MAX):
        try:
            res = httpx.post(
                "https://open.feishu.cn/open-apis/optical_char_recognition/v1/image/basic_recognize",
                headers={"Authorization": f"Bearer {token}"},
                json={"image": b64},
                timeout=15
            )
            data = res.json()
            if data.get("code") == 0:
                text_list = data.get("data", {}).get("text_list", [])
                return "\n".join(t for t in text_list if t) if text_list else ""
            msg = data.get("msg", "")
            # 频率限制时退避重试
            if "frequency" in msg.lower() or "limit" in msg.lower():
                if attempt < OCR_RETRY_MAX - 1:
                    delay = OCR_RETRY_DELAYS[attempt]
                    logger.info("飞书 OCR 频率限制，%ds 后重试 (%d/%d)", delay, attempt + 1, OCR_RETRY_MAX)
                    time.sleep(delay)
                    token = get_token()  # 刷新 token
                    continue
            logger.warning("飞书 OCR 失败: %s", msg)
            break
        except Exception as e:
            logger.warning("飞书 OCR 异常: %s", e)
            break
    return ""


def extract_text_from_file(file_content, file_name, get_token):
    """
    从 Word/PDF/图片/扫描件提取文本。
    图片和扫描件使用飞书 OCR。返回前 8000 字符。
    供所有有附件识别读取需求的工单类型调用。
    """
    if not file_content:
        return ""
    ext = (file_name.rsplit(".", 1)[-1] or "").lower()
    try:
        if ext in ("docx", "doc"):
            from docx import Document
            from io import BytesIO
            try:
                doc = Document(BytesIO(file_content))
                parts = [p.text for p in doc.paragraphs if p.text.strip()]
                for table in doc.tables:
                    for row in table.rows:
                        for cell in row.cells:
                            if cell.text.strip():
                                parts.append(cell.text)
                text = "\n".join(parts)[:8000]
                if not text.strip():
                    logger.debug("Word 解析(%s): 段落和表格均为空", file_name)
                return text
            except Exception as doc_err:
                logger.warning("Word 解析失败(%s): %s", file_name, doc_err)
                return ""
        if ext == "xlsx":
            from io import BytesIO
            try:
                from openpyxl import load_workbook
                wb = load_workbook(BytesIO(file_content), read_only=True, data_only=True)
                parts = []
                for sheet in wb.worksheets:
                    for row in sheet.iter_rows(values_only=True):
                        row_cells = [str(c).strip() for c in (row or []) if c is not None and str(c).strip()]
                        if row_cells:
                            parts.append(" | ".join(row_cells))
                text = "\n".join(parts)[:8000]
                return text
            except Exception as excel_err:
                logger.warning("Excel 解析失败(%s): %s", file_name, excel_err)
                return ""
        if ext in ("png", "jpg", "jpeg", "bmp", "gif", "webp"):
            return feishu_ocr(file_content, get_token)[:8000]
        if ext == "pdf":
            from pypdf import PdfReader
            from io import BytesIO
            reader = PdfReader(BytesIO(file_content))
            parts = []
            for page in reader.pages[:20]:
                t = page.extract_text()
                if t:
                    parts.append(t)
            text = "\n".join(parts)
            if len(text.strip()) >= 50:
                return text[:8000]
            # 文本很少，可能是扫描件，用 PyMuPDF 渲染页面为图片后 OCR
            import fitz
            doc = fitz.open(stream=file_content, filetype="pdf")
            ocr_parts = []
            for i in range(min(len(doc), 20)):
                if i > 0:
                    time.sleep(1)  # 页间间隔，降低飞书 OCR 频率限制触发
                page = doc[i]
                pix = page.get_pixmap(dpi=150)
                img_bytes = pix.tobytes("png")
                ocr_text = feishu_ocr(img_bytes, get_token)
                if ocr_text:
                    ocr_parts.append(ocr_text)
            doc.close()
            return "\n".join(ocr_parts)[:8000] if ocr_parts else text[:8000]
    except Exception as e:
        logger.warning("提取文件文本失败(%s): %s", file_name, e)
    return ""
