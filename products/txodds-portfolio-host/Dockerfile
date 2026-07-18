# Combined one-service host for both World Cup products.
# Build context must be the repository's `products/` directory so both products
# and this host are available:
#   docker build -f products/txodds-portfolio-host/Dockerfile -t txodds-portfolio-host products
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080 \
    PROOFGUARD_MODE=AUTO \
    PROOFGUARD_AUTO_START=true \
    PROOFGUARD_POLL_SECONDS=60

WORKDIR /app

RUN addgroup --system portfolio && adduser --system --ingroup portfolio portfolio

# Install both products (no dev extras) and the host, from local source.
COPY proofguard-agent /app/proofguard-agent
COPY finalitygate-resolver /app/finalitygate-resolver
COPY txodds-portfolio-host /app/txodds-portfolio-host
RUN python -m pip install --no-cache-dir \
      /app/proofguard-agent \
      /app/finalitygate-resolver \
      /app/txodds-portfolio-host

USER portfolio
EXPOSE 8080

# Functional combined healthcheck (no extra packages required).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8080')+'/health',timeout=3).read()" || exit 1

CMD ["sh", "-c", "uvicorn txodds_portfolio_host.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
