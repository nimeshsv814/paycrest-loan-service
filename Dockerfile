# ── Stage 1: Install Python packages ──────────────────────────
FROM python:3.11-slim AS builder
WORKDIR /app
COPY requirements.txt .
# Install to the standard system path
RUN pip install --no-cache-dir -r requirements.txt

# ── Stage 2: Runtime ──────────────────────────────────────────
FROM python:3.11-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# 1. Create the user and group
RUN groupadd -r appgroup && useradd -r -g appgroup appuser

# 2. Copy the installed libraries and the uvicorn executable
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# 3. Create uploads directory (safe to include even if not used)
RUN mkdir -p /app/uploads && chown -R appuser:appgroup /app/uploads

# 4. Copy the application code
COPY --chown=appuser:appgroup app ./app

USER appuser
EXPOSE 8000

# Use --host 0.0.0.0 so it's reachable outside the container
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]