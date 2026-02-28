FROM python:3.12-slim

# Install system packages: Node.js 20, git, build tools, common dev utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        wget \
        git \
        jq \
        gcc \
        g++ \
        make \
        ca-certificates \
        gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install uv (fast Python package manager)
RUN pip install --no-cache-dir uv

# Create executor user
RUN useradd -m -u 1000 executor

# Default workspace directory
RUN mkdir -p /workspace && chown executor:executor /workspace

USER executor
WORKDIR /workspace

# Copy executor script
COPY --chown=executor:executor coding/executor.py /app/executor.py

ENTRYPOINT ["python", "/app/executor.py"]
