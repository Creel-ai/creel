FROM creel-executor-base:latest

COPY --chown=executor:executor cal_availability/requirements.lock ./requirements.lock
RUN uv pip install --system --no-cache -r requirements.lock

COPY --chown=executor:executor google_creds.py .
COPY --chown=executor:executor cal_availability/executor.py ./executor.py

ENTRYPOINT ["python", "executor.py"]
