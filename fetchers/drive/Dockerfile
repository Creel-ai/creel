FROM python:3.12-slim

RUN useradd -m -u 1000 fetcher

USER fetcher
WORKDIR /app

COPY --chown=fetcher:fetcher requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

COPY --chown=fetcher:fetcher fetcher.py .

ENTRYPOINT ["python", "fetcher.py"]
