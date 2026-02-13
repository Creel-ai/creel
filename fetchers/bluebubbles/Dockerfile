FROM python:3.12-slim

RUN groupadd -r fetcher && useradd -r -g fetcher fetcher

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY fetcher.py .

USER fetcher
ENTRYPOINT ["python", "fetcher.py"]
