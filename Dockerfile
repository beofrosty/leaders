
# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Install system deps (curl for healthchecks / debugging)
RUN apt-get update && apt-get install -y --no-install-recommends         curl tini && rm -rf /var/lib/apt/lists/*

# Create a non-root user
RUN useradd -m appuser
WORKDIR /app

# Copy only dependency files first for better caching
COPY requirements.txt /app/requirements.txt

# Ensure python doesn't buffer logs and writes .pyc files to disk
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1


# Install dependencies
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /app/requirements.txt \
    && pip install --no-cache-dir python-dotenv

# Copy the rest of the app
COPY . /app

# Switch to the non-root user
USER appuser

EXPOSE 8000

# Use tini as PID 1 for proper signal handling
ENTRYPOINT ["/usr/bin/tini", "--"]

# Default command: run via gunicorn
# Module is forum.run:app (run.py creates the Flask app via create_app())
CMD ["gunicorn","-w","4","-k","gthread","-b","0.0.0.0:8000","run:app"]
