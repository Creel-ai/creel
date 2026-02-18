FROM python:3.12-alpine

# Install basic shell tools
RUN apk add --no-cache \
    bash \
    grep \
    sed \
    gawk \
    coreutils \
    findutils \
    curl \
    jq \
    tar \
    gzip

# Create executor user
RUN adduser -D -u 1000 executor

USER executor
WORKDIR /app

# Copy executor script
COPY --chown=executor:executor exec/executor.py ./executor.py

ENTRYPOINT ["python", "executor.py"]
