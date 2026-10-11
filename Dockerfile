FROM node:20-slim AS frontend-build

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ .
RUN npm run build

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
COPY --from=frontend-build /frontend/dist ./frontend/dist

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]