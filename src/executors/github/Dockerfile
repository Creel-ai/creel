FROM creel-executor-base:latest

# Switch to root to install gh CLI from GitHub's official Debian repo
USER root
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
USER executor

COPY --chown=executor:executor github/executor.py ./executor.py
ENTRYPOINT ["python", "executor.py"]
