FROM python:3.12-slim

RUN groupadd -r executor && useradd -r -g executor executor

WORKDIR /app
COPY bluebubbles/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY bluebubbles/executor.py ./executor.py

USER executor
ENTRYPOINT ["python", "executor.py"]
