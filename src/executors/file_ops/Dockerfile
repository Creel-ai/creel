FROM python:3.12-slim

RUN useradd -m -u 1000 executor

USER executor
WORKDIR /app

COPY --chown=executor:executor file_ops/requirements.txt ./requirements.txt

COPY --chown=executor:executor file_ops/executor.py ./executor.py

ENTRYPOINT ["python", "executor.py"]
