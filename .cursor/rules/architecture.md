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
- `DELETE /jobs/{job_uuid}` - Delete a job (kills processes, releases ports, triggers next queued job)
- `POST /stt/evaluate` - Start STT evaluation task
- `GET /stt/evaluate/{task_id}` - Get STT evaluation status (includes timeout detection)
- `POST /tts/evaluate` - Start TTS evaluation task
- `GET /tts/evaluate/{task_id}` - Get TTS evaluation status (includes timeout detection)
- `POST /agent-tests/agent/{uuid}/run` - Run agent unit tests
- `POST /agent-tests/agent/{uuid}/benchmark` - Run multi-model benchmark
- `GET /agent-tests/agent/{uuid}/runs` - List all test runs for an agent
- `GET /agent-tests/run/{task_id}` - Get test run status (includes timeout detection)
- `GET /agent-tests/benchmark/{task_id}` - Get benchmark status (includes timeout detection)
- `DELETE /agent-tests/job/{job_uuid}` - Delete an agent test job (triggers next queued job)

#### Simulations

- `POST /simulations/{uuid}/run` - Start simulation (chat or voice)
- `GET /simulations/run/{task_id}` - Get simulation run status (includes timeout detection, partial results for voice)
- `DELETE /simulations/run/{job_uuid}` - Delete a simulation job (kills process, releases port, triggers next queued job)
- `GET /simulations/{uuid}/runs` - List all runs for a simulation

**Status API Response Fields:**

- `total_simulations` - Expected number of simulations (personas × scenarios)
- `completed_simulations` - Number of completed simulations (for in_progress text/voice simulations)
- `simulation_results` - Array of simulation results (partial for in_progress; includes both complete and in-progress simulations)
- `metrics` - Aggregated evaluation metrics (only populated when all simulations complete)

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
5. **Process & Port Tracking**: PIDs and ports stored in job details for cleanup (STT/TTS evals and voice simulations)
6. **Incremental Updates**: Results updated in DB during execution (STT/TTS evals, text/voice simulations, agent tests)
7. **Timeout Detection**: Status API checks if job hasn't updated in 5+ minutes; if so, marks as failed
8. **Completion**: Job updated with `done` status and `results` JSON
9. **Queue Processing**: On completion/timeout/deletion, `try_start_queued_*_job()` starts next queued job if capacity allows
10. **Recovery**: On app startup, `job_recovery.py` kills orphaned processes, restarts `in_progress` jobs, and starts queued jobs

### Job Status Values

- `queued` - Job is waiting for capacity (FIFO order)
- `in_progress` - Job is running (may have partial results for voice simulations)
- `done` - Job completed successfully
- `failed` - Job failed (timed out or error occurred; check `results.error` for details, partial results preserved)
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

STT, TTS, and voice simulation jobs spawn subprocesses that run on specific ports. To handle server restarts and cleanup gracefully:

- **Process isolation**: All subprocesses started with `start_new_session=True` (creates new process group)
- **PID tracking**: Process PIDs stored in job `details`:
  - Voice simulations: Single `pid` and `pgid` fields
  - STT/TTS evaluations: `running_pids` dict mapping provider name to PID (e.g., `{"deepgram": 12345, "openai": 12346}`)
- **Port tracking**: Reserved ports stored in job `details`:
  - Voice simulations: Single `port` field (only for voice type)
  - STT/TTS evaluations: `provider_ports` dict mapping provider name to port (e.g., `{"deepgram": 8765, "openai": 8766}`)
- **Orphan cleanup**: On recovery, `job_recovery.py` kills process groups using `os.killpg()` before restarting
- **Graceful termination**: Sends SIGTERM first, waits briefly, then SIGKILL if still running
- **Port release**: Ports are released back to the pool after process termination

This prevents orphaned processes from accumulating across server restarts and frees up ports.

### Job Deletion

Jobs can be deleted via DELETE endpoints:

| Endpoint                             | Table             | Notes                                               |
| ------------------------------------ | ----------------- | --------------------------------------------------- |
| `DELETE /jobs/{job_uuid}`            | `jobs`            | STT/TTS eval jobs; kills processes, releases ports  |
| `DELETE /agent-tests/job/{job_uuid}` | `agent_test_jobs` | Agent test jobs; no process cleanup (blocking call) |
| `DELETE /simulations/run/{job_uuid}` | `simulation_jobs` | Simulation jobs; kills process, releases port       |

When deleting a running job:

1. Kill running processes (if applicable)
2. Release reserved ports (if applicable)
3. Delete job from database
4. Trigger next queued job in the same queue

### Job Timeout Detection

Jobs that haven't updated their `updated_at` timestamp in 5+ minutes are considered timed out:

- **Timeout threshold**: `JOB_TIMEOUT_MINUTES = 5` (configured in `utils.py`)
- **Detection**: Status API checks `updated_at` for `in_progress` jobs
- **Timeout handling**:
  1. Kill running processes (if applicable)
  2. Release reserved ports (if applicable)
  3. Mark job as `failed` with error (existing partial results are preserved)
  4. Trigger next queued job

The `is_job_timed_out(updated_at)` utility function in `utils.py` handles timestamp comparison.

**Important**: SQLite stores timestamps in UTC via `CURRENT_TIMESTAMP`. The timeout function uses `datetime.utcnow()` to match. Using `datetime.now()` would cause timezone mismatches and incorrect timeout detection (e.g., jobs marked as timed out immediately after creation if server is ahead of UTC).

| Utility Function             | Location   | Purpose                              |
| ---------------------------- | ---------- | ------------------------------------ |
| `is_job_timed_out()`         | `utils.py` | Checks if job has exceeded timeout   |
| `kill_process_group()`       | `utils.py` | Kills a single process group by PID  |
| `kill_processes_from_dict()` | `utils.py` | Kills multiple processes from a dict |

### STT/TTS Evaluation Incremental Updates

STT and TTS evaluations run multiple providers in parallel, with each provider processing multiple items sequentially. The backend monitors both `results.csv` and `metrics.json` for each provider during execution and updates the database incrementally:

- **Polling interval**: Every 2 seconds while each provider subprocess is running
- **During execution**: Status API returns partial `provider_results` for each provider
- **Provider completion detection**: When `metrics.json` exists for a provider, that provider is marked as complete (`success: true`, `metrics` populated) even while other providers are still running
- **Job completion**: Final results include `leaderboard_summary` after all providers complete

**TTS-specific behavior**: Audio files are uploaded to S3 as they become available during processing:

- Each audio file is uploaded immediately when it appears in `results.csv`
- Presigned URLs are generated and cached for intermediate results (allows immediate playback)
- The `uploaded_audio_cache` dict prevents re-uploading the same file on each polling iteration
- When the job completes, final results store S3 keys (not presigned URLs); the status API generates presigned URLs on-the-fly for done jobs

**Response Model (`ProviderResult`):**

| Field      | Type                   | When Present                                                          |
| ---------- | ---------------------- | --------------------------------------------------------------------- |
| `provider` | `str`                  | Always                                                                |
| `success`  | `Optional[bool]`       | `None` while processing, `True` when `metrics.json` exists            |
| `message`  | `str`                  | `"Processing..."` while in progress, `"Completed"` when metrics exist |
| `metrics`  | `Optional[List[Dict]]` | When `metrics.json` exists (provider complete)                        |
| `results`  | `Optional[List[Dict]]` | Partial rows during execution, complete when done                     |

**TTS `results` row `audio_path` field:**

- **While in progress**: Contains presigned URL for immediate playback
- **When done**: Contains S3 key; status API generates presigned URL on fetch

This allows clients to track progress per-provider, see which providers have completed (with their metrics), and play audio files without waiting for all providers to complete.

### Simulation Incremental Updates (Text and Voice)

Both text simulations (`pense llm simulations run`) and voice simulations (`pense agent simulation`) run multiple persona-scenario combinations. Each combination creates a folder named `simulation_persona_<n>_scenario_<m>`. The backend monitors for these folders during execution and updates the database incrementally:

- **Polling interval**: Every 2 seconds while the subprocess is running
- **Completion marker**: A simulation folder is considered complete when `evaluation_results.csv` exists (created after the LLM judge evaluation step finishes)
- **In-progress detection**: A simulation is considered in-progress when `transcript.json` or `config.json` exists but `evaluation_results.csv` does not
- **Persona/scenario data resolution** (both text and voice):
  1. First try reading from `config.json` in the simulation directory
  2. Fallback: parse directory name `simulation_persona_N_scenario_M` to get 1-based indices, then look up `personas_list[N-1]` and `scenarios_list[M-1]` from the original pense config
- **During execution**: Status API returns partial `simulation_results` including:
  - **For completed simulations**:
    - `persona` and `scenario` data (always populated via config.json or fallback)
    - `transcript` from `transcript.json`
    - `evaluation_results` with per-criterion metrics (name, value, reasoning) from `evaluation_results.csv`
  - **For in-progress simulations** (both text and voice):
    - `persona` and `scenario` data (always populated via config.json or fallback)
    - `transcript` from `transcript.json` (partial conversation so far)
    - `evaluation_results` is `null`
    - **Voice simulations**: No audio URLs returned until evaluation completes (all audio fields are `null`)
  - Plus `completed_simulations` count for progress tracking (counts only fully completed simulations)
  - **`metrics` field is `null` during in-progress**: The `pense` CLI creates `metrics.json` incrementally as each simulation completes, so reading it before all simulations finish would give incomplete aggregate data (e.g., `values` array with fewer entries than expected). The backend intentionally does NOT read `metrics.json` until the job completes.
- **On completion**: Final aggregated `metrics` (from `metrics.json`) are added to the response only after the subprocess exits
- **Presigned URL handling** (voice simulations):
  - **During in-progress**: No audio URLs are returned for simulations until their `evaluation_results` are available. This means:
    - In-progress simulations (no `evaluation_results.csv` yet) have all audio fields as `null`
    - Completed simulations (have `evaluation_results.csv`) include `audio_urls` and `conversation_wav_url` presigned URLs stored in DB
  - **When job status becomes done**: Presigned URLs are stripped from the stored results; only S3 paths (`audios_s3_path`, `conversation_wav_s3_key`) remain
  - **When fetching done status**: Presigned URLs are generated on-the-fly from S3 paths using `_get_audio_urls_from_s3_key()` and `generate_presigned_download_url()`, but NOT saved back to DB
  - This ensures audio is only accessible after evaluation completes and prevents stale URLs from accumulating

This allows clients to display progress and per-simulation results (including partial transcripts for in-progress simulations) without waiting for all simulations to complete.

### Agent Test Job Results Format

Agent test jobs (`llm-unit-test`) monitor the output directory during execution and provide incremental results as each test completes:

- **Polling interval**: Every 2 seconds while the subprocess is running
- **During execution**: The backend reads `results.json` and updates the database incrementally
- **Test completion detection**: When a test appears in `results.json`, it's shown with full results; pending tests show just the name
- **Job completion**: When `metrics.json` exists, `total_tests`, `passed`, and `failed` are populated

**Response Model (`TestCaseResult`):**

| Field       | Type                   | When Present                                    |
| ----------- | ---------------------- | ----------------------------------------------- |
| `name`      | `Optional[str]`        | Always (pending and completed tests)            |
| `passed`    | `Optional[bool]`       | When test completes (appears in `results.json`) |
| `output`    | `Optional[TestOutput]` | When test completes                             |
| `test_case` | `Optional[Dict]`       | When test completes                             |

This allows clients to see which tests have completed with their results while other tests are still running.

### Benchmark Job Results Format

Benchmark jobs (`llm-benchmark`) run multiple models in parallel, with per-test intermediate results for each model:

- **Parallel execution**: All models run simultaneously using `ThreadPoolExecutor`
- **Polling interval**: Every 2 seconds while any model is still running
- **Thread-safe updates**: Uses `threading.Lock` when updating shared results to prevent race conditions
- **Output discovery**: Uses `os.walk()` on the output directory to find all `results.json` and `metrics.json` files, then matches them to models by folder name
- **Model name matching**: `_match_model_to_folder()` handles various pense naming conventions:
  - `openai/gpt-4` → `openai_gpt-4` (single underscore)
  - `openai/gpt-4` → `openai__gpt-4` (double underscore)
  - `openai/gpt-4` → `openai-gpt-4` (dash)
- **Model states**:
  - **Queued**: No output folder found yet (`success: None`, `message: "Queued..."`)
  - **In-progress**: Has `results.json` but no `metrics.json` (`success: None`, `message: "Processing... (X/Y tests)"`)
  - **Completed**: Has `metrics.json` (`success: True/False`, full metrics)
- **Job completion**: Final `leaderboard_summary` added after all models complete

**Response Model (`ModelResult`):**

| Field          | Type                   | When Present                                        |
| -------------- | ---------------------- | --------------------------------------------------- |
| `model`        | `str`                  | Always                                              |
| `success`      | `Optional[bool]`       | `None` while queued/processing, `True/False` done   |
| `message`      | `str`                  | Status message (queued/processing/completed)        |
| `total_tests`  | `Optional[int]`        | When model completes (from `metrics.json`)          |
| `passed`       | `Optional[int]`        | When model completes                                |
| `failed`       | `Optional[int]`        | When model completes                                |
| `test_results` | `Optional[List[Dict]]` | During execution (partial) and on completion (full) |

Intermediate results always include ALL tests in `test_results`:

- **Completed tests**: Full data (`name`, `passed`, `output`, `test_case`)
- **Pending tests**: `{name, passed: null, output: null, test_case: null}`

This ensures consistent array length and allows clients to show progress for each test.

**S3 Upload Structure for Benchmarks:**

Benchmarks use `skip_s3_upload=True` for individual model runs, then upload once at the end to preserve the pense CLI output structure:

```
s3://bucket/agent-tests/benchmarks/{task_id}/
  outputs/
    test_config/
      anthropic__claude-opus-4.5/
        results.json
        metrics.json
      openai__gpt-5.1/
        results.json
        metrics.json
  leaderboard/
    llm_leaderboard.csv
```

This avoids duplicate uploads (each model uploading everyone's files) and matches the local pense output structure.

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

**Build gotcha**: The Dockerfile uses two separate package installation methods:

- `pip install` for the pense wheel (includes pipecat, nltk, etc.)
- `uv sync` for project dependencies (fastapi, boto3, etc.)

When running Python commands during build that need packages from the wheel, use `python` directly (not `uv run python`).

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
        update_job(task_id, status=TaskStatus.FAILED.value, results={"error": str(e)})
    finally:
        # Start next queued job if capacity allows
        try_start_queued_job(JOB_TYPES)
```

### Incremental Job Processing Pattern (STT/TTS Evaluations, Simulations, Agent Tests)

For long-running jobs that produce incremental outputs (STT/TTS evaluations, text/voice simulations, and agent tests), use non-blocking subprocess execution with polling:

```python
def run_incremental_task(task_id: str, output_dir: Path):
    # Start process without blocking
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    processed_items = set()
    results = []
    prev_state = None  # Track state to avoid unnecessary DB updates

    # Monitor for new outputs while process runs
    while process.poll() is None:
        transcript_lengths = []  # For change detection

        for item in output_dir.iterdir():
            if item.name not in processed_items and is_item_complete(item):
                result = parse_item(item)
                results.append(result)
                processed_items.add(item.name)
            # Track in-progress items too (e.g., transcript length)
            transcript = get_transcript(item)
            transcript_lengths.append((item.name, len(transcript)))

        # Build state tuple for comparison
        current_state = (len(results), tuple(sorted(transcript_lengths)))

        # Only update DB if state changed
        if current_state != prev_state:
            update_job(task_id, status="in_progress", results={
                "completed": len(results),
                "items": results
            })
            prev_state = current_state

        time.sleep(2)  # Poll interval

    # Final update with all results
    update_job(task_id, status="done", results={"items": results, "metrics": ...})
```

**Critical**: Track previous state and only update DB when state changes. This:

- Avoids unnecessary DB writes every 2-second poll when nothing changed
- Preserves `updated_at` timestamp for the 5-minute timeout check (without this, every poll would reset `updated_at`, making stuck job detection unreliable)

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

- Check for completion markers (e.g., `evaluation_results.csv` for simulations, `results.csv` for STT/TTS evaluations, `results.json` for agent tests)
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
