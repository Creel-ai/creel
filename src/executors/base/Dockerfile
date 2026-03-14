FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
RUN useradd -m -u 1000 executor \
    && chmod -R a+rwX /usr/local/lib/python3.12/site-packages/ \
    && chmod -R a+rwX /usr/local/bin/
USER executor
WORKDIR /app
