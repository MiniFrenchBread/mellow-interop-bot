FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY abi/ ./abi/
COPY src/ ./src/
COPY config.json .

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

# The scheduler records when each task last ran so a restart cannot skip a due
# slot. Without a volume that record dies with the container and a redeploy
# just after an interval boundary silently forfeits that run -- a fortnight,
# for the reward distribution.
VOLUME ["/app/state"]
ENV SCHEDULER_STATE_FILE=/app/state/scheduler-state.json

# The scheduler, not the one-shot oracle report: main.py runs a single pass
# and exits, which is not what a container of this bot should do.
CMD ["python", "-u", "src/scheduler.py"]
