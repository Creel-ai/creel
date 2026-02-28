FROM python:3.12-alpine

# Install gh CLI
RUN apk add --no-cache github-cli

# Create executor user
RUN adduser -D -u 1000 executor

USER executor
WORKDIR /app

# Copy executor script
COPY --chown=executor:executor github/executor.py ./executor.py

ENTRYPOINT ["python", "executor.py"]
