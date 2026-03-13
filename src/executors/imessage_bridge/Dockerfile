FROM creel-executor-base:latest
COPY --chown=executor:executor imessage_bridge/requirements.lock ./requirements.lock
RUN uv pip install --system --no-cache -r requirements.lock
COPY --chown=executor:executor imessage_bridge/executor.py ./executor.py
ENTRYPOINT ["python", "executor.py"]
