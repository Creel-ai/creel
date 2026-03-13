FROM creel-executor-base:latest

# Switch to root to install system packages: Node.js 20, git, build tools, common dev utilities
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        wget \
        git \
        jq \
        gcc \
        g++ \
        make \
        cmake \
        pkg-config \
        libssl-dev \
        libffi-dev \
        openssh-client \
        sqlite3 \
        unzip \
        zip \
        tree \
        ripgrep \
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

# Default workspace directory
RUN mkdir -p /workspace && chown executor:executor /workspace

USER executor
WORKDIR /workspace

# Copy executor scripts
COPY --chown=executor:executor coding/executor.py /app/executor.py
COPY --chown=executor:executor coding/dev_runner.py /app/dev_runner.py

ENTRYPOINT ["python", "/app/executor.py"]
