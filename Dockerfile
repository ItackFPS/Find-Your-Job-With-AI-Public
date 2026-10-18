# Build stage
FROM python:3.12-slim AS builder

WORKDIR /app

COPY . .
RUN pip install --no-cache-dir --prefix=/install -e .

# Runtime stage
FROM python:3.12-slim

WORKDIR /app

# Create data directory for SQLite persistence
RUN mkdir -p /data

COPY --from=builder /install /usr/local
COPY . .

EXPOSE 8000

CMD ["python", "src/main.py"]
