FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GPTFREE_REGISTER_ROOT=/opt/gptfree-register

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates nodejs npm \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin app

COPY pyproject.toml README.md README.zh-CN.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY src/hidemyemail_generator/vendor/gptfree_register /opt/gptfree-register
RUN cd /opt/gptfree-register/core \
    && npm ci --omit=dev --no-audit --no-fund

COPY protocol-registration-server/pyproject.toml ./protocol-registration-server/
COPY protocol-registration-server/protocol_registration_server ./protocol-registration-server/protocol_registration_server
RUN pip install --no-cache-dir ./protocol-registration-server

USER app

ENTRYPOINT ["protocol-registration-server"]
