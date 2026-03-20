FROM creel-executor-base:latest
COPY --chown=executor:executor pdf_reader/requirements.lock ./requirements.lock
RUN uv pip install --system --no-cache -r requirements.lock
COPY --chown=executor:executor pdf_reader/executor.py ./executor.py
ENTRYPOINT ["python", "executor.py"]
