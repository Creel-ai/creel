FROM creel-executor-base:latest

COPY --chown=executor:executor gcal_write/requirements.lock ./requirements.lock
RUN uv pip install --system --no-cache -r requirements.lock

COPY --chown=executor:executor google_creds.py .
COPY --chown=executor:executor gcal_write/executor.py ./executor.py

ENTRYPOINT ["python", "executor.py"]
