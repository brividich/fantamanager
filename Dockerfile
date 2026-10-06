# ==============================================================================
# FantaManager - Production / Standalone Dockerfile
# Daphne ASGI Server with WebSocket & HTTP support
# ==============================================================================
FROM python:3.11-slim

# Prevent Python from writing .pyc files & enable unbuffered logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

# Install essential system dependencies (Pillow image processing & healthcheck curl)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gcc \
    libjpeg-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependencies first for Docker layer caching
COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . /app/

# Create persistent storage directories
RUN mkdir -p /app/data /app/media /app/logs /app/backups /app/staticfiles && \
    chmod +x /app/entrypoint.sh

# Expose HTTP & WebSocket port
EXPOSE 8000

# Periodic container healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz/ || exit 1

ENTRYPOINT ["/bin/sh", "/app/entrypoint.sh"]
CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "liveauction.asgi:application"]
