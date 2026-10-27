FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl dnsutils iputils-ping openssl procps \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "cryptography==43.0.3"
COPY make_certs.py synthetic_check.py /tools/
