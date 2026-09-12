FROM python:3.11-slim

# ffmpeg is required for merging video+audio and audio extraction
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/downloads

EXPOSE 5000

# gunicorn for production; single worker keeps ffmpeg merges from competing for CPU/RAM on small instances
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--timeout", "300", "--workers", "1", "app:app"]
