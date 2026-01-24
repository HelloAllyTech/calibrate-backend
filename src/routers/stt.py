import os
import csv
import json
import subprocess
import tempfile
import time
import traceback
import concurrent.futures
import threading
import logging
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
import openpyxl

from db import create_job, get_job, update_job
from auth_utils import get_current_user_id
from utils import (
    TaskStatus,
    ProviderResult,
    TaskCreateResponse,
    TaskStatusResponse,
    get_s3_client,
    get_s3_output_config,
    can_start_job,
    try_start_queued_job,
    register_job_starter,
    is_job_timed_out,
    kill_processes_from_dict,
)

# Job types that share the same queue
EVAL_JOB_TYPES = ["stt-eval", "tts-eval"]


def _start_stt_job_from_queue(job: dict) -> bool:
    """Start an STT evaluation job from the queue.

    This is called by the job queue manager when there's capacity to run a new job.
    """
    job_id = job["uuid"]
    details = job.get("details", {})

    # Reconstruct request from job details
    request = STTEvaluationRequest(
        audio_paths=details.get("audio_paths", []),
        texts=details.get("texts", []),
        providers=details.get("providers", []),
        language=details.get("language", ""),
    )
    s3_bucket = details.get("s3_bucket", "")

    # Start background task in a separate thread
    thread = threading.Thread(
        target=run_evaluation_task,
        args=(job_id, request, s3_bucket),
        daemon=True,
    )
    thread.start()

    return True


# Register the job starter for STT evaluation jobs
register_job_starter("stt-eval", _start_stt_job_from_queue)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stt", tags=["stt"])


def _normalize_metrics(metrics):
    """Convert old list-of-dicts metrics format to new dict format.

    Old format: [{"wer": 2.4}, {"string_similarity": 0.15}, {"metric_name": "ttfb", "mean": 0.1, ...}, ...]
    New format: {"wer": 2.4, "string_similarity": 0.15, "ttfb": {"mean": 0.1, ...}, ...}
    """
    if metrics is None:
        return None
    if isinstance(metrics, dict):
        return metrics
    if isinstance(metrics, list):
        # Convert list of dicts to single dict
        result = {}
        for item in metrics:
            if isinstance(item, dict):
                # Check if it's a latency metric with metric_name field
                if "metric_name" in item:
                    metric_name = item["metric_name"]
                    # Create a copy without metric_name for the value
                    value = {k: v for k, v in item.items() if k != "metric_name"}
                    result[metric_name] = value
                else:
                    # Simple metric: {"wer": 2.4} - merge directly
                    result.update(item)
        return result if result else metrics  # Return original if conversion fails
    return metrics


class STTEvaluationRequest(BaseModel):
    audio_paths: List[str]  # S3 paths to audio files
    texts: List[str]  # Ground truth text for each audio file
    providers: List[
        str
    ]  # List of STT providers (e.g., ["deepgram", "openai", "sarvam"])
    language: str  # Language (e.g., "english", "hindi")


def _find_provider_output_dir(output_dir: Path, provider: str) -> Optional[Path]:
    """Find the provider-specific output directory."""
    if not output_dir.exists():
        return None
    for item in output_dir.iterdir():
        if item.is_dir() and provider in item.name.lower():
            return item
    return None


def _read_results_csv(provider_output_dir: Path) -> Optional[List[dict]]:
    """Read results.csv from provider output directory if it exists."""
    if not provider_output_dir:
        return None
    results_file = provider_output_dir / "results.csv"
    if not results_file.exists():
        return None
    try:
        results_data = []
        with open(results_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                results_data.append(dict(row))
        return results_data
    except Exception:
        return None


def _read_metrics_json(provider_output_dir: Path) -> Optional[dict]:
    """Read metrics.json from provider output directory if it exists.

    Handles both new format (dict) and old format (list of dicts) for backward compatibility.
    """
    if not provider_output_dir:
        return None
    metrics_file = provider_output_dir / "metrics.json"
    if not metrics_file.exists():
        return None
    try:
        with open(metrics_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Handle backward compatibility: if it's a list, return as-is
            # New format is a dict
            return data
    except Exception:
        return None


def _update_intermediate_results(
    output_dir: Path,
    provider: str,
    task_id: str,
    intermediate_results: dict,
    results_lock: threading.Lock,
):
    """Update intermediate results for a provider and save to job."""
    provider_output_dir = _find_provider_output_dir(output_dir, provider)
    if not provider_output_dir:
        return

    results_data = _read_results_csv(provider_output_dir)
    if results_data is None:
        return

    # Check if metrics.json exists - if so, provider evaluation is complete
    metrics_data = _read_metrics_json(provider_output_dir)
    is_complete = metrics_data is not None

    with results_lock:
        intermediate_results[provider] = {
            "provider": provider,
            "success": True if is_complete else None,
            "message": "Completed" if is_complete else "Processing...",
            "metrics": metrics_data,
            "results": results_data,
        }
        # Update job with current intermediate results
        update_job(
            task_id,
            results={
                "provider_results": list(intermediate_results.values()),
                "leaderboard_summary": None,
                "error": None,
            },
        )


def evaluate_provider(
    run_id: str,
    provider: str,
    language: str,
    input_dir: Path,
    output_dir: Path,
    s3_bucket: str,
    task_id: str,
    running_pids: dict,
    intermediate_results: dict,
    results_lock: threading.Lock,
) -> ProviderResult:
    """Evaluate a single STT provider."""
    try:
        s3 = get_s3_client()

        # Run pense STT eval command
        eval_cmd = [
            "pense",
            "stt",
            "eval",
            "-p",
            provider,
            "-l",
            language,
            "-i",
            str(input_dir),
            "-o",
            str(output_dir),
        ]

        logger.info(f"Running {run_id} with command: {' '.join(eval_cmd)}")

        # Create temp files for stdout/stderr to avoid pipe buffer issues during polling
        stdout_path = output_dir / f"{provider}_stdout.log"
        stderr_path = output_dir / f"{provider}_stderr.log"

        stdout_f = open(stdout_path, "w")
        stderr_f = open(stderr_path, "w")

        try:
            # Use Popen with start_new_session to create a process group for cleanup
            process = subprocess.Popen(
                eval_cmd,
                stdout=stdout_f,
                stderr=stderr_f,
                text=True,
                start_new_session=True,  # Create new process group for cleanup
                cwd=str(output_dir.parent),
            )

            # Track the process PID for cleanup on server restart
            running_pids[provider] = process.pid
            logger.info(f"STT eval for {provider} started with PID {process.pid}")

            # Update job details with current running PIDs
            update_job(task_id, details={"running_pids": dict(running_pids)})

            # Poll for process completion while updating intermediate results
            while process.poll() is None:
                _update_intermediate_results(
                    output_dir, provider, task_id, intermediate_results, results_lock
                )
                time.sleep(2)  # Check every 2 seconds

            # One final update after process completes
            _update_intermediate_results(
                output_dir, provider, task_id, intermediate_results, results_lock
            )

        finally:
            stdout_f.close()
            stderr_f.close()

        # Read stdout/stderr from files
        with open(stdout_path, "r") as f:
            stdout = f.read()
        with open(stderr_path, "r") as f:
            stderr = f.read()

        # Remove from running PIDs
        running_pids.pop(provider, None)
        update_job(task_id, details={"running_pids": dict(running_pids)})

        if process.returncode != 0:
            raise subprocess.CalledProcessError(
                process.returncode, eval_cmd, stdout, stderr
            )

        # Find the provider-specific output directory
        provider_output_dir = _find_provider_output_dir(output_dir, provider)

        # Upload STT eval results to S3
        results_prefix = f"stt/evals/{run_id}/outputs/{provider}"

        # Read metrics.json and results.csv before uploading
        metrics_data = None
        results_data = None

        metrics_file = provider_output_dir / "metrics.json"
        results_file = provider_output_dir / "results.csv"

        if metrics_file.exists():
            with open(metrics_file, "r", encoding="utf-8") as f:
                metrics_data = json.load(f)

        if results_file.exists():
            results_data = []
            with open(results_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    results_data.append(dict(row))

        for root, dirs, files in os.walk(provider_output_dir):
            for file in files:
                local_file_path = Path(root) / file
                relative_path = local_file_path.relative_to(provider_output_dir)
                s3_key = f"{results_prefix}/{relative_path}"

                s3.upload_file(str(local_file_path), s3_bucket, s3_key)

        return ProviderResult(
            provider=provider,
            success=True,
            message=f"STT evaluation completed successfully for {provider}",
            metrics=metrics_data,
            results=results_data,
        )

    except subprocess.CalledProcessError as e:
        traceback.print_exc()
        return ProviderResult(
            provider=provider,
            success=False,
            message=f"STT eval failed: {e.stderr}",
        )
    except Exception as e:
        traceback.print_exc()
        return ProviderResult(
            provider=provider,
            success=False,
            message=f"Unexpected error: {str(e)}",
        )


def run_evaluation_task(
    task_id: str,
    request: STTEvaluationRequest,
    s3_bucket: str,
):
    """Run the STT evaluation in the background."""
    try:
        logger.info(
            f"Running evaluation task {task_id} with {len(request.providers)} providers"
        )
        update_job(task_id, status=TaskStatus.IN_PROGRESS.value)

        s3 = get_s3_client()

        # Create temporary directory for processing
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            try:
                # Create directory structure
                input_dir = temp_path / "input"
                input_dir.mkdir()
                audios_dir = input_dir / "audios"
                audios_dir.mkdir(parents=True)

                # Download audio files from S3 and create CSV
                stt_csv_path = input_dir / "stt.csv"
                with open(stt_csv_path, "w", newline="", encoding="utf-8") as csvfile:
                    writer = csv.writer(csvfile)
                    writer.writerow(["id", "text"])

                    for idx, (audio_path, gt_text) in enumerate(
                        zip(request.audio_paths, request.texts)
                    ):
                        # Parse S3 path (format: s3://bucket/key or bucket/key)
                        if audio_path.startswith("s3://"):
                            parts = audio_path[5:].split("/", 1)
                            bucket = parts[0]
                            key = parts[1] if len(parts) > 1 else ""
                        else:
                            parts = audio_path.split("/", 1)
                            bucket = parts[0]
                            key = parts[1] if len(parts) > 1 else ""

                        # Generate audio ID
                        audio_id = f"audio_{idx + 1}"

                        # Download audio file directly to audios folder
                        local_audio_path = audios_dir / f"{audio_id}.wav"

                        logger.info(
                            f"Downloading audio file from {bucket}/{key} to {local_audio_path}"
                        )
                        s3.download_file(bucket, key, str(local_audio_path))

                        # Write CSV row
                        writer.writerow([audio_id, gt_text])

                # Create output directory
                output_dir = temp_path / "output"
                output_dir.mkdir()

                # Run pense STT eval for all providers in parallel
                provider_results = []

                logger.info(f"Running {len(request.providers)} providers in parallel")

                # Shared dict to track running process PIDs for cleanup
                running_pids = {}

                # Shared dict and lock for intermediate results
                intermediate_results = {}
                results_lock = threading.Lock()

                # Limit to 2 concurrent providers to avoid resource exhaustion
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(2, len(request.providers))
                ) as executor:
                    future_to_provider = {
                        executor.submit(
                            evaluate_provider,
                            task_id,
                            provider,
                            request.language,
                            input_dir,
                            output_dir,
                            s3_bucket,
                            task_id,
                            running_pids,
                            intermediate_results,
                            results_lock,
                        ): provider
                        for provider in request.providers
                    }

                    for future in concurrent.futures.as_completed(future_to_provider):
                        result = future.result()
                        provider_results.append(result)

                logger.info("Completed running all providers in parallel")

                # Check if all providers succeeded
                all_succeeded = all(r.success for r in provider_results)
                if not all_succeeded:
                    failed_providers = [
                        r.provider for r in provider_results if not r.success
                    ]
                    # Get error messages from failed providers
                    error_details = [
                        f"{r.provider}: {r.message}"
                        for r in provider_results
                        if not r.success
                    ]
                    update_job(
                        task_id,
                        status=TaskStatus.FAILED.value,
                        results={
                            "provider_results": [
                                r.model_dump() for r in provider_results
                            ],
                            "leaderboard_summary": None,
                            "error": f"Some providers failed: {'; '.join(error_details)}",
                        },
                    )
                    return

                # Run pense STT leaderboard command
                leaderboard_dir = temp_path / "leaderboard"
                leaderboard_dir.mkdir()

                leaderboard_cmd = [
                    "pense",
                    "stt",
                    "leaderboard",
                    "-o",
                    str(output_dir),
                    "-s",
                    str(leaderboard_dir),
                ]

                leaderboard_prefix = f"stt/evals/{task_id}/leaderboard"
                leaderboard_summary = None

                logger.info(f"Running leaderboard command: {' '.join(leaderboard_cmd)}")

                try:
                    result = subprocess.run(
                        leaderboard_cmd,
                        capture_output=True,
                        text=True,
                        check=True,
                        cwd=temp_path,
                    )

                    logger.info("Leaderboard command completed successfully")

                    if result.stdout:
                        logger.info(f"Leaderboard stdout: {result.stdout}")
                    if result.stderr:
                        logger.info(f"Leaderboard stderr: {result.stderr}")

                    # Upload leaderboard results
                    for root, dirs, files in os.walk(leaderboard_dir):
                        for file in files:
                            local_file_path = Path(root) / file
                            relative_path = local_file_path.relative_to(leaderboard_dir)
                            s3_key = f"{leaderboard_prefix}/{relative_path}"

                            s3.upload_file(str(local_file_path), s3_bucket, s3_key)

                            # Read xlsx file and extract summary sheet
                            if file == "stt_leaderboard.xlsx":
                                logger.info(
                                    f"Found leaderboard metrics file: {local_file_path}"
                                )
                                try:
                                    wb = openpyxl.load_workbook(
                                        str(local_file_path), data_only=True
                                    )
                                    if "summary" in wb.sheetnames:
                                        ws = wb["summary"]
                                        # Get headers from first row (skip empty cells)
                                        headers = [
                                            cell.value
                                            for cell in ws[1]
                                            if cell.value is not None
                                        ]

                                        logger.info("Preparing leaderboard summary")

                                        # Read all data rows
                                        leaderboard_summary = []

                                        for row in ws.iter_rows(
                                            min_row=2, values_only=False
                                        ):
                                            # Check if row has any data
                                            if any(
                                                cell.value is not None for cell in row
                                            ):
                                                row_dict = {}
                                                for idx, cell in enumerate(row):
                                                    if idx < len(headers):
                                                        # Use header name as key, cell value as value
                                                        row_dict[headers[idx]] = (
                                                            cell.value
                                                        )
                                                # Only add row if it has at least one non-None value
                                                if any(
                                                    v is not None
                                                    for v in row_dict.values()
                                                ):
                                                    leaderboard_summary.append(row_dict)

                                        logger.info(
                                            f"Prepared leaderboard summary with {len(leaderboard_summary)} rows"
                                        )
                                except Exception as e:
                                    traceback.print_exc()
                                    # If reading xlsx fails, continue without summary
                                    raise e

                except subprocess.CalledProcessError as e:
                    # Leaderboard failure is critical too
                    traceback.print_exc()
                    raise e

                # Update job with results
                update_job(
                    task_id,
                    status=TaskStatus.DONE.value,
                    results={
                        "provider_results": [r.model_dump() for r in provider_results],
                        "leaderboard_summary": leaderboard_summary,
                        "error": None,
                    },
                )

            except Exception as e:
                traceback.print_exc()
                update_job(
                    task_id,
                    status=TaskStatus.FAILED.value,
                    results={
                        "error": f"Unexpected error during STT evaluation: {str(e)}",
                    },
                )

    except Exception as e:
        traceback.print_exc()
        update_job(
            task_id,
            status=TaskStatus.FAILED.value,
            results={"error": f"Task failed: {str(e)}"},
        )
    finally:
        # Try to start the next queued job
        try_start_queued_job(EVAL_JOB_TYPES)


@router.post("/evaluate", response_model=TaskCreateResponse)
async def evaluate_stt(
    request: STTEvaluationRequest, user_id: str = Depends(get_current_user_id)
):
    """
    Start a background task to evaluate multiple STT providers with audio files from S3.

    Returns a task ID that can be used to poll for status and results.
    """
    # Validate input
    if len(request.audio_paths) != len(request.texts):
        raise HTTPException(
            status_code=400,
            detail="Number of audio paths must match number of ground truth texts",
        )

    if not request.providers:
        raise HTTPException(
            status_code=400,
            detail="At least one provider must be specified",
        )

    # Get S3 configuration from environment
    try:
        s3_bucket = get_s3_output_config()
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Check if we can start immediately or need to queue
    can_start = can_start_job(EVAL_JOB_TYPES)
    initial_status = (
        TaskStatus.IN_PROGRESS.value if can_start else TaskStatus.QUEUED.value
    )

    # Create job in database with details for recovery
    job_id = create_job(
        job_type="stt-eval",
        user_id=user_id,
        status=initial_status,
        details={
            "audio_paths": request.audio_paths,
            "texts": request.texts,
            "providers": request.providers,
            "language": request.language,
            "s3_bucket": s3_bucket,
        },
        results=None,
    )

    if can_start:
        # Start background task in a separate thread
        thread = threading.Thread(
            target=run_evaluation_task,
            args=(job_id, request, s3_bucket),
            daemon=True,
        )
        thread.start()
        logger.info(f"Started STT evaluation job {job_id} immediately")
    else:
        logger.info(f"Queued STT evaluation job {job_id}")

    return TaskCreateResponse(task_id=job_id, status=initial_status)


@router.get("/evaluate/{task_id}", response_model=TaskStatusResponse)
async def get_evaluation_status(
    task_id: str, user_id: str = Depends(get_current_user_id)
):
    """
    Get the status of an STT evaluation task.

    Returns the current status and, if done, the provider results and leaderboard path.
    """
    job = get_job(task_id, user_id=user_id)
    if not job:
        raise HTTPException(status_code=404, detail="Task not found")

    status = job["status"]
    results = job.get("results") or {}
    details = job.get("details") or {}

    # Check for timeout on in-progress jobs
    if status == TaskStatus.IN_PROGRESS.value:
        updated_at = job.get("updated_at")
        if updated_at and is_job_timed_out(updated_at):
            logger.warning(f"Job {task_id} timed out, marking as failed")

            # Kill running processes
            running_pids = details.get("running_pids")
            if running_pids:
                kill_processes_from_dict(running_pids, task_id)

            # Mark job as failed (preserve existing results, add error)
            results["error"] = "Job timed out after 5 minutes of inactivity"
            update_job(
                task_id,
                status=TaskStatus.FAILED.value,
                results=results,
            )
            status = TaskStatus.FAILED.value

            # Try to start the next queued job
            try_start_queued_job(EVAL_JOB_TYPES)

    # Normalize metrics format for backward compatibility (list -> dict)
    provider_results = results.get("provider_results")
    if provider_results:
        for provider_result in provider_results:
            if provider_result.get("metrics"):
                provider_result["metrics"] = _normalize_metrics(
                    provider_result["metrics"]
                )

    return TaskStatusResponse(
        task_id=task_id,
        status=status,
        language=details.get("language"),
        provider_results=provider_results,
        leaderboard_summary=results.get("leaderboard_summary"),
        error=results.get("error"),
    )
