# ── Build stage ──────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# Copy pre-built packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY . .

# /data is the persistent volume mount point for database.db and uploads/
RUN mkdir -p /data/uploads

EXPOSE 5000

ENV DATA_DIR=/data \
    PYTHONUNBUFFERED=1

# 2 workers is enough for a personal app on t4g.micro/t3.micro
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "app:app"]
