FROM creel-executor-base:latest
COPY --chown=executor:executor git_ops/requirements.lock ./requirements.lock
RUN uv pip install --system --no-cache -r requirements.lock
COPY --chown=executor:executor git_ops/executor.py ./executor.py
ENTRYPOINT ["python", "executor.py"]
