FROM creel-executor-base:latest
COPY --chown=executor:executor file_ops/executor.py ./executor.py
ENTRYPOINT ["python", "executor.py"]
