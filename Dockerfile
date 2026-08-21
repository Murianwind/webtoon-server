FROM python:3.12-slim

WORKDIR /app

# TZ 환경변수가 실제로 동작하려면 시간대 데이터가 필요함
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

ENV LIBRARY_ROOT=/library
ENV STATIC_DIR=/app/static

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
