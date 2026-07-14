FROM python:3.11-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba

ARG TARGETARCH=amd64
ARG OFFICECLI_VERSION=1.0.135
ARG CHECKSUMS_SHA256=9a991e1db05c6c6896c6747c76d833735f0b907648cb1a416f80f1840999a3d5
ARG OFFICECLI_AMD64_SHA256=00028af3db48678fc9ff6c8f9e70fa4c150dcee2b43ed85254afc8f4a48e13eb
ARG OFFICECLI_ARM64_SHA256=2ec0d5a455646647f798f723e62fd9c18e2e801f8460d4401d2554a6a1c6d5b0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OFFICECLI_SKIP_UPDATE=1 \
    OFFICECLI_BIN=/usr/local/bin/officecli \
    OFFICE_AGENT_WORKSPACE=/workspace \
    CHROME_BIN=/usr/bin/chromium

WORKDIR /opt/office-agent

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates chromium curl fonts-liberation \
    && rm -rf /var/lib/apt/lists/* \
    && case "${TARGETARCH}" in \
         amd64) asset=officecli-linux-x64; expected="${OFFICECLI_AMD64_SHA256}" ;; \
         arm64) asset=officecli-linux-arm64; expected="${OFFICECLI_ARM64_SHA256}" ;; \
         *) echo "Unsupported target architecture: ${TARGETARCH}" >&2; exit 1 ;; \
       esac \
    && release="https://github.com/iOfficeAI/OfficeCLI/releases/download/v${OFFICECLI_VERSION}" \
    && curl --fail --location --proto '=https' --tlsv1.2 "${release}/SHA256SUMS" --output /tmp/SHA256SUMS \
    && echo "${CHECKSUMS_SHA256}  /tmp/SHA256SUMS" | sha256sum --check --strict \
    && curl --fail --location --proto '=https' --tlsv1.2 "${release}/${asset}" --output /tmp/officecli \
    && echo "${expected}  /tmp/officecli" | sha256sum --check --strict \
    && grep -F "${expected}" /tmp/SHA256SUMS | grep -F "${asset}" >/dev/null \
    && install -m 0755 /tmp/officecli /usr/local/bin/officecli \
    && rm /tmp/officecli /tmp/SHA256SUMS \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin officeagent \
    && mkdir -p /workspace \
    && chown 10001:10001 /workspace

COPY requirements.txt .
RUN python -m pip install --no-cache-dir --upgrade pip==26.1.2 \
    && python -m pip install --no-cache-dir --requirement requirements.txt

COPY --chown=10001:10001 app.py .

USER 10001:10001
EXPOSE 8082

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8082", "--no-access-log"]
