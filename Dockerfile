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

# Copy calibrate CLI source so path dep ../calibrate resolves to /calibrate
COPY calibrate/ /calibrate/

# Copy backend dependency files
COPY calibrate-backend/pyproject.toml calibrate-backend/uv.lock ./

# Install dependencies (calibrate-agent resolved from /calibrate via path dep)
RUN uv sync --frozen --no-dev --no-install-project

# Put venv on PATH so subprocess.Popen("calibrate ...") works without uv run
ENV PATH="/app/.venv/bin:$PATH"

# Download NLTK data required by pipecat (nltk is installed via calibrate-agent)
RUN python -c "import nltk; nltk.download('punkt_tab', quiet=True)"

# Copy application code
COPY calibrate-backend/src/ ./src/

WORKDIR /app/src

# Expose the port
EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

