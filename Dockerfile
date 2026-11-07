FROM node:20-slim AS frontend-build

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ .
ARG VITE_NEON_AUTH_URL=https://ep-shy-violet-b305q0j3.neonauth.c-4.ap-southeast-1.aws.neon.tech/neondb/auth
ENV VITE_NEON_AUTH_URL=${VITE_NEON_AUTH_URL}
RUN npm run build

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
COPY wheels ./wheels

RUN if ls /app/wheels/*.whl >/dev/null 2>&1; then \
      pip install --no-cache-dir --no-index --find-links=/app/wheels -r requirements.txt; \
    else \
      pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple torch && \
      pip install --no-cache-dir -r requirements.txt; \
    fi

COPY app ./app
COPY ingest ./ingest
COPY eval ./eval
COPY evaluation ./evaluation
COPY scripts ./scripts
COPY --from=frontend-build /frontend/dist ./frontend/dist

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]