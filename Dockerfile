FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Pinning exact Debian package versions here would make the build brittle
# against upstream mirror/security-patch churn for a base image we don't
# control the release cadence of; we accept the hadolint DL3008 finding
# below rather than pin versions that would routinely go stale and break CI.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user in every service that uses this image (web,
# worker, beat — see docker-compose.yml), not just for its own sake but
# because Celery's worker prints a SecurityWarning and refuses to be quiet
# about it when started as root/superuser (confirmed by actually running
# it) — this is the real fix, not a suppressed flag.
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/api/v1/health/ || exit 1

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
