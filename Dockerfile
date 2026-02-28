FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1
# PaddleOCR/OpenCV 所需系统库（无头环境）
# libgl1-mesa-glx 在新版 Debian 中已废弃，改用 libgl1
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxcb1 libgl1 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
# paddlepaddle 2.5.2 已从 PyPI 下架，从官方源安装（2.6 在部分环境会 SIGILL）
RUN pip install paddlepaddle==2.5.2 -i https://www.paddlepaddle.org.cn/packages/stable/cpu/ \
    || pip install paddlepaddle==2.5.2 -i https://mirror.baidu.com/pypi/simple
RUN pip install -r requirements.txt
COPY . .
CMD ["python", "-u", "main.py"]
