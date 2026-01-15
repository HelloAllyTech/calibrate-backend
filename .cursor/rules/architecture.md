---
description: "Pense Backend Architecture - AI Agent Testing & Evaluation Platform"
alwaysApply: true
---

# Pense Backend Architecture

## Project Overview

**Pense Backend** is a FastAPI-based REST API that serves as the backend for an AI agent testing and evaluation platform. It provides capabilities for:

1. **Speech-to-Text (STT) Evaluation** - Benchmark multiple STT providers against ground truth transcriptions
2. **Text-to-Speech (TTS) Evaluation** - Benchmark multiple TTS providers for quality metrics
3. **LLM Agent Testing** - Run unit tests and benchmarks on LLM-based agents
4. **Voice/Chat Simulations** - Run simulated conversations between AI agents and personas across various scenarios

The backend wraps the `pense` CLI tool and orchestrates evaluation jobs while providing a RESTful API interface.

---

## Technology Stack

| Component            | Technology         | Purpose                                                      |
| -------------------- | ------------------ | ------------------------------------------------------------ |
| **Framework**        | FastAPI            | Async REST API framework                                     |
| **Database**         | SQLite             | Persistent data storage                                      |
| **Storage**          | AWS S3             | File/result storage                                          |
| **Authentication**   | Google OAuth + JWT | User authentication via Google ID tokens, API access via JWT |
| **Package Manager**  | uv                 | Python dependency management                                 |
| **Containerization** | Docker             | Deployment                                                   |
| **CLI Tool**         | pense              | Core evaluation/simulation engine                            |

---

## Project Structure

```
pense-backend/
├── src/
│   ├── main.py              # FastAPI app entry point, lifespan management
│   ├── db.py                # SQLite database layer (~2300 lines)
│   ├── utils.py             # Shared utilities (S3 client, port finding)
│   ├── job_recovery.py      # Restart in-progress jobs on app startup
│   └── routers/
│       ├── auth.py          # Google OAuth authentication
│       ├── users.py         # User management endpoints
│       ├── agents.py        # Agent CRUD operations
│       ├── tools.py         # Tool CRUD operations
│       ├── agent_tools.py   # Agent-Tool relationship management
│       ├── tests.py         # Test case CRUD operations
│       ├── agent_tests.py   # Agent test execution & benchmarking
│       ├── personas.py      # Persona CRUD operations
│       ├── scenarios.py     # Scenario CRUD operations
│       ├── metrics.py       # Metric/evaluation criteria CRUD
│       ├── simulations.py   # Simulation orchestration (chat/voice)
│       ├── stt.py           # STT provider evaluation
│       └── tts.py           # TTS provider evaluation
├── db/
│   └── pense.db             # SQLite database file
├── pyproject.toml           # Python project configuration
├── Dockerfile               # Container build configuration
└── docker-compose.yml       # Container orchestration
```

---

## Database Schema

### Entity Relationship

```
users
  ├── agents (user_id FK)
  ├── tools (user_id FK)
  ├── tests (user_id FK)
  ├── personas (user_id FK)
  ├── scenarios (user_id FK)
  ├── metrics (user_id FK)
  └── simulations (user_id FK)

agents
  ├── agent_tools (many-to-many with tools)
  ├── agent_tests (many-to-many with tests)
  ├── agent_test_jobs
  └── simulations (agent_id FK)

simulations
  ├── simulation_personas (many-to-many with personas)
  ├── simulation_scenarios (many-to-many with scenarios)
  ├── simulation_metrics (many-to-many with metrics)
  └── simulation_jobs
```

### Core Tables

| Table             | Purpose                                                                |
| ----------------- | ---------------------------------------------------------------------- |
| `users`           | User accounts (Google OAuth)                                           |
| `agents`          | AI agent configurations (system prompt, LLM config, STT/TTS settings)  |
| `tools`           | Tool/function definitions for agents                                   |
| `tests`           | Test cases with evaluation criteria                                    |
| `personas`        | Simulated user personas (characteristics, gender, language)            |
| `scenarios`       | Conversation scenarios/contexts                                        |
| `metrics`         | Evaluation criteria for simulations                                    |
| `simulations`     | Simulation configurations linking agents, personas, scenarios, metrics |
| `jobs`            | Generic STT/TTS evaluation jobs                                        |
| `agent_test_jobs` | LLM unit test and benchmark jobs                                       |
| `simulation_jobs` | Chat/voice simulation jobs                                             |

### Design Patterns

1. **Soft Deletes**: All entity tables use `deleted_at` timestamp for soft deletion
2. **UUIDs**: All entities use UUID as primary identifier (separate from auto-increment id)
3. **JSON Config**: Complex configurations stored as JSON strings in `config` columns
4. **Pivot Tables**: Many-to-many relationships use dedicated pivot tables with soft delete support

---

## API Architecture

### Router Organization

Each router follows a consistent pattern:

- Pydantic models for request/response validation
- CRUD endpoints following REST conventions
- Background job execution for long-running tasks

### API Endpoints

#### Authentication

- `POST /auth/google` - Google OAuth login (returns JWT access token)

**JWT Token Usage**: After login, all API requests must include the JWT token in the Authorization header:

```
Authorization: Bearer <jwt_token>
```

The JWT token contains the user's UUID and is validated on every protected endpoint.

#### Entity Management (CRUD)

- `/agents` - Agent management
- `/tools` - Tool definitions
- `/tests` - Test cases
- `/personas` - User personas
- `/scenarios` - Conversation scenarios
- `/metrics` - Evaluation metrics
- `/simulations` - Simulation configurations
- `/users` - User management (read-only)

#### Relationship Management

- `/agent-tools` - Link/unlink tools to agents
- `/agent-tests` - Link/unlink tests to agents

#### Evaluation & Testing

- `POST /stt/evaluate` - Start STT evaluation task
- `GET /stt/evaluate/{task_id}` - Get STT evaluation status
- `POST /tts/evaluate` - Start TTS evaluation task
- `GET /tts/evaluate/{task_id}` - Get TTS evaluation status
- `POST /agent-tests/agent/{uuid}/run` - Run agent unit tests
- `POST /agent-tests/agent/{uuid}/benchmark` - Run multi-model benchmark
- `GET /agent-tests/run/{task_id}` - Get test run status
- `GET /agent-tests/benchmark/{task_id}` - Get benchmark status

#### Simulations

- `POST /simulations/{uuid}/run` - Start simulation (chat or voice)
- `GET /simulations/run/{task_id}` - Get simulation run status
- `GET /simulations/{uuid}/runs` - List all runs for a simulation

#### Utilities

- `GET /` - Health check
- `POST /presigned-url` - Generate S3 presigned URL for uploads

---

## Background Job System

### Job Types

| Job Type        | Table             | Description               |
| --------------- | ----------------- | ------------------------- |
| `stt-eval`      | `jobs`            | STT provider evaluation   |
| `tts-eval`      | `jobs`            | TTS provider evaluation   |
| `llm-unit-test` | `agent_test_jobs` | Agent unit test execution |
| `llm-benchmark` | `agent_test_jobs` | Multi-model benchmarking  |
| `chat`          | `simulation_jobs` | Chat-based simulation     |
| `voice`         | `simulation_jobs` | Voice-based simulation    |

### Job Lifecycle

1. **Creation**: Job created with `in_progress` status and `details` JSON containing recovery info
2. **Execution**: Background thread runs the task
3. **Completion**: Job updated with `done` status and `results` JSON
4. **Recovery**: On app startup, `job_recovery.py` restarts any `in_progress` jobs

### Job Status Values

- `in_progress` - Job is running
- `done` - Job completed (check `results.error` for failure)
- `cancelled` - Job was cancelled (not currently used)

---

## External Integrations

### Pense CLI

The backend orchestrates the `pense` CLI tool for actual evaluations:

```bash
# STT Evaluation
pense stt eval -p <provider> -l <language> -i <input_dir> -o <output_dir>
pense stt leaderboard -o <output_dir> -s <summary_dir>

# TTS Evaluation
pense tts eval -p <provider> -l <language> -i <input_csv> -o <output_dir>
pense tts leaderboard -o <output_dir> -s <summary_dir>

# LLM Tests
pense llm tests run -c <config.json> -o <output_dir> -m <model>
pense llm tests leaderboard -o <output_dir> -s <summary_dir>

# LLM Simulations
pense llm simulations run -c <config.json> -o <output_dir> -m <model>

# Voice Agent Simulation
pense agent simulation -c <config.json> -o <output_dir>
```

### AWS S3

Used for:

- Storing input audio files (STT evaluation)
- Storing generated audio files (TTS evaluation, voice simulations)
- Storing evaluation results (JSON, CSV, Excel files)
- Presigned URLs for secure file access

Required environment variables:

- `S3_OUTPUT_BUCKET` - Target bucket for outputs
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (optional, falls back to IAM role)
- `AWS_REGION` - Default: `ap-south-1`

### Supported Providers

**STT Providers**: deepgram, openai, sarvam, google, etc.

**TTS Providers**: smallest, cartesia, openai, google, elevenlabs, etc.

**LLM Providers**: OpenAI, Groq, OpenRouter, Google (via respective API keys)

---

## Configuration

### Environment Variables

```bash
# Database
DB_ROOT_DIR=/appdata/db          # Directory containing pense.db

# CORS
CORS_ALLOWED_ORIGINS=*           # Comma-separated origins (e.g., "http://localhost:3000,https://app.example.com")

# JWT Authentication
JWT_SECRET_KEY=your-secret-key   # REQUIRED: At least 32 characters, change in production!
JWT_EXPIRATION_HOURS=168         # Token validity (default: 7 days)

# AWS
S3_OUTPUT_BUCKET=your-bucket
AWS_ACCESS_KEY_ID=xxx
AWS_SECRET_ACCESS_KEY=xxx
AWS_REGION=ap-south-1

# Authentication
GOOGLE_CLIENT_ID=xxx.apps.googleusercontent.com

# Provider API Keys
OPENAI_API_KEY=xxx
DEEPGRAM_API_KEY=xxx
CARTESIA_API_KEY=xxx
SMALLEST_API_KEY=xxx
GROQ_API_KEY=xxx
GOOGLE_API_KEY=xxx
GOOGLE_APPLICATION_CREDENTIALS=/path/to/credentials.json
SARVAM_API_KEY=xxx
ELEVENLABS_API_KEY=xxx
OPENROUTER_API_KEY=xxx
```

### Agent Configuration Schema

```json
{
  "system_prompt": "You are a helpful assistant...",
  "llm": {
    "provider": "openai",
    "model": "gpt-4.1"
  },
  "stt": {
    "provider": "deepgram",
    "model": "nova-2"
  },
  "tts": {
    "provider": "elevenlabs",
    "voice_id": "xxx"
  },
  "speaks_first": true
}
```

### Test Case Configuration Schema

```json
{
  "input": "What's the weather in Mumbai?",
  "evaluation": {
    "type": "tool_call",
    "tool_calls": [
      {
        "tool": "get_weather",
        "arguments": { "city": "Mumbai" },
        "accept_any_arguments": false
      }
    ]
  }
}
```

### Persona Configuration Schema

```json
{
  "gender": "female",
  "language": "english",
  "interruption_sensitivity": "medium"
}
```

---

## Key Design Decisions

### 1. SQLite over PostgreSQL

- **Rationale**: Simpler deployment, no external database service needed
- **Trade-off**: Single-writer limitation, not horizontally scalable
- **Mitigation**: Using connection context manager for proper cleanup

### 2. Threading over Async Tasks

- **Rationale**: Long-running subprocess calls to `pense` CLI
- **Implementation**: Python `threading.Thread` with `daemon=True`
- **Recovery**: Job details stored in DB, recovered on app restart

### 3. Soft Deletes

- **Rationale**: Data preservation, audit trail, easy recovery
- **Implementation**: `deleted_at` timestamp, queries filter `deleted_at IS NULL`

### 4. JSON Configuration Columns

- **Rationale**: Flexible schemas for agent/tool/test configs
- **Trade-off**: No database-level validation
- **Mitigation**: Pydantic validation at API layer

### 5. Presigned URLs for S3

- **Rationale**: Secure, time-limited access without exposing credentials
- **Implementation**: 1-hour expiration for generated URLs

### 6. Background Job Pattern

- **Rationale**: STT/TTS/LLM evaluations are long-running (minutes)
- **Pattern**: Create job → Return task_id → Poll for status
- **Recovery**: Jobs with `in_progress` status restarted on app boot

---

## Dependencies

Key Python packages:

- `fastapi>=0.127.0` - Web framework
- `uvicorn>=0.40.0` - ASGI server
- `boto3>=1.34.0` - AWS SDK for S3
- `pydantic>=2.0.0` - Data validation
- `python-dotenv>=1.0.0` - Environment variable loading
- `openpyxl>=3.1.5` - Excel file parsing for leaderboards
- `httpx>=0.27.0` - Async HTTP client for Google OAuth
- `python-jose[cryptography]>=3.3.0` - JWT token encoding/decoding

External:

- `pense` CLI (installed via wheel file: `pense-0.1.0-py3-none-any.whl`)
- `ffmpeg` - Audio format conversion

---

## Deployment

### Docker Build

```bash
docker build -t pense-backend .
```

### Docker Compose

```bash
docker-compose up -d
```

The container:

- Exposes port 8000
- Mounts `/appdata` volume for database and credentials
- Requires environment variables via `.env` file or docker-compose

### Development

```bash
cd src
uv run uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

---

## Common Patterns in Codebase

### Database Function Pattern

```python
def get_entity(uuid: str) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM entities WHERE uuid = ? AND deleted_at IS NULL",
            (uuid,)
        )
        row = cursor.fetchone()
        if row:
            return _parse_entity_row(row)
        return None
```

### Router Endpoint Pattern (with JWT Auth)

```python
from auth_utils import get_current_user_id

@router.get("/{uuid}", response_model=EntityResponse)
async def get_entity_endpoint(uuid: str, user_id: str = Depends(get_current_user_id)):
    entity = get_entity(uuid)
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")
    # Verify user owns this entity
    if entity.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    return entity
```

### Background Task Pattern

```python
@router.post("/run", response_model=TaskCreateResponse)
async def start_task(request: TaskRequest):
    job_id = create_job(
        job_type="task-type",
        status=TaskStatus.IN_PROGRESS.value,
        details={"param": value, "s3_bucket": bucket},
    )

    thread = threading.Thread(
        target=run_task,
        args=(job_id, request, s3_bucket),
        daemon=True,
    )
    thread.start()

    return TaskCreateResponse(task_id=job_id, status=TaskStatus.IN_PROGRESS.value)
```

---

## Future Considerations

1. **Scalability**: Consider PostgreSQL + Redis for multi-instance deployment
2. **Task Queue**: Consider Celery/RQ for more robust job management
3. **Caching**: Add Redis caching for frequently accessed data
4. **Rate Limiting**: Add rate limiting for API protection
5. **Pagination**: Implement pagination for list endpoints as data grows
