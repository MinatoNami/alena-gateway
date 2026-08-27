FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

# Runs unprivileged: this process is reachable from the tailnet through nginx,
# and it has no reason to own anything on disk.
RUN useradd --system --uid 10001 gateway
USER gateway

EXPOSE 8000

# One worker is right here. The work is a handful of concurrent HTTP probes
# behind a shared cache, and a second worker would keep a second cache — halving
# the cache hit rate and doubling the probe traffic for no throughput gain.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
