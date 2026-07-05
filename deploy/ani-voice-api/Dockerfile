FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04

ARG ANI_VOICE_REV=91722a7fdd404cca1818ace44038ed43322fc66e

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PATH="/opt/venv/bin:${PATH}"
ENV HF_HOME=/root/.cache/huggingface
ENV TORCH_HOME=/root/.cache/torch

WORKDIR /opt/ani-voice-api

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        git \
        git-lfs \
        libsndfile1 \
        python3 \
        python3-pip \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv \
    && python -m pip install --no-cache-dir --upgrade pip setuptools wheel

RUN git lfs install \
    && git clone https://huggingface.co/beleata74/Ani-Voice-API . \
    && git checkout "${ANI_VOICE_REV}" \
    && git lfs pull

# Ani's repository pins unreleased CUDA 13.2 nightly torch wheels. Keep that
# dependency risk in this sidecar and use stable CUDA 12.8 wheels here.
RUN python -m pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cu128 \
        "torch>=2.7.0" \
        "torchaudio>=2.7.0" \
        "torchvision>=0.22.0"

RUN python -m pip install --no-cache-dir \
        "numpy<2.0.0" \
        "miocodec @ git+https://github.com/Aratako/MioCodec" \
        "supertonic==1.3.1" \
        "bg-text-normalizer==1.1.0" \
        "num2cyrillic==1.0.0" \
        "fastapi>=0.110.0" \
        "uvicorn>=0.29.0" \
        "pydantic>=2.7.0" \
        "requests>=2.31.0"

EXPOSE 8000

CMD ["python", "api.py"]
