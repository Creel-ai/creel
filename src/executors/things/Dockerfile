FROM python:3.12-slim

RUN useradd -m -u 1000 executor

USER executor
WORKDIR /app

COPY --chown=executor:executor things/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --user -r requirements.txt

COPY --chown=executor:executor things/executor.py ./executor.py

ENTRYPOINT ["python", "executor.py"]
