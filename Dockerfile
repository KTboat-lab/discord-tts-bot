FROM python:3.12-slim

# ติดตั้ง FFmpeg และ libopus สำหรับระบบเสียง Discord บน Linux
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libopus0 \
    libffi-dev \
    libsodium23 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

ENV PYTHONUNBUFFERED=1

CMD ["python", "-u", "bot.py"]
