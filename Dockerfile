FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir \
    python-telegram-bot \
    playwright \
    && playwright install --with-deps chromium

COPY . .

EXPOSE 8026

CMD ["python", "api_server.py", "--host", "0.0.0.0", "--port", "8026"]