# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set work directory
WORKDIR /app


# Install runtime dependencies first
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        portaudio19-dev \
        libasound2-dev \
        libpulse-dev \
        ffmpeg \
        && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt /app/

# Install build dependencies, install Python packages, then remove build deps and clean up
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential && \
    pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    apt-get purge -y build-essential && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# Copy app source
COPY . /app/

# Expose the web port
EXPOSE 5000

# Run the app with Gunicorn and eventlet for production
CMD ["gunicorn", "-k", "eventlet", "-b", "0.0.0.0:5000", "app:app"]
