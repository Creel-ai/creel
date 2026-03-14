FROM python:3.12-slim

RUN useradd -m -u 1000 llmrunner

USER llmrunner
WORKDIR /app

COPY --chown=llmrunner:llmrunner requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

COPY --chown=llmrunner:llmrunner _provider.py runner.py agent_runner.py .

ENTRYPOINT ["python"]
CMD ["runner.py"]
