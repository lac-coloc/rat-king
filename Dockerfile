# syntax=docker/dockerfile:1
FROM python:3.14.7-slim-trixie@sha256:cae66f2ef0ec51a9891263eeee7f987dacf0a9879e8aa9353d5606e0530619a5 AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml requirements.lock README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir "pip==25.2" \
    && python -m pip wheel --wheel-dir /wheels --requirement requirements.lock \
    && python -m pip wheel --wheel-dir /wheels --no-deps .

FROM python:3.14.7-slim-trixie@sha256:cae66f2ef0ec51a9891263eeee7f987dacf0a9879e8aa9353d5606e0530619a5 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BK_REFRESH_INTERVAL_SECONDS=21600 \
    BK_OUTPUT_DIR=/data/site \
    BK_STATE_DIR=/data \
    BK_HTTP_TIMEOUT_SECONDS=30 \
    BK_CACHE_MAX_RESTAURANTS=20 \
    BK_REFRESH_QUEUE_MAX=10 \
    BK_COLD_LOADS_PER_HOUR=6 \
    BK_SEARCH_MAX_RESULTS=20 \
    PORT=8080

RUN groupadd --gid 10001 rat-king \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin rat-king \
    && install -d -o 10001 -g 10001 /data

COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels "rat-king==0.2.3" \
    && rm -rf /wheels

USER 10001:10001
WORKDIR /data
EXPOSE 8080

ENTRYPOINT ["rat-king"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080", "--refresh-interval", "21600"]
