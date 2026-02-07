# syntax=docker/dockerfile:1
FROM python:3.11-slim

# Install build dependencies, audio libraries, and ffmpeg
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    make \
    libasound2-dev \
    portaudio19-dev \
    ffmpeg \
    vim \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN uv sync --frozen --no-dev --no-install-project

# Download NLTK data required by pipecat (nltk is installed via calibrate-agent)
RUN uv run python -c "import nltk; nltk.download('punkt_tab', quiet=True)"

# Copy application code
COPY src/ ./src/

WORKDIR /app/src

# Expose the port
EXPOSE 8000

# Run the application
CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

