FROM python:3.12-slim

RUN useradd -m -u 1000 executor

USER executor
WORKDIR /app

COPY --chown=executor:executor google_creds.py .
COPY --chown=executor:executor gmail_send/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --user -r requirements.txt

COPY --chown=executor:executor gmail_send/executor.py ./executor.py

ENTRYPOINT ["python", "executor.py"]
