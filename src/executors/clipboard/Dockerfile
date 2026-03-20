FROM creel-executor-base:latest
COPY --chown=executor:executor clipboard/requirements.lock ./requirements.lock
RUN uv pip install --system --no-cache -r requirements.lock
COPY --chown=executor:executor clipboard/executor.py ./executor.py
ENTRYPOINT ["python", "executor.py"]
