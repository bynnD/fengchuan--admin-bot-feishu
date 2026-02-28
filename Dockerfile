FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1
# RapidOCR/OpenCV 所需系统库（无头环境）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxcb1 libgl1 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["python", "-u", "main.py"]
