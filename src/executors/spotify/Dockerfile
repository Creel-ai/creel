FROM creel-executor-base:latest
COPY --chown=executor:executor spotify/requirements.lock ./requirements.lock
RUN uv pip install --system --no-cache -r requirements.lock
COPY --chown=executor:executor spotify/executor.py ./executor.py
ENTRYPOINT ["python", "executor.py"]
