FROM ghcr.io/astral-sh/uv:0.9.17@sha256:5cb6b54d2bc3fe2eb9a8483db958a0b9eebf9edff68adedb369df8e7b98711a2 AS uv
FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       tesseract-ocr=5.3.0-2 tesseract-ocr-deu=1:4.1.0-2 tesseract-ocr-eng=1:4.1.0-2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-editable
COPY src/ src/
RUN uv sync --locked --no-dev --no-editable \
    && groupadd --gid 10001 splitter \
    && useradd --uid 10001 --gid 10001 --no-create-home splitter

ARG VCS_REF=unknown
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TOKENIZERS_PARALLELISM=false \
    HF_HUB_OFFLINE=1 \
    SPLIT_IMAGE_REVISION=${VCS_REF}
LABEL org.opencontainers.image.source="https://github.com/nabbl/doc-splitter" \
      org.opencontainers.image.revision="${VCS_REF}"
USER 10001:10001
HEALTHCHECK --interval=15s --timeout=5s --start-period=30m --retries=3 \
    CMD ["doc-splitter", "health"]
ENTRYPOINT ["doc-splitter"]
CMD ["run"]
