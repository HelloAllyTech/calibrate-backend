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
    reserve_port,
    release_port,
    get_s3_client,
    get_s3_output_config,
    can_start_job,
    try_start_queued_job,
    register_job_starter,
    generate_presigned_download_url,
    is_job_timed_out,
    kill_processes_from_dict,
)

# Job types that share the same queue
EVAL_JOB_TYPES = ["stt-eval", "tts-eval"]


def _start_tts_job_from_queue(job: dict) -> bool:
    """Start a TTS evaluation job from the queue.

    This is called by the job queue manager when there's capacity to run a new job.
    """
    job_id = job["uuid"]
    details = job.get("details", {})

    # Reconstruct request from job details
    request = TTSEvaluationRequest(
        texts=details.get("texts", []),
        providers=details.get("providers", []),
        language=details.get("language", ""),
    )
    s3_bucket = details.get("s3_bucket", "")

    # Start background task in a separate thread
    thread = threading.Thread(
        target=run_tts_evaluation_task,
        args=(job_id, request, s3_bucket),
        daemon=True,
    )
    thread.start()

    return True


# Register the job starter for TTS evaluation jobs
register_job_starter("tts-eval", _start_tts_job_from_queue)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tts", tags=["tts"])


class TTSEvaluationRequest(BaseModel):
    texts: List[str]  # List of texts to synthesize
    providers: List[
        str
    ]  # List of TTS providers (e.g., ["smallest", "cartesia", "openai"])
    language: str  # Language (e.g., "english", "hindi")


def _find_tts_provider_output_dir(output_dir: Path, provider: str) -> Optional[Path]:
    """Find the provider-specific output directory."""
    if not output_dir.exists():
        return None
    for item in output_dir.iterdir():
        if item.is_dir() and provider in item.name.lower():
            return item
    # Fallback: try to find any directory
    dirs = [d for d in output_dir.iterdir() if d.is_dir()]
    if dirs:
        return dirs[0]
    return None


def _read_tts_results_csv(provider_output_dir: Path) -> Optional[List[dict]]:
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


def _read_tts_metrics_json(provider_output_dir: Path) -> Optional[List[dict]]:
    """Read metrics.json from provider output directory if it exists."""
    if not provider_output_dir:
        return None
    metrics_file = provider_output_dir / "metrics.json"
    if not metrics_file.exists():
        return None
    try:
        with open(metrics_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _update_tts_intermediate_results(
    output_dir: Path,
    provider: str,
    task_id: str,
    run_id: str,
    s3_bucket: str,
    intermediate_results: dict,
    uploaded_audio_cache: dict,
    results_lock: threading.Lock,
):
    """Update intermediate results for a TTS provider and save to job.

    Uploads audio files to S3 as they become available and stores presigned URLs
    in the intermediate results for immediate playback.
    """
    provider_output_dir = _find_tts_provider_output_dir(output_dir, provider)
    if not provider_output_dir:
        return

    results_data = _read_tts_results_csv(provider_output_dir)
    if results_data is None:
        return

    # Get S3 client for uploading
    s3 = get_s3_client()
    results_prefix = f"tts/evals/{run_id}/outputs/{provider}"

    # Process each row and upload audio files as they become available
    for result_row in results_data:
        if "audio_path" not in result_row or not result_row["audio_path"]:
            continue

        local_audio_path = result_row["audio_path"]

        # Skip if already a presigned URL (already processed)
        if local_audio_path.startswith("http"):
            continue

        # Check if we've already uploaded this audio file
        if local_audio_path in uploaded_audio_cache:
            # Use cached presigned URL
            result_row["audio_path"] = uploaded_audio_cache[local_audio_path]
            continue

        # Check if the audio file exists on disk
        audio_file = Path(local_audio_path)
        if not audio_file.exists():
            continue

        try:
            # Upload audio file to S3
            relative_path = audio_file.relative_to(provider_output_dir)
            s3_key = f"{results_prefix}/{relative_path}"

            s3.upload_file(str(audio_file), s3_bucket, s3_key)
            logger.info(f"Uploaded intermediate audio file to S3: {s3_key}")

            # Generate presigned URL for immediate access
            presigned_url = generate_presigned_download_url(s3_key)
            if presigned_url:
                # Cache the presigned URL for this local path
                uploaded_audio_cache[local_audio_path] = presigned_url
                result_row["audio_path"] = presigned_url
            else:
                # Fallback to S3 key if presigned URL generation fails
                uploaded_audio_cache[local_audio_path] = s3_key
                result_row["audio_path"] = s3_key
        except Exception as e:
            logger.warning(
                f"Failed to upload intermediate audio {local_audio_path}: {e}"
            )
            continue

    # Check if metrics.json exists - if so, provider evaluation is complete
    metrics_data = _read_tts_metrics_json(provider_output_dir)
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


def evaluate_tts_provider(
    run_id: str,
    provider: str,
    language: str,
    input_csv: Path,
    output_dir: Path,
    port: int,
    s3_bucket: str,
    task_id: str,
    running_pids: dict,
    intermediate_results: dict,
    results_lock: threading.Lock,
) -> ProviderResult:
    """Evaluate a single TTS provider."""
    try:
        s3 = get_s3_client()

        # Run pense TTS eval command
        eval_cmd = [
            "pense",
            "tts",
            "eval",
            "-p",
            provider,
            "-l",
            language,
            "-i",
            str(input_csv),
            "-o",
            str(output_dir),
            "--port",
            str(port),
        ]

        logger.info(f"Running TTS eval {run_id} with command: {' '.join(eval_cmd)}")

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
            logger.info(f"TTS eval for {provider} started with PID {process.pid}")

            # Update job details with current running PIDs
            update_job(task_id, details={"running_pids": dict(running_pids)})

            # Cache for uploaded audio files (maps local path -> presigned URL)
            uploaded_audio_cache = {}

            # Poll for process completion while updating intermediate results
            while process.poll() is None:
                _update_tts_intermediate_results(
                    output_dir,
                    provider,
                    task_id,
                    run_id,
                    s3_bucket,
                    intermediate_results,
                    uploaded_audio_cache,
                    results_lock,
                )
                time.sleep(2)  # Check every 2 seconds

            # One final update after process completes
            _update_tts_intermediate_results(
                output_dir,
                provider,
                task_id,
                run_id,
                s3_bucket,
                intermediate_results,
                uploaded_audio_cache,
                results_lock,
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
        provider_output_dir = _find_tts_provider_output_dir(output_dir, provider)

        if provider_output_dir is None:
            raise Exception(f"Could not find provider output directory for {provider}")

        # Upload TTS eval results to S3
        results_prefix = f"tts/evals/{run_id}/outputs/{provider}"

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

        # Upload all files including generated audio files
        # Map local audio paths to S3 keys for presigned URL generation
        audio_files_uploaded = 0
        audio_path_to_s3_key = {}
        for root, dirs, files in os.walk(provider_output_dir):
            for file in files:
                local_file_path = Path(root) / file
                relative_path = local_file_path.relative_to(provider_output_dir)
                s3_key = f"{results_prefix}/{relative_path}"

                # Check if this is an audio file
                is_audio_file = (
                    file.endswith(".wav")
                    or file.endswith(".mp3")
                    or file.endswith(".ogg")
                    or "audios" in str(relative_path).lower()
                )

                s3.upload_file(str(local_file_path), s3_bucket, s3_key)

                if is_audio_file:
                    audio_files_uploaded += 1
                    # Store mapping from local path to S3 key
                    audio_path_to_s3_key[str(local_file_path)] = s3_key
                    logger.info(f"Uploaded audio file {file} to S3: {s3_key}")

        logger.info(
            f"Uploaded {audio_files_uploaded} audio file(s) for provider {provider}"
        )

        # Replace local audio paths with S3 keys in results (presigned URLs generated on fetch)
        if results_data:
            for result_row in results_data:
                if "audio_path" in result_row and result_row["audio_path"]:
                    local_audio_path = result_row["audio_path"]
                    # Look up S3 key from the mapping
                    audio_s3_key = audio_path_to_s3_key.get(local_audio_path)

                    if not audio_s3_key:
                        logger.warning(
                            f"Could not find S3 key for audio path: {local_audio_path}"
                        )
                        continue

                    # Store S3 key instead of presigned URL
                    result_row["audio_path"] = audio_s3_key
                    logger.info(f"Stored S3 key for audio: {audio_s3_key}")

        return ProviderResult(
            provider=provider,
            success=True,
            message=f"TTS evaluation completed successfully for {provider}",
            metrics=metrics_data,
            results=results_data,
        )

    except subprocess.CalledProcessError as e:
        traceback.print_exc()
        return ProviderResult(
            provider=provider,
            success=False,
            message=f"TTS eval failed: {e.stderr}",
        )
    except Exception as e:
        traceback.print_exc()
        return ProviderResult(
            provider=provider,
            success=False,
            message=f"Unexpected error: {str(e)}",
        )


def run_tts_evaluation_task(
    task_id: str,
    request: TTSEvaluationRequest,
    s3_bucket: str,
):
    """Run the TTS evaluation in the background."""
    provider_ports = {}  # Track reserved ports for cleanup
    try:
        logger.info(
            f"Running TTS evaluation task {task_id} with {len(request.providers)} providers"
        )
        update_job(task_id, status=TaskStatus.IN_PROGRESS.value)

        s3 = get_s3_client()

        # Create temporary directory for processing
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            try:
                # Create input CSV file
                input_csv = temp_path / "input.csv"
                with open(input_csv, "w", newline="", encoding="utf-8") as csvfile:
                    writer = csv.writer(csvfile)
                    writer.writerow(["id", "text"])
                    for idx, text in enumerate(request.texts):
                        writer.writerow([idx, text])

                # Create output directory
                output_dir = temp_path / "output"
                output_dir.mkdir()

                # Reserve ports for each provider
                start_port = 8765
                for provider in request.providers:
                    port = reserve_port(f"{task_id}_{provider}", start_port)
                    provider_ports[provider] = port
                    start_port = port + 1

                # Store ports in job details for cleanup
                update_job(task_id, details={"provider_ports": dict(provider_ports)})

                # Run pense TTS eval for all providers in parallel
                provider_results = []

                logger.info(
                    f"Running {len(request.providers)} TTS providers in parallel"
                )

                # Shared dict to track running process PIDs for cleanup
                running_pids = {}

                # Shared dict and lock for intermediate results
                intermediate_results = {}
                results_lock = threading.Lock()

                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=len(request.providers)
                ) as executor:
                    future_to_provider = {
                        executor.submit(
                            evaluate_tts_provider,
                            task_id,
                            provider,
                            request.language,
                            input_csv,
                            output_dir,
                            provider_ports[provider],
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

                logger.info("Completed running all TTS providers in parallel")

                # Check if all providers succeeded
                all_succeeded = all(r.success for r in provider_results)
                if not all_succeeded:
                    failed_providers = [
                        r.provider for r in provider_results if not r.success
                    ]
                    update_job(
                        task_id,
                        status=TaskStatus.DONE.value,
                        results={
                            "provider_results": [
                                r.model_dump() for r in provider_results
                            ],
                            "leaderboard_summary": None,
                            "error": f"Some providers failed: {', '.join(failed_providers)}",
                        },
                    )
                    return

                # Run pense TTS leaderboard command
                leaderboard_dir = temp_path / "leaderboard"
                leaderboard_dir.mkdir()

                leaderboard_cmd = [
                    "pense",
                    "tts",
                    "leaderboard",
                    "-o",
                    str(output_dir),
                    "-s",
                    str(leaderboard_dir),
                ]

                leaderboard_prefix = f"tts/evals/{task_id}/leaderboard"
                leaderboard_summary = None

                logger.info(
                    f"Running TTS leaderboard command: {' '.join(leaderboard_cmd)}"
                )

                try:
                    result = subprocess.run(
                        leaderboard_cmd,
                        capture_output=True,
                        text=True,
                        check=True,
                        cwd=temp_path,
                    )

                    logger.info("TTS leaderboard command completed successfully")

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
                            if file == "tts_leaderboard.xlsx":
                                logger.info(
                                    f"Found TTS leaderboard metrics file: {local_file_path}"
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

                                        logger.info("Preparing TTS leaderboard summary")

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
                                            f"Prepared TTS leaderboard summary with {len(leaderboard_summary)} rows"
                                        )
                                except Exception as e:
                                    traceback.print_exc()
                                    # If reading xlsx fails, continue without summary
                                    pass

                except subprocess.CalledProcessError as e:
                    # Leaderboard failure is not critical, continue
                    pass

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
                    status=TaskStatus.DONE.value,
                    results={
                        "error": f"Unexpected error during TTS evaluation: {str(e)}",
                    },
                )

    except Exception as e:
        traceback.print_exc()
        update_job(
            task_id,
            status=TaskStatus.DONE.value,
            results={"error": f"Task failed: {str(e)}"},
        )
    finally:
        # Release all reserved ports
        for port in provider_ports.values():
            release_port(port)

        # Try to start the next queued job
        try_start_queued_job(EVAL_JOB_TYPES)


@router.post("/evaluate", response_model=TaskCreateResponse)
async def evaluate_tts(
    request: TTSEvaluationRequest, user_id: str = Depends(get_current_user_id)
):
    """
    Start a background task to evaluate multiple TTS providers with text inputs.

    Returns a task ID that can be used to poll for status and results.
    """
    # Validate input
    if not request.texts:
        raise HTTPException(
            status_code=400,
            detail="At least one text must be provided",
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
        job_type="tts-eval",
        user_id=user_id,
        status=initial_status,
        details={
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
            target=run_tts_evaluation_task,
            args=(job_id, request, s3_bucket),
            daemon=True,
        )
        thread.start()
        logger.info(f"Started TTS evaluation job {job_id} immediately")
    else:
        logger.info(f"Queued TTS evaluation job {job_id}")

    return TaskCreateResponse(task_id=job_id, status=initial_status)


@router.get("/evaluate/{task_id}", response_model=TaskStatusResponse)
async def get_tts_evaluation_status(
    task_id: str, user_id: str = Depends(get_current_user_id)
):
    """
    Get the status of a TTS evaluation task.

    Returns the current status and, if done, the provider results and leaderboard path.
    """
    job = get_job(task_id, user_id=user_id)
    if not job:
        raise HTTPException(status_code=404, detail="Task not found")

    status = job["status"]
    results = job.get("results") or {}
    details = job.get("details") or {}
    provider_results = results.get("provider_results")

    # Check for timeout on in-progress jobs
    if status == TaskStatus.IN_PROGRESS.value:
        updated_at = job.get("updated_at")
        if updated_at and is_job_timed_out(updated_at):
            logger.warning(f"Job {task_id} timed out, marking as failed")

            # Kill running processes
            running_pids = details.get("running_pids")
            if running_pids:
                kill_processes_from_dict(running_pids, task_id)

            # Release ports
            provider_ports = details.get("provider_ports")
            if provider_ports:
                for port in provider_ports.values():
                    release_port(port)

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

    # Generate presigned URLs on the fly for completed jobs
    if status == TaskStatus.DONE.value and provider_results:
        for provider_result in provider_results:
            if provider_result.get("results"):
                for result_row in provider_result["results"]:
                    if "audio_path" in result_row and result_row["audio_path"]:
                        audio_s3_key = result_row["audio_path"]
                        # Skip if already a URL (backwards compatibility)
                        if audio_s3_key.startswith("http") or audio_s3_key.startswith(
                            "s3://"
                        ):
                            continue
                        presigned_url = generate_presigned_download_url(audio_s3_key)
                        if presigned_url:
                            result_row["audio_path"] = presigned_url

    return TaskStatusResponse(
        task_id=task_id,
        status=status,
        provider_results=provider_results,
        leaderboard_summary=results.get("leaderboard_summary"),
        error=results.get("error"),
    )
