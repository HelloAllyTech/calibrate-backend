---
description: "Calibrate Backend Architecture - AI Agent Testing & Evaluation Platform"
alwaysApply: true
---

# Calibrate Backend Architecture

## Project Overview

**Calibrate Backend** is a FastAPI-based REST API that serves as the backend for an AI agent testing and evaluation platform. It provides capabilities for:

1. **Speech-to-Text (STT) Evaluation** - Benchmark multiple STT providers against ground truth transcriptions
2. **Text-to-Speech (TTS) Evaluation** - Benchmark multiple TTS providers for quality metrics
3. **LLM Agent Testing** - Run unit tests and benchmarks on LLM-based agents
4. **Voice/Chat Simulations** - Run simulated conversations between AI agents and personas across various scenarios

The backend wraps the `calibrate` CLI tool and orchestrates evaluation jobs while providing a RESTful API interface.

---

## Technology Stack

| Component            | Technology         | Purpose                                                      |
| -------------------- | ------------------ | ------------------------------------------------------------ |
| **Framework**        | FastAPI            | Async REST API framework                                     |
| **Database**         | SQLite             | Persistent data storage                                      |
| **Storage**          | AWS S3             | File/result storage                                          |
| **Authentication**   | Google OAuth + JWT | User authentication via Google ID tokens, API access via JWT |
| **Monitoring**       | Sentry             | Error tracking and performance monitoring                    |
| **Tracing**          | Langfuse (via OTEL) | LLM observability and tracing                                |
| **Package Manager**  | uv                 | Python dependency management                                 |
| **Containerization** | Docker             | Deployment                                                   |
| **CLI Tool**         | calibrate          | Core evaluation/simulation engine                            |

---

## Project Structure

```
calibrate-backend/
├── src/
│   ├── main.py              # FastAPI app entry point, lifespan management
│   ├── db.py                # SQLite database layer (~2300 lines)
│   ├── utils.py             # Shared utilities (S3 client, tool config building)
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
│   └── calibrate.db         # SQLite database file
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
- `DELETE /jobs/{job_uuid}` - Delete a job (kills processes, triggers next queued job)
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
- `POST /simulations/run/{job_uuid}/abort` - Abort a running simulation (kills process, saves partial results with abort marker, triggers next queued job); returns full `SimulationRunStatusResponse`
- `DELETE /simulations/run/{job_uuid}` - Delete a simulation job (kills process, triggers next queued job)
- `GET /simulations/{uuid}/runs` - List all runs for a simulation

**Status API Response Fields:**

- `total_simulations` - Expected number of simulations (personas × scenarios)
- `completed_simulations` - Number of completed simulations (for in_progress text/voice simulations)
- `simulation_results` - Array of simulation results (partial for in_progress; includes both complete and in-progress simulations). Each result includes an `aborted` field (`true`/`false`/`null`) set by the abort endpoint for incomplete/complete simulations respectively; `null` for non-aborted runs.
- `metrics` - Aggregated evaluation metrics (only populated when all simulations complete)

#### Utilities

- `GET /` or `HEAD /` - Health check (HEAD supported for uptime monitors)
- `POST /presigned-url` - Generate S3 presigned URL for uploads
- `GET` or `HEAD /provider-status` - Check status of all configured providers (runs `calibrate status` CLI, non-blocking via `asyncio.create_subprocess_exec`). Returns `{"success": true}` if all pass, 503 with failed provider details if any fail. HEAD supported for uptime monitors.

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
5. **Process Tracking**: PIDs stored in job details for cleanup (voice simulations, STT/TTS evals only; agent tests/benchmarks use blocking `process.wait()` without PID tracking)
6. **Incremental Updates**: Results updated in DB during execution (text/voice simulations, agent unit tests, benchmarks)
7. **Timeout Detection**: Status API checks if job hasn't updated in 5+ minutes; if so, marks as failed
8. **Completion**: Job updated with `done` status and `results` JSON
9. **Queue Processing**: On completion/timeout/deletion, `try_start_queued_*_job()` starts next queued job if capacity allows
10. **Recovery**: On app startup, `job_recovery.py` kills orphaned processes, restarts `in_progress` jobs, and starts queued jobs

### Job Status Values

- `queued` - Job is waiting for capacity (FIFO order)
- `in_progress` - Job is running (may have partial results for simulations, agent unit tests, and benchmarks)
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

Voice simulation and STT/TTS evaluation jobs spawn subprocesses. To handle server restarts and cleanup gracefully:

- **Process isolation**: All subprocesses started with `start_new_session=True` (creates new process group)
- **PID tracking**: Process PIDs stored in job `details` as `pid` and `pgid` fields (STT/TTS evals and voice simulations only)
- **Orphan cleanup**: On recovery, `job_recovery.py` kills process groups using `os.killpg()` before restarting
- **Graceful termination**: Sends SIGTERM first, waits 0.5s, then SIGKILL if still running. The SIGKILL step catches both `ProcessLookupError` and `PermissionError` (on macOS, dead/zombie processes can raise `PermissionError` instead of `ProcessLookupError`)

**Note**: Agent test jobs (`llm-unit-test`, `llm-benchmark`) do NOT have PID tracking because the `agent_test_jobs` table doesn't have a `details` column. These jobs use blocking `process.wait()` calls, so the process completes before the function returns.

This prevents orphaned processes from accumulating across server restarts.

### Job Deletion

Jobs can be deleted via DELETE endpoints:

| Endpoint                             | Table             | Notes                                          |
| ------------------------------------ | ----------------- | ---------------------------------------------- |
| `DELETE /jobs/{job_uuid}`            | `jobs`            | STT/TTS eval jobs; kills processes             |
| `DELETE /agent-tests/job/{job_uuid}` | `agent_test_jobs` | Agent test jobs; no process cleanup (blocking) |
| `DELETE /simulations/run/{job_uuid}` | `simulation_jobs` | Simulation jobs; kills process                 |

When deleting a running job:

1. Kill running processes (if applicable)
2. Delete job from database
3. Trigger next queued job in the same queue

### Job Abort (Simulations Only)

Simulation jobs can be aborted via `POST /simulations/run/{job_uuid}/abort`. Unlike deletion (which removes the job from the DB), abort preserves results:

1. Kill running process
2. Read current intermediate results from DB (already stored by the polling loop)
3. Add `"aborted": true` to each incomplete simulation result (where `evaluation_results` is `None`), and `"aborted": false` to completed ones. Transcripts are left untouched.
4. Save with `status=done` and `aborted: true` in job details

**Race condition handling**: The `_is_job_aborted(task_id)` helper checks for the `aborted` flag in job details. It is used in all places where the background thread would otherwise overwrite abort results:

- `_run_calibrate_text_simulation` and `_run_calibrate_voice_simulation` call it after the polling loop exits. If set, they return early without final processing.
- `run_simulation_task` calls it before the final `update_simulation_job` call and in all exception handlers. If set, it returns early (the `finally` block still triggers the queue).

### Job Timeout Detection

Jobs that haven't updated their `updated_at` timestamp in 5+ minutes are considered timed out:

- **Timeout threshold**: `JOB_TIMEOUT_MINUTES = 5` (configured in `utils.py`)
- **Detection**: Status API checks `updated_at` for `in_progress` jobs
- **Timeout handling**:
  1. Kill running processes (if applicable)
  2. Mark job as `failed` with error (existing partial results are preserved)
  3. Trigger next queued job

The `is_job_timed_out(updated_at)` utility function in `utils.py` handles timestamp comparison.

**Important**: SQLite stores timestamps in UTC via `CURRENT_TIMESTAMP`. The timeout function uses `datetime.utcnow()` to match. Using `datetime.now()` would cause timezone mismatches and incorrect timeout detection (e.g., jobs marked as timed out immediately after creation if server is ahead of UTC).

| Utility Function                | Location   | Purpose                                                                                      |
| ------------------------------- | ---------- | -------------------------------------------------------------------------------------------- |
| `is_job_timed_out()`            | `utils.py` | Checks if job has exceeded timeout                                                           |
| `kill_process_group()`          | `utils.py` | Kills a single process group by PID                                                          |
| `kill_processes_from_dict()`    | `utils.py` | Kills multiple processes from a dict                                                         |
| `capture_exception_to_sentry()` | `utils.py` | Logs exception to Sentry as unhandled error                                                  |
| `build_tool_configs()`          | `utils.py` | Builds calibrate tool configs from agent tools (handles structured_output and webhook types) |

### STT/TTS Evaluation Flow

STT and TTS evaluations run a single `calibrate stt` or `calibrate tts` command with all providers specified at once. The calibrate CLI handles parallelization internally and generates the leaderboard automatically as part of the same command.

- **Single command execution**: All providers evaluated in one CLI call (e.g., `calibrate stt -p openai deepgram sarvam -l english -i input -o output`)
- **Internal parallelization**: The calibrate CLI handles concurrent provider execution internally
- **Automatic leaderboard**: The CLI generates the leaderboard in `output/leaderboard/` as part of the same command
- **Job completion**: After the command completes, the backend reads per-provider results and the leaderboard, then uploads to S3
- **Leaderboard reading**: The backend finds any `.xlsx` file in the leaderboard directory (dynamic discovery) and reads the `summary` sheet

**Intermediate updates via on-demand disk reads**: The background task stores the `output_dir` path in job details. When the status API is called for an `in_progress` job, it reads each provider's `results.csv` (and `metrics.json` if available) directly from disk. This provides per-file progress as the CLI writes rows to `results.csv` incrementally. Unlike simulations which poll and update the DB, STT/TTS reads are on-demand from the status API — no polling loop in the background task.

**Response Model (`TaskStatusResponse`):**

| Field                 | Type                             | Description                                                           |
| --------------------- | -------------------------------- | --------------------------------------------------------------------- |
| `task_id`             | `str`                            | Job UUID                                                              |
| `status`              | `str`                            | Job status: `queued`, `in_progress`, `done`, `failed`                 |
| `language`            | `Optional[str]`                  | Language from job details (e.g., "english", "hindi")                  |
| `provider_results`    | `Optional[List[ProviderResult]]` | Results per provider (partial during in_progress, full on completion) |
| `leaderboard_summary` | `Optional[List[Dict]]`           | Summary after job completes                                           |
| `error`               | `Optional[str]`                  | Error message if job failed                                           |

**Response Model (`ProviderResult`):**

| Field      | Type                           | When Present                                                                                        |
| ---------- | ------------------------------ | --------------------------------------------------------------------------------------------------- |
| `provider` | `str`                          | Always                                                                                              |
| `success`  | `Optional[bool]`               | `None` while job is running, `True`/`False` when complete                                           |
| `message`  | `str`                          | `"Queued..."` → `"Running... (N files/texts processed)"` → `"Completed"` or error message when done |
| `metrics`  | `Optional[Dict \| List[Dict]]` | Available when provider's `metrics.json` exists (during or after execution)                         |
| `results`  | `Optional[List[Dict]]`         | Partial rows from `results.csv` while running, complete when done                                   |

**TTS success determination**: A TTS provider is marked `success: true` only if at least one text was successfully synthesized (has an `audio_path` in results). If the calibrate CLI completes but no audio files were generated (e.g., voice not found, API errors), `success: false` is returned with an error message. This prevents false positives where the process exits normally but all synthesis attempts failed.

**Provider status while running**: While the job is `in_progress`, the status API reads intermediate results from disk:

- **Provider with partial results**: `success: null`, `message: "Running... (N files processed)"` (STT) or `"Running... (N texts processed)"` (TTS), `results` populated from `results.csv`, `metrics` from `metrics.json` if available
- **Provider with no output yet**: `success: null`, `message: "Queued..."`, `metrics`/`results` as `null`
- **TTS audio paths during in-progress**: Local audio files are uploaded to S3 on-the-fly (using the same key convention as the final upload: `tts/evals/{task_id}/outputs/{provider}/...`) and presigned download URLs are returned. Falls back to `null` if the file doesn't exist or upload fails. The uploads are idempotent — the final background task upload overwrites the same keys.
- **Fallback**: If `output_dir` is not in job details (e.g., process hasn't started yet), all providers shown as `"Queued..."`

**Metrics normalization**: The status API normalizes old list-of-dicts metrics format to the new dict format before returning. The `_normalize_metrics()` helper handles two old formats:

- Simple metrics: `[{"wer": 2.4}, {"string_similarity": 0.15}, ...]` → merged into result dict
- Latency metrics: `[{"metric_name": "ttfb", "mean": 0.1, ...}, ...]` → uses `metric_name` as key, rest as value

This ensures clients always receive metrics in dict format (e.g., `{"wer": 2.4, "ttfb": {"mean": 0.1, ...}}`) regardless of when the job was created.

**STT Metrics** (returned by `calibrate stt`):

- `wer` - Word Error Rate (float)
- `string_similarity` - String similarity score (float)
- `llm_judge_score` - LLM judge accuracy score (float)
- `processing_time` - Processing time stats: `{mean, std, values}` (may be absent/null)
- `ttfb` - Time to First Byte stats: `{mean, std, values}` (may be absent/null)

**TTS Metrics** (returned by `calibrate tts`):

- `llm_judge_score` - LLM judge accuracy score (float)
- `ttfb` - Time to First Byte stats: `{mean, std, values}` (may be absent/null)

Note: TTS does not return `processing_time`. Both `ttfb` values may be absent if the provider doesn't support streaming or the measurement failed - clients should handle null/missing values.

**TTS `results` row `audio_path` field:**

- **Stored**: S3 key (e.g., `tts/evals/{job_id}/outputs/{provider}/audios/0.wav`)
- **On status fetch**: Presigned URL generated on-the-fly for completed jobs

This allows secure, time-limited access to audio files without storing expirable URLs in the database.

### Simulation Incremental Updates (Text and Voice)

Both text simulations (`calibrate llm simulations run`) and voice simulations (`calibrate agent simulation`) run multiple persona-scenario combinations. Each combination creates a folder named `simulation_persona_<n>_scenario_<m>`. The backend monitors for these folders during execution and updates the database incrementally:

- **Polling interval**: Every 2 seconds while the subprocess is running
- **Completion marker**: A simulation folder is considered complete when `evaluation_results.csv` exists (created after the LLM judge evaluation step finishes)
- **In-progress detection**: A simulation is considered in-progress when `transcript.json` or `config.json` exists but `evaluation_results.csv` does not
- **Persona/scenario data resolution** (both text and voice):
  1. First try reading from `config.json` in the simulation directory
  2. Fallback: parse directory name `simulation_persona_N_scenario_M` to get 1-based indices, then look up `personas_list[N-1]` and `scenarios_list[M-1]` from the original calibrate config
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
  - **`metrics` field is `null` during in-progress**: The `calibrate` CLI creates `metrics.json` incrementally as each simulation completes, so reading it before all simulations finish would give incomplete aggregate data (e.g., `values` array with fewer entries than expected). The backend intentionally does NOT read `metrics.json` until the job completes.
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

Agent test jobs (`llm-unit-test`) run a single `calibrate llm` command with the model specified. The backend monitors the output directory during execution and provides incremental results as each test completes.

- **Single command execution**: `calibrate llm -c config.json -m model -p provider -o output`
- **Polling interval**: Every 2 seconds while the subprocess is running
- **During execution**: The backend reads `results.json` and updates the database incrementally
- **Test completion detection**: When a test appears in `results.json`, it's shown with full results; pending tests show just the name

**Response Model (`TestCaseResult`):**

| Field       | Type                   | When Present                                    |
| ----------- | ---------------------- | ----------------------------------------------- |
| `name`      | `Optional[str]`        | Always (pending and completed tests)            |
| `passed`    | `Optional[bool]`       | When test completes (appears in `results.json`) |
| `output`    | `Optional[TestOutput]` | When test completes                             |
| `test_case` | `Optional[Dict]`       | When test completes                             |

This allows clients to see which tests have completed with their results while other tests are still running.

### Benchmark Job Results Format

Benchmark jobs (`llm-benchmark`) run a single `calibrate llm` command with all models specified at once. The calibrate CLI handles parallelization internally and generates the leaderboard automatically.

- **Single command execution**: All models evaluated in one CLI call (e.g., `calibrate llm -c config.json -m gpt-4.1 claude-3.5-sonnet -p openrouter -o output`)
- **Internal parallelization**: The calibrate CLI handles concurrent model execution internally
- **Automatic leaderboard**: The CLI generates the leaderboard in `output/leaderboard/` as part of the same command
- **Output discovery**: After command completes, uses `os.walk()` on the output directory to find all `results.json` and `metrics.json` files
- **Model name matching**: `_match_model_to_folder()` handles various calibrate naming conventions:
  - `openai/gpt-4` → `openai_gpt-4` (single underscore)
  - `openai/gpt-4` → `openai__gpt-4` (double underscore)
  - `openai/gpt-4` → `openai-gpt-4` (dash)
- **Polling interval**: Every 2 seconds while the subprocess is running
- **During execution**: The backend scans output directory for per-model results and updates incrementally
- **Job completion**: Final results and `leaderboard_summary` populated after command completes

**Response Model (`ModelResult`):**

| Field          | Type                   | When Present                                                |
| -------------- | ---------------------- | ----------------------------------------------------------- |
| `model`        | `str`                  | Always                                                      |
| `success`      | `Optional[bool]`       | `None` while running/queued, `True/False` when done         |
| `message`      | `str`                  | `"Queued..."`, `"Running... (N tests done)"`, `"Completed"` |
| `total_tests`  | `Optional[int]`        | When model has results (partial or complete)                |
| `passed`       | `Optional[int]`        | When model has results                                      |
| `failed`       | `Optional[int]`        | When model has results                                      |
| `test_results` | `Optional[List[Dict]]` | When model has results (partial or complete)                |

**Model states during execution:**

- `"Queued..."` - No output folder found for this model yet
- `"Running... (N tests done)"` - Has `results.json` but no `metrics.json` (partial results)
- `"Completed"` - Has `metrics.json` (all tests done for this model)

This allows clients to see per-model progress as each model completes its tests.

**S3 Upload Structure for Benchmarks:**

After the benchmark command completes, outputs are uploaded to S3 preserving the calibrate CLI output structure:

```
s3://bucket/agent-tests/benchmarks/{task_id}/
  benchmark_config.json        # Config file with test config + list of models
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

This avoids duplicate uploads (each model uploading everyone's files) and matches the local calibrate output structure.

### Config File Uploads to S3

All job types upload their config files to S3 for reproducibility and debugging. The config file is uploaded after job completion to the job's output directory:

| Job Type         | Config File Name         | S3 Path                                                  |
| ---------------- | ------------------------ | -------------------------------------------------------- |
| STT Evaluation   | `config.json`            | `stt/evals/{task_id}/config.json`                        |
| TTS Evaluation   | `config.json`            | `tts/evals/{task_id}/config.json`                        |
| Agent Test       | `test_config.json`       | `{s3_prefix}/test_config.json`                           |
| Benchmark        | `benchmark_config.json`  | `agent-tests/benchmarks/{task_id}/benchmark_config.json` |
| Text Simulation  | `simulation_config.json` | `{s3_prefix}/simulation_config.json`                     |
| Voice Simulation | `simulation_config.json` | `{s3_prefix}/simulation_config.json`                     |

**Config file contents:**

- **STT/TTS**: Contains `providers`, `language`, and `audio_count`/`text_count`
- **Agent tests**: Contains the full calibrate config (system prompt, tools, test cases, etc.)
- **Benchmarks**: Contains the calibrate config plus the `models` list being benchmarked
- **Simulations**: Contains the full calibrate simulation config (personas, scenarios, metrics, tools, etc.)

---

## External Integrations

### Calibrate CLI

The backend orchestrates the `calibrate` CLI tool for actual evaluations:

```bash
# STT Evaluation (multiple providers, leaderboard generated automatically)
calibrate stt -p <provider1> <provider2> ... -l <language> -i <input_dir> -o <output_dir>

# TTS Evaluation (multiple providers, leaderboard generated automatically)
calibrate tts -p <provider1> <provider2> ... -l <language> -i <input_csv> -o <output_dir>

# LLM Tests / Benchmark (single or multiple models)
# Single model = unit test, multiple models = benchmark with leaderboard
calibrate llm -c <config.json> -m <model1> [model2 ...] -p <provider> -o <output_dir>

# LLM Simulations (text, runs 4 simulations in parallel)
calibrate llm simulations run -c <config.json> -o <output_dir> -m <model> -n 4

# Voice Agent Simulation (runs 4 simulations in parallel)
calibrate agent simulation -c <config.json> -o <output_dir> -n 4
```

**STT/TTS CLI Notes:**

- Multiple providers can be specified with `-p` (space-separated)
- The CLI handles parallelization internally
- Leaderboard is generated automatically in `<output_dir>/leaderboard/`
- No separate `eval` and `leaderboard` commands needed

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
DB_ROOT_DIR=/appdata/db          # Directory containing calibrate.db

# Job Queue
MAX_CONCURRENT_JOBS=2            # Max concurrent jobs per queue type (default: 2)

# CORS
CORS_ALLOWED_ORIGINS=*           # Comma-separated origins (e.g., "http://localhost:3000,https://app.example.com")

# JWT Authentication
JWT_SECRET_KEY=your-secret-key   # REQUIRED: At least 32 characters, change in production!
JWT_EXPIRATION_HOURS=168         # Token validity (default: 7 days)

# Sentry (Error Tracking)
SENTRY_DSN=                      # Sentry DSN (leave empty to disable)
SENTRY_ENVIRONMENT=development   # Environment name (development, staging, production)
SENTRY_TRACES_SAMPLE_RATE=1.0    # Performance monitoring sample rate (0.0-1.0)
SENTRY_PROFILES_SAMPLE_RATE=1.0  # Profiling sample rate (0.0-1.0)

# Langfuse Tracing (LLM Observability via OpenTelemetry)
ENVIRONMENT=development                              # Langfuse tracing environment
ENABLE_TRACING=true                                  # Enable/disable tracing
OTEL_EXPORTER_OTLP_ENDPOINT=https://host/api/public/otel  # OTLP endpoint (Langfuse self-hosted or cloud)
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic%20xxx      # Base64-encoded Langfuse public:secret key
LANGFUSE_TRACING_ENVIRONMENT=                        # Langfuse tracing environment label
LANGFUSE_HOST=                                       # Langfuse host URL
LANGFUSE_PUBLIC_KEY=                                 # Langfuse public key
LANGFUSE_SECRET_KEY=                                 # Langfuse secret key

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
  "settings": {
    "agent_speaks_first": true,
    "max_assistant_turns": 20
  },
  "system_tools": {
    "end_call": true
  },
  "data_extraction_fields": []
}
```

**Simulation Config Generation**: When running simulations, `_build_calibrate_simulation_config()` builds the calibrate config from agent/personas/scenarios/metrics. Key fields always included:

- `tools` - Built from linked agent tools (see Tool Configuration Schema below)
- `evaluation_criteria` - Built from linked metrics (name + description)
- `settings.agent_speaks_first` - Defaults to `true` if not specified in agent config
- `settings.max_turns` - Mapped from agent config's `settings.max_assistant_turns` (defaults to `50` if not set)

### Tool Configuration Schema (in Calibrate Config)

Tools in calibrate configs (used by both simulations and agent tests) support two types: **structured_output** (default) and **webhook**. The `build_tool_configs()` utility function in `utils.py` handles building these configs from database tool records.

**Structured Output Tool** (for tools that return structured data to the LLM):

```json
{
  "type": "structured_output",
  "name": "get_user_info",
  "description": "Retrieves user information",
  "parameters": [
    {
      "id": "user_id",
      "type": "string",
      "description": "The user ID",
      "required": true
    }
  ]
}
```

**Webhook Tool** (for tools that call external HTTP endpoints):

```json
{
  "type": "webhook",
  "name": "get_presigned_url",
  "description": "Always call this tool after receiving a user response",
  "parameters": [],
  "webhook": {
    "method": "POST",
    "url": "http://localhost:8000/presigned-url",
    "timeout": 20,
    "headers": [{ "name": "Authorization", "value": "Bearer X" }],
    "queryParameters": [
      {
        "id": "key",
        "type": "string",
        "description": "Query param description",
        "required": true
      }
    ],
    "body": {
      "description": "Request body description",
      "parameters": [
        {
          "id": "task_type",
          "type": "string",
          "description": "Type of task",
          "required": true
        }
      ]
    }
  }
}
```

The tool type is determined by the `type` field in the tool's `config` column in the database. If not specified, defaults to `"structured_output"`.

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

- **Rationale**: Long-running subprocess calls to `calibrate` CLI
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

- `fastapi>=0.115.6,<0.122.0` - Web framework (upper bound required for compatibility with calibrate-agent's pipecat-ai dependency)
- `uvicorn>=0.40.0` - ASGI server
- `boto3>=1.34.0` - AWS SDK for S3
- `pydantic>=2.0.0` - Data validation
- `python-dotenv>=1.0.0` - Environment variable loading
- `openpyxl>=3.1.5` - Excel file parsing for leaderboards
- `httpx>=0.27.0` - Async HTTP client for Google OAuth
- `python-jose[cryptography]>=3.3.0` - JWT token encoding/decoding
- `sentry-sdk[fastapi]>=2.0.0` - Error tracking and performance monitoring

External:

- `calibrate-agent` - Core CLI tool for evaluations and simulations (installed from PyPI via `pip install calibrate-agent`)
- `ffmpeg` - Required by calibrate CLI for audio processing (TTS, voice simulations)
- `nltk` data (`punkt_tab`) - Required by pipecat for sentence tokenization; pre-downloaded in Dockerfile to avoid runtime network issues

---

## Deployment

### Docker Build

```bash
docker build -t calibrate-backend .
```

**Package installation**: The Dockerfile installs packages via `uv sync` which reads from `pyproject.toml` and `uv.lock`. The `calibrate-agent` package (and its dependencies like pipecat, nltk, etc.) is listed as a dependency in `pyproject.toml` and installed automatically.

**Important**: `uv sync` installs packages into a `.venv` virtual environment, not system Python. Any `RUN` commands in the Dockerfile that need installed packages must use `uv run python ...` (not bare `python`), otherwise the system Python won't find them.

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
        # ... do work (e.g., run CLI command) ...
        result = run_cli_command(...)  # Returns {"success": bool, "error": str|None, ...}

        # Determine status based on whether the command succeeded
        final_status = TaskStatus.DONE.value if result["success"] else TaskStatus.FAILED.value
        update_job(task_id, status=final_status, results={...})
    except Exception as e:
        traceback.print_exc()
        capture_exception_to_sentry(e)  # Log to Sentry as unhandled
        # Preserve existing results (e.g., partial test_results from intermediate updates)
        existing_job = get_job(task_id)
        existing_results = (existing_job.get("results") or {}) if existing_job else {}
        existing_results["error"] = str(e)
        update_job(task_id, status=TaskStatus.FAILED.value, results=existing_results)
    finally:
        # Start next queued job if capacity allows
        try_start_queued_job(JOB_TYPES)
```

**Preserving Results on Failure**: When a job fails due to an exception, the exception handler fetches existing results from the database and preserves them (e.g., `test_results`, `model_results`) while adding the error message. This ensures that partial results from intermediate updates are still available to clients even when the job ultimately fails.

**Sentry Error Logging**: All job failures are logged to Sentry using the `capture_exception_to_sentry()` utility function from `utils.py`. This function:

- Marks exceptions as **unhandled** so they appear as unresolved issues in Sentry (not handled/resolved)
- Calls `sentry_sdk.flush(timeout=2)` to ensure events are sent immediately (critical for background tasks that may complete before Sentry's async queue is processed)

This applies to:

- STT/TTS evaluation failures (provider-level and task-level)
- Agent test and benchmark failures (model-level and task-level)
- Simulation failures (text and voice)
- CLI command failures (non-zero exit codes OR tracebacks in stderr) - logged with full stderr output

**CLI Failure Detection**: The calibrate CLI may catch exceptions internally and exit with code 0 even when errors occur. To handle this, CLI wrapper functions check for BOTH:

1. Non-zero return code (`process.returncode != 0`)
2. Error tracebacks in stderr (`"Traceback (most recent call last):" in stderr`)

If either condition is true, the job is marked as `FAILED` and logged to Sentry. This ensures errors are properly surfaced even when the CLI doesn't set an appropriate exit code.

### Incremental Job Processing Pattern (Simulations, Agent Tests, Benchmarks)

For long-running jobs that produce incremental outputs (text/voice simulations, agent unit tests, and benchmarks), use non-blocking subprocess execution with polling.

**Note**: This polling-loop pattern applies to simulations (`text`, `voice`), agent unit tests (`llm-unit-test`), and benchmarks (`llm-benchmark`). STT/TTS evaluations use a different approach: the background task blocks on `process.wait()`, but the status API reads intermediate results directly from disk on-demand (see "STT/TTS Evaluation Flow" section).

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

- Check for completion markers (e.g., `evaluation_results.csv` for simulations, `results.json` for agent tests)
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
