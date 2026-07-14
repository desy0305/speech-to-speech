FROM python:3.11-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba

ARG MODEL_URL=https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01.tar.bz2
ARG MODEL_SHA256=f170013b4716e41b62b9bfd809687c207cef798ef9bc6534d524e17af9b6561a

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WAKE_WORD_MODEL_DIR=/opt/wake-word/model

WORKDIR /opt/wake-word

RUN apt-get update \
    && apt-get install -y --no-install-recommends bzip2 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl --fail --location --proto '=https' --tlsv1.2 "${MODEL_URL}" --output /tmp/wake-model.tar.bz2 \
    && echo "${MODEL_SHA256}  /tmp/wake-model.tar.bz2" | sha256sum --check --strict \
    && mkdir -p /opt/wake-word/model \
    && tar -xjf /tmp/wake-model.tar.bz2 --strip-components=1 -C /opt/wake-word/model \
    && rm /tmp/wake-model.tar.bz2 \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin wakeword \
    && chmod -R a=rX /opt/wake-word/model

COPY requirements.txt .
RUN python -m pip install --no-cache-dir --upgrade pip==26.1.2 \
    && python -m pip install --no-cache-dir --requirement requirements.txt

COPY --chown=10001:10001 app.py .

USER 10001:10001
EXPOSE 8081

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8081", "--no-access-log"]
