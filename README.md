# Calibrate Backend

The backend for Calibrate.

## Installation

Install dependencies using [uv](https://docs.astral.sh/uv/):

```bash
uv sync --frozen
```

## Running Locally

Start the development server:

```bash
cd src
uv run uvicorn main:app --reload
```

The app will be available at: http://localhost:8000

API documentation: http://localhost:8000/docs
