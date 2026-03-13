FROM creel-executor-base:latest
COPY --chown=executor:executor apple_notes/requirements.lock ./requirements.lock
RUN uv pip install --system --no-cache -r requirements.lock
COPY --chown=executor:executor apple_notes/executor.py ./executor.py
ENTRYPOINT ["python", "executor.py"]
