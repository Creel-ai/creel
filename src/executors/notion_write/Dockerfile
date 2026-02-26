FROM python:3.12-slim

RUN useradd -m -u 1000 executor

USER executor
WORKDIR /app

COPY --chown=executor:executor notion/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --user -r requirements.txt

# Copy the read executor (imported by notion_write for shared helpers)
COPY --chown=executor:executor notion/ ./executors/notion/

# Copy the write executor
COPY --chown=executor:executor notion_write/__init__.py notion_write/executor.py ./executors/notion_write/
COPY --chown=executor:executor notion_write/executor.py ./executor.py

ENTRYPOINT ["python", "executor.py"]
