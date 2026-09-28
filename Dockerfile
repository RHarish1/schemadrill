FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
COPY wheels ./wheels

RUN pip install --no-cache-dir --no-index --find-links=/app/wheels -r requirements.txt

COPY app ./app
COPY ingest ./ingest
COPY eval ./eval
COPY evaluation ./evaluation
COPY scripts ./scripts

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]