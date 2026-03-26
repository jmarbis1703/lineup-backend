FROM python:3.11-slim

WORKDIR /app
ENV PYTHONPATH=/app

# System dependencies required by asyncpg and bcrypt
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# SEC-15: Run as a non-root system user
RUN addgroup --system appgroup && \
    adduser --system --ingroup appgroup appuser
USER appuser

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
