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
    reserve_port,
    release_port,
    get_s3_client,
    get_s3_output_config,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stt", tags=["stt"])


class STTEvaluationRequest(BaseModel):
    audio_paths: List[str]  # S3 paths to audio files
    texts: List[str]  # Ground truth text for each audio file
    providers: List[
        str
    ]  # List of STT providers (e.g., ["deepgram", "openai", "sarvam"])
    language: str  # Language (e.g., "english", "hindi")


def evaluate_provider(
    run_id: str,
    provider: str,
    language: str,
    input_dir: Path,
    output_dir: Path,
    port: int,
    s3_bucket: str,
    task_id: str,
    running_pids: dict,
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
            "--port",
            str(port),
        ]

        logger.info(f"Running {run_id} with command: {' '.join(eval_cmd)}")

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
        logger.info(f"STT eval for {provider} started with PID {process.pid}")

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
    provider_ports = {}  # Track reserved ports for cleanup
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
                audios_wav_dir = input_dir / "audios" / "wav"
                audios_pcm16_dir = input_dir / "audios" / "pcm16"
                audios_wav_dir.mkdir(parents=True)
                audios_pcm16_dir.mkdir(parents=True)

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

                        # Download audio file
                        local_wav_path = audios_wav_dir / f"{audio_id}.wav"
                        local_pcm16_path = audios_pcm16_dir / f"{audio_id}.wav"

                        logger.info(
                            f"Downloading audio file from {bucket}/{key} to {local_wav_path}"
                        )
                        s3.download_file(bucket, key, str(local_wav_path))

                        # Convert to pcm16 (mono, 16KHz, s16le) using ffmpeg instead of copying
                        cmd = [
                            "ffmpeg",
                            "-y",
                            "-i",
                            str(local_wav_path),
                            "-ac",
                            "1",
                            "-ar",
                            "16000",
                            "-f",
                            "wav",
                            "-sample_fmt",
                            "s16",
                            str(local_pcm16_path),
                        ]

                        logger.info(
                            f"Converting audio file to pcm16 {local_pcm16_path}"
                        )
                        subprocess.run(cmd, check=True)

                        # Write CSV row
                        writer.writerow([audio_id, gt_text])

                # Create output directory
                output_dir = temp_path / "output"
                output_dir.mkdir()

                # Reserve ports for each provider
                start_port = 8000
                for provider in request.providers:
                    port = reserve_port(f"{task_id}_{provider}", start_port)
                    provider_ports[provider] = port
                    start_port = port + 1

                # Run pense STT eval for all providers in parallel
                provider_results = []

                logger.info(f"Running {len(request.providers)} providers in parallel")

                # Shared dict to track running process PIDs for cleanup
                running_pids = {}

                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=len(request.providers)
                ) as executor:
                    future_to_provider = {
                        executor.submit(
                            evaluate_provider,
                            task_id,
                            provider,
                            request.language,
                            input_dir,
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

                logger.info("Completed running all providers in parallel")

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
                    status=TaskStatus.DONE.value,
                    results={
                        "error": f"Unexpected error during STT evaluation: {str(e)}",
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


@router.post("/evaluate", response_model=TaskCreateResponse)
async def evaluate_stt(request: STTEvaluationRequest):
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

    # Create job in database with details for recovery
    job_id = create_job(
        job_type="stt-eval",
        status=TaskStatus.IN_PROGRESS.value,
        details={
            "audio_paths": request.audio_paths,
            "texts": request.texts,
            "providers": request.providers,
            "language": request.language,
            "s3_bucket": s3_bucket,
        },
        results=None,
    )

    # Start background task in a separate thread
    thread = threading.Thread(
        target=run_evaluation_task,
        args=(job_id, request, s3_bucket),
        daemon=True,
    )
    thread.start()

    return TaskCreateResponse(task_id=job_id, status=TaskStatus.IN_PROGRESS.value)


@router.get("/evaluate/{task_id}", response_model=TaskStatusResponse)
async def get_evaluation_status(task_id: str):
    """
    Get the status of an STT evaluation task.

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
