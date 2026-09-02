# ──────────────────────────────────────────────────────────────────
# Stage 1: build dependencies into a clean layer
# ──────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# Install dependencies into an isolated prefix so only what we need
# ends up in the final image.
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ──────────────────────────────────────────────────────────────────
# Stage 2: lean runtime image
# ──────────────────────────────────────────────────────────────────
FROM python:3.12-slim

# Non-root user for security
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY app/       app/
COPY db/        db/
COPY scripts/   scripts/
COPY main.py    .

# Environment defaults (override via docker run -e or compose env_file)
ENV DATABASE_URL=postgresql://postgres:password@db:5432/mrd_orders \
    WORKER_POLL_INTERVAL=2 \
    WORKER_BATCH_SIZE=50 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER appuser

EXPOSE 8000

# Uvicorn is started directly; main.py is used so logging config is applied.
CMD ["python", "main.py"]
