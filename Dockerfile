FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir --retries 10 --timeout 120 \
    --index-url https://download.pytorch.org/whl/cpu torch \
    && pip install --no-cache-dir --retries 10 --timeout 120 -r requirements.txt

COPY app ./app
COPY ingest ./ingest
COPY eval ./eval
COPY scripts ./scripts

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]