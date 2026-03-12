FROM creel-executor-base:latest

# Switch to root to install system packages
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        jq \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
USER executor

COPY --chown=executor:executor exec/executor.py ./executor.py
ENTRYPOINT ["python", "executor.py"]
