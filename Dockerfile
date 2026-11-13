FROM node:20-slim AS frontend-build

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ .
ARG VITE_NEON_AUTH_URL
RUN if [ -n "$VITE_NEON_AUTH_URL" ]; then \
      VITE_NEON_AUTH_URL="$VITE_NEON_AUTH_URL" npm run build; \
    else \
      npm run build; \
    fi

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
COPY wheels ./wheels

RUN if ls /app/wheels/*.whl >/dev/null 2>&1; then \
      pip install --no-cache-dir --no-index --find-links=/app/wheels -r requirements.txt; \
    else \
      pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple torch && \
      pip install --no-cache-dir -r requirements.txt; \
    fi

COPY pyproject.toml .
COPY app ./app
COPY ingest ./ingest
COPY eval ./eval
COPY evaluation ./evaluation
COPY scripts ./scripts
COPY tests ./tests
COPY --from=frontend-build /frontend/dist ./frontend/dist

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]