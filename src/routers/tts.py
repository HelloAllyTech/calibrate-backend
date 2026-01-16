import os
import csv
import json
import subprocess
import tempfile
import traceback
import concurrent.futures
import threading
import logging
from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import openpyxl

from db import create_job, get_job, update_job
from utils import (
    TaskStatus,
    ProviderResult,
    TaskCreateResponse,
    TaskStatusResponse,
    find_available_port,
    get_s3_client,
    get_s3_output_config,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tts", tags=["tts"])


class TTSEvaluationRequest(BaseModel):
    texts: List[str]  # List of texts to synthesize
    providers: List[
        str
    ]  # List of TTS providers (e.g., ["smallest", "cartesia", "openai"])
    language: str  # Language (e.g., "english", "hindi")


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

        # Use Popen with start_new_session to create a process group for cleanup
        process = subprocess.Popen(
            eval_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,  # Create new process group for cleanup
            cwd=str(output_dir.parent),
        )

        # Track the process PID for cleanup on server restart
        running_pids[provider] = process.pid
        logger.info(f"TTS eval for {provider} started with PID {process.pid}")

        # Update job details with current running PIDs
        update_job(task_id, details={"running_pids": dict(running_pids)})

        # Wait for process to complete
        stdout, stderr = process.communicate()

        # Remove from running PIDs
        running_pids.pop(provider, None)
        update_job(task_id, details={"running_pids": dict(running_pids)})

        if process.returncode != 0:
            raise subprocess.CalledProcessError(
                process.returncode, eval_cmd, stdout, stderr
            )

        # Find the provider-specific output directory
        provider_output_dir = None
        for item in output_dir.iterdir():
            if item.is_dir() and provider in item.name.lower():
                provider_output_dir = item
                break

        if provider_output_dir is None:
            # Try to find any directory in output_dir
            dirs = [d for d in output_dir.iterdir() if d.is_dir()]
            if dirs:
                provider_output_dir = dirs[0]
            else:
                raise Exception(
                    f"Could not find provider output directory for {provider}"
                )

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

        # Replace local audio paths with presigned S3 URLs in results
        if results_data:
            expiration = 3600  # 1 hour expiration
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

                    # Generate presigned URL
                    try:
                        presigned_url = s3.generate_presigned_url(
                            "get_object",
                            Params={
                                "Bucket": s3_bucket,
                                "Key": audio_s3_key,
                            },
                            ExpiresIn=expiration,
                        )
                        result_row["audio_path"] = presigned_url
                        logger.info(
                            f"Replaced audio path with presigned URL for {audio_s3_key}"
                        )
                    except Exception as e:
                        logger.warning(
                            f"Failed to generate presigned URL for {audio_s3_key}: {str(e)}"
                        )
                        # Fallback to S3 path if presigned URL generation fails
                        result_row["audio_path"] = f"s3://{s3_bucket}/{audio_s3_key}"

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

                # Find available ports for each provider
                provider_ports = {}
                start_port = 8000
                for provider in request.providers:
                    port = find_available_port(start_port)
                    provider_ports[provider] = port
                    start_port = port + 1

                # Run pense TTS eval for all providers in parallel
                provider_results = []

                logger.info(
                    f"Running {len(request.providers)} TTS providers in parallel"
                )

                # Shared dict to track running process PIDs for cleanup
                running_pids = {}

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


@router.post("/evaluate", response_model=TaskCreateResponse)
async def evaluate_tts(request: TTSEvaluationRequest):
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

    # Create job in database with details for recovery
    job_id = create_job(
        job_type="tts-eval",
        status=TaskStatus.IN_PROGRESS.value,
        details={
            "texts": request.texts,
            "providers": request.providers,
            "language": request.language,
            "s3_bucket": s3_bucket,
        },
        results=None,
    )

    # Start background task in a separate thread
    thread = threading.Thread(
        target=run_tts_evaluation_task,
        args=(job_id, request, s3_bucket),
        daemon=True,
    )
    thread.start()

    return TaskCreateResponse(task_id=job_id, status=TaskStatus.IN_PROGRESS.value)


@router.get("/evaluate/{task_id}", response_model=TaskStatusResponse)
async def get_tts_evaluation_status(task_id: str):
    """
    Get the status of a TTS evaluation task.

    Returns the current status and, if done, the provider results and leaderboard path.
    """
    job = get_job(task_id)
    if not job:
        raise HTTPException(status_code=404, detail="Task not found")

    results = job.get("results") or {}

    return TaskStatusResponse(
        task_id=task_id,
        status=job["status"],
        provider_results=results.get("provider_results"),
        leaderboard_summary=results.get("leaderboard_summary"),
        error=results.get("error"),
    )
