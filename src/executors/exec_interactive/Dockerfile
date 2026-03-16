FROM creel-executor-base:latest

# Switch to root to install openssh-client for SSH use case
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
        openssh-client \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
USER executor

ENV PYTHONDONTWRITEBYTECODE=1

COPY --chown=executor:executor exec_interactive/executor.py ./executor.py
COPY --chown=executor:executor exec_interactive/pty_runner.py ./pty_runner.py

ENTRYPOINT ["python", "pty_runner.py"]
