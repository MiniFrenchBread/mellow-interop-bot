FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY abi/ ./abi/
COPY src/ ./src/
COPY config.json .

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

# The scheduler, not the one-shot oracle report: main.py runs a single pass
# and exits, which is not what a container of this bot should do.
CMD ["python", "-u", "src/scheduler.py"]
