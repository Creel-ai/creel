FROM creel-executor-base:latest
COPY --chown=executor:executor notion/requirements.lock ./requirements.lock
RUN uv pip install --system --no-cache -r requirements.lock

# Copy the read executor (imported by notion_write for shared helpers)
COPY --chown=executor:executor notion/ ./executors/notion/

# Copy the write executor
COPY --chown=executor:executor notion_write/__init__.py notion_write/executor.py ./executors/notion_write/
COPY --chown=executor:executor notion_write/executor.py ./executor.py
ENTRYPOINT ["python", "executor.py"]
