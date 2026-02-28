FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1
# Tesseract OCR（轻量，镜像约 500MB 内）
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["python", "-u", "main.py"]
