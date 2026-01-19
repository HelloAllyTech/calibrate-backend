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
│       ├── tts.py           # TTS provider evaluation
│       └── jobs.py          # Job listing API (STT/TTS eval jobs)
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
  ├── simulations (user_id FK)
  └── jobs (user_id FK)

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
| `jobs`            | Generic STT/TTS evaluation jobs (user_id FK to users)                  |
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

#### Evaluation & Testing (all require JWT auth)

- `GET /jobs` - List all STT/TTS evaluation jobs for authenticated user
- `POST /stt/evaluate` - Start STT evaluation task
- `GET /stt/evaluate/{task_id}` - Get STT evaluation status
- `POST /tts/evaluate` - Start TTS evaluation task
- `GET /tts/evaluate/{task_id}` - Get TTS evaluation status
- `POST /agent-tests/agent/{uuid}/run` - Run agent unit tests
- `POST /agent-tests/agent/{uuid}/benchmark` - Run multi-model benchmark
- `GET /agent-tests/agent/{uuid}/runs` - List all test runs for an agent
- `GET /agent-tests/run/{task_id}` - Get test run status
- `GET /agent-tests/benchmark/{task_id}` - Get benchmark status

#### Simulations

- `POST /simulations/{uuid}/run` - Start simulation (chat or voice)
- `GET /simulations/run/{task_id}` - Get simulation run status (includes partial results for voice simulations)
- `GET /simulations/{uuid}/runs` - List all runs for a simulation

**Status API Response Fields:**

- `total_simulations` - Expected number of simulations (personas × scenarios)
- `completed_simulations` - Number of completed simulations (voice only, while in_progress)
- `simulation_results` - Array of completed simulation results (partial for in_progress voice simulations)
- `metrics` - Aggregated evaluation metrics (only populated when status is `done`)

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

1. **Request**: New job request received via API
2. **Capacity Check**: Check if `running_jobs < MAX_CONCURRENT_JOBS`
3. **Creation**: Job created with status based on capacity:
   - `in_progress` if capacity available (job starts immediately)
   - `queued` if capacity full (job waits in queue)
4. **Execution**: Background thread runs the task
5. **Process Tracking** (voice simulations only): PID and PGID stored in job details for cleanup
6. **Incremental Updates** (voice simulations only): Results updated in DB as each simulation completes
7. **Completion**: Job updated with `done` status and `results` JSON
8. **Queue Processing**: On completion, `try_start_queued_*_job()` starts next queued job if capacity allows
9. **Recovery**: On app startup, `job_recovery.py` kills orphaned processes, restarts `in_progress` jobs, and starts queued jobs

### Job Status Values

- `queued` - Job is waiting for capacity (FIFO order)
- `in_progress` - Job is running (may have partial results for voice simulations)
- `done` - Job completed (check `results.error` for failure)
- `cancelled` - Job was cancelled (not currently used)

### Job Queueing System

The queueing mechanism limits concurrent jobs to prevent resource exhaustion. Controlled by `MAX_CONCURRENT_JOBS` env var (default: 2).

#### Queue Architecture

Three separate queues exist, each with its own concurrency limit:

| Queue            | Job Types                        | Shared Limit          |
| ---------------- | -------------------------------- | --------------------- |
| Eval Queue       | `stt-eval`, `tts-eval`           | `MAX_CONCURRENT_JOBS` |
| Agent Test Queue | `llm-unit-test`, `llm-benchmark` | `MAX_CONCURRENT_JOBS` |
| Simulation Queue | `text`, `voice`                  | `MAX_CONCURRENT_JOBS` |

#### Key Components

| Component                  | Location   | Purpose                              |
| -------------------------- | ---------- | ------------------------------------ |
| `TaskStatus.QUEUED`        | `utils.py` | New status for queued jobs           |
| `_job_starters` registry   | `utils.py` | Maps job types to starter callbacks  |
| `register_job_starter()`   | `utils.py` | Registers callback for starting jobs |
| `can_start_*_job()`        | `utils.py` | Checks if capacity allows new job    |
| `try_start_queued_*_job()` | `utils.py` | Starts next queued job               |
| `get_queued_*()`           | `db.py`    | Gets queued jobs (FIFO order)        |
| `count_running_*()`        | `db.py`    | Counts in-progress jobs              |

#### Job Starter Registration

Each router registers its job starters at module load time:

```python
# In router module (e.g., stt.py)
def _start_stt_job_from_queue(job: dict) -> bool:
    # Reconstruct request from job details
    # Start background thread
    ...

register_job_starter("stt-eval", _start_stt_job_from_queue)
```

#### Queue Flow

```
New Job Request
      │
      ▼
Check: running_count < MAX_CONCURRENT_JOBS?
      │
      ├─── YES ──→ Create job (status=in_progress) ──→ Start immediately
      │
      └─── NO ───→ Create job (status=queued) ──→ Wait in queue

Job Completion
      │
      ▼
try_start_queued_*_job()
      │
      ├─── Capacity available? ──→ Start oldest queued job
      │
      └─── No capacity ──→ Do nothing
```

### Process Management for Long-Running Jobs

STT, TTS, and voice simulation jobs spawn subprocesses that run on specific ports. To handle server restarts gracefully:

- **Process isolation**: All subprocesses started with `start_new_session=True` (creates new process group)
- **PID tracking**: Process PIDs stored in job `details`:
  - Voice simulations: Single `pid` and `pgid` fields
  - STT/TTS evaluations: `running_pids` dict mapping provider name to PID (e.g., `{"deepgram": 12345, "openai": 12346}`)
- **Orphan cleanup**: On recovery, `job_recovery.py` kills process groups using `os.killpg()` before restarting
- **Graceful termination**: Sends SIGTERM first, waits briefly, then SIGKILL if still running

This prevents orphaned processes from accumulating across server restarts and frees up ports.

### STT/TTS Evaluation Incremental Updates

STT and TTS evaluations run multiple providers in parallel, with each provider processing multiple items sequentially. The backend monitors the `results.csv` file for each provider during execution and updates the database incrementally:

- **Polling interval**: Every 2 seconds while each provider subprocess is running
- **During execution**: Status API returns partial `provider_results` for each provider
- **On completion**: Final results include `metrics` data and `leaderboard_summary`

**TTS-specific behavior**: Audio files are uploaded to S3 as they become available during processing:

- Each audio file is uploaded immediately when it appears in `results.csv`
- Presigned URLs are generated and cached for intermediate results (allows immediate playback)
- The `uploaded_audio_cache` dict prevents re-uploading the same file on each polling iteration
- When the job completes, final results store S3 keys (not presigned URLs); the status API generates presigned URLs on-the-fly for done jobs

**Response Model (`ProviderResult`):**

| Field      | Type                   | When Present                                      |
| ---------- | ---------------------- | ------------------------------------------------- |
| `provider` | `str`                  | Always                                            |
| `success`  | `Optional[bool]`       | `None` while processing, `True`/`False` when done |
| `message`  | `str`                  | Always (`"Processing..."` while in progress)      |
| `metrics`  | `Optional[List[Dict]]` | Only when provider completes                      |
| `results`  | `Optional[List[Dict]]` | Partial rows during execution, complete when done |

**TTS `results` row `audio_path` field:**

- **While in progress**: Contains presigned URL for immediate playback
- **When done**: Contains S3 key; status API generates presigned URL on fetch

This allows clients to track progress per-provider and play audio files without waiting for all providers to complete.

### Voice Simulation Incremental Updates

Voice simulations (`pense agent simulation`) run multiple persona-scenario combinations sequentially. Each combination creates a folder named `simulation_persona_<n>_scenario_<m>`. The backend monitors for these folders during execution and updates the database incrementally:

- **Completion marker**: A simulation folder is considered complete when `evaluation_results.csv` exists (created after the LLM judge evaluation step finishes)
- **During execution**: Status API returns partial `simulation_results` for completed simulations, each including:
  - `persona` and `scenario` data from `config.json`
  - `transcript` from `transcript.json`
  - `evaluation_results` with per-criterion metrics (name, value, reasoning) from `evaluation_results.csv`
  - `audio_urls` (presigned S3 URLs for individual audio files in the `audios/` folder)
  - `conversation_wav_url` (presigned S3 URL for the combined `conversation.wav` file, or empty string if not present)
  - Plus `completed_simulations` count for progress tracking
- **On completion**: Final aggregated `metrics` (from `metrics.json`) are added to the response

This allows clients to display progress and per-simulation evaluation results without waiting for all simulations to complete.

### Agent Test Job Results Format

Agent test jobs (both `llm-unit-test` and `llm-benchmark`) store test names in job details and provide partial results while in progress:

- **Job details**: Contains `test_names` array with names of tests being run
- **While queued/in_progress**: `results.test_results` contains `[{"name": "test1"}, {"name": "test2"}, ...]`
- **When done**: `results.test_results` contains full results with `passed`, `output`, and `test_case` fields

**Response Model (`TestCaseResult`):**

| Field       | Type                   | When Present                  |
| ----------- | ---------------------- | ----------------------------- |
| `name`      | `Optional[str]`        | Always (in-progress and done) |
| `passed`    | `Optional[bool]`       | Only when done                |
| `output`    | `Optional[TestOutput]` | Only when done                |
| `test_case` | `Optional[Dict]`       | Only when done                |

This allows clients to display which tests are being run before results are available.

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

# Job Queue
MAX_CONCURRENT_JOBS=2            # Max concurrent jobs per queue type (default: 2)

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
- **Storage Pattern**: Store S3 keys (not presigned URLs) in the database; generate presigned URLs on-the-fly when fetching job status. This prevents expired URLs from being served to clients.

**Helper Functions** (in `utils.py`):

| Function                            | Purpose                                             |
| ----------------------------------- | --------------------------------------------------- |
| `generate_presigned_download_url()` | Generate presigned URL for `get_object` (downloads) |
| `generate_presigned_upload_url()`   | Generate presigned URL for `put_object` (uploads)   |

Both functions return `None` on failure, allowing callers to handle errors (skip, fallback to S3 path, raise error).

**Example Pattern (TTS/STT evaluation results):**

```python
from utils import generate_presigned_download_url

# When storing results - save S3 key only
result_row["audio_path"] = audio_s3_key  # e.g., "tts/evals/{job_id}/outputs/{provider}/audios/0.wav"

# When fetching status - generate presigned URL on-the-fly
if job["status"] == TaskStatus.DONE.value:
    for result in results:
        if result.get("audio_path") and not result["audio_path"].startswith("http"):
            presigned_url = generate_presigned_download_url(result["audio_path"])
            if presigned_url:
                result["audio_path"] = presigned_url
```

**Backwards Compatibility**: When generating presigned URLs, skip entries that already start with `http` or `s3://` (older data with stored URLs).

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
- `nltk` data (`punkt_tab`) - Required by pipecat for sentence tokenization; pre-downloaded in Dockerfile to avoid runtime network issues

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

### Nested Resource Auth Pattern

For resources that don't have direct `user_id` (e.g., `simulation_jobs` linked via `simulation_id`), verify ownership through the parent entity:

```python
@router.get("/run/{task_id}", response_model=JobStatusResponse)
async def get_job_status(task_id: str, user_id: str = Depends(get_current_user_id)):
    job = get_job(task_id)
    if not job:
        raise HTTPException(status_code=404, detail="Task not found")

    # Verify user owns the parent entity
    parent_id = job.get("parent_id")
    if parent_id:
        parent = get_parent(parent_id)
        if not parent or parent.get("user_id") != user_id:
            raise HTTPException(status_code=404, detail="Task not found")  # 404, not 403

    return JobStatusResponse(...)
```

**Security note**: Return 404 (not 403) when access is denied to prevent information leakage about whether a resource exists.

### Background Task Pattern (with Queueing)

```python
# Job types sharing a queue
JOB_TYPES = ["type-a", "type-b"]

# Job starter callback (registered at module load)
def _start_job_from_queue(job: dict) -> bool:
    details = job.get("details", {})
    request = TaskRequest(**details)
    thread = threading.Thread(target=run_task, args=(job["uuid"], request), daemon=True)
    thread.start()
    return True

register_job_starter("type-a", _start_job_from_queue)

# Endpoint with queue check and JWT auth
@router.post("/run", response_model=TaskCreateResponse)
async def start_task(request: TaskRequest, user_id: str = Depends(get_current_user_id)):
    # Check capacity
    can_start = can_start_job(JOB_TYPES)
    initial_status = TaskStatus.IN_PROGRESS.value if can_start else TaskStatus.QUEUED.value

    job_id = create_job(
        job_type="type-a",
        user_id=user_id,
        status=initial_status,
        details={"param": value, "s3_bucket": bucket},
    )

    if can_start:
        thread = threading.Thread(target=run_task, args=(job_id, request), daemon=True)
        thread.start()

    return TaskCreateResponse(task_id=job_id, status=initial_status)

# Task function with queue trigger on completion
def run_task(task_id: str, request: TaskRequest):
    try:
        # ... do work ...
        update_job(task_id, status=TaskStatus.DONE.value, results={...})
    except Exception as e:
        update_job(task_id, status=TaskStatus.DONE.value, results={"error": str(e)})
    finally:
        # Start next queued job if capacity allows
        try_start_queued_job(JOB_TYPES)
```

### Incremental Job Processing Pattern (STT Evaluations, Voice Simulations)

For long-running jobs that produce incremental outputs (like STT evaluations and voice simulations), use non-blocking subprocess execution with polling:

```python
def run_incremental_task(task_id: str, output_dir: Path):
    # Start process without blocking
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    processed_items = set()
    results = []

    # Monitor for new outputs while process runs
    while process.poll() is None:
        for item in output_dir.iterdir():
            if item.name not in processed_items and is_item_complete(item):
                result = parse_item(item)
                results.append(result)
                processed_items.add(item.name)

                # Update DB with partial results
                update_job(task_id, status="in_progress", results={
                    "completed": len(results),
                    "items": results
                })

        time.sleep(2)  # Poll interval

    # Final update with all results
    update_job(task_id, status="done", results={"items": results, "metrics": ...})
```

Key aspects:

- Use `subprocess.Popen` instead of `subprocess.run` for non-blocking execution
- **Important**: Write stdout/stderr to temp files instead of pipes when polling with `process.poll()`. Using pipes with polling can cause deadlocks if the subprocess output exceeds the pipe buffer size:

```python
# Redirect to files to avoid pipe buffer deadlock during polling
stdout_path = output_dir / f"{provider}_stdout.log"
stderr_path = output_dir / f"{provider}_stderr.log"
stdout_f = open(stdout_path, "w")
stderr_f = open(stderr_path, "w")

try:
    process = subprocess.Popen(cmd, stdout=stdout_f, stderr=stderr_f, ...)
    while process.poll() is None:
        # Read intermediate results from output directory
        time.sleep(2)
finally:
    stdout_f.close()
    stderr_f.close()

# Read captured output after process completes
with open(stdout_path) as f:
    stdout = f.read()
```

- Check for completion markers (e.g., `evaluation_results.csv` exists for voice simulations, or just read `results.csv` for STT evaluations)
- Update DB incrementally so status API can return partial results
- Use thread-safe mechanisms (locks) when multiple threads update shared state
- Final aggregated metrics only computed after all items complete

---

## Future Considerations

1. **Scalability**: Consider PostgreSQL + Redis for multi-instance deployment
2. **Task Queue**: Consider Celery/RQ for more robust job management
3. **Caching**: Add Redis caching for frequently accessed data
4. **Rate Limiting**: Add rate limiting for API protection
5. **Pagination**: Implement pagination for list endpoints as data grows
