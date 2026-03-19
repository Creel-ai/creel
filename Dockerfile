FROM python:3.12-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
        age \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv

RUN useradd -m -u 1000 creel
WORKDIR /app

# Copy project files
COPY pyproject.toml README.md LICENSE ./
COPY src/ src/

# Install the package with guardian extras
RUN uv pip install --system --no-cache ".[guardian]"

USER creel

EXPOSE 8080

ENTRYPOINT ["creel"]
CMD ["daemon", "run"]
