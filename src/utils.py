import os
import socket
import logging
import threading
from enum import Enum
from typing import List, Optional, Dict, Any

import boto3
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# In-memory task storage (shared across routers)
tasks = {}
tasks_lock = threading.Lock()

# In-memory port registry (shared across stt, tts, simulations)
# Maps port -> job_id to track which ports are in use
_reserved_ports: Dict[int, str] = {}
_ports_lock = threading.Lock()


class TaskStatus(str, Enum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    CANCELLED = "cancelled"
    DONE = "done"


class ProviderResult(BaseModel):
    provider: str
    success: bool
    message: str
    metrics: Optional[List[Dict[str, Any]]] = None
    results: Optional[List[Dict[str, Any]]] = None


class TaskCreateResponse(BaseModel):
    task_id: str
    status: str


class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    provider_results: Optional[List[ProviderResult]] = None
    leaderboard_summary: Optional[List[Dict[str, Any]]] = None
    error: Optional[str] = None


def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("localhost", port)) == 0


def find_available_port(start_port: int = 8000) -> int:
    """Find an available port starting from start_port.

    Note: This only checks if the port is in use at the OS level.
    For job-level port management, use reserve_port() instead.
    """
    port = start_port
    while True:
        logger.debug(f"Checking port {port}")
        if is_port_in_use(port):
            port += 1
            if port > 65535:
                raise RuntimeError("No available ports found")
            continue

        return port


def reserve_port(job_id: str, start_port: int = 8000) -> int:
    """Find and reserve an available port for a job.

    This checks both OS-level port usage and the shared port registry
    to ensure the port is not being used by another job.

    Args:
        job_id: The job ID to associate with this port
        start_port: The port number to start searching from

    Returns:
        The reserved port number
    """
    with _ports_lock:
        port = start_port
        while True:
            logger.debug(f"Checking port {port} for job {job_id}")

            # Check if port is in our registry (used by another job)
            if port in _reserved_ports:
                logger.debug(f"Port {port} is reserved by job {_reserved_ports[port]}")
                port += 1
                if port > 65535:
                    raise RuntimeError("No available ports found")
                continue

            # Check if port is in use at OS level
            if is_port_in_use(port):
                logger.debug(f"Port {port} is in use at OS level")
                port += 1
                if port > 65535:
                    raise RuntimeError("No available ports found")
                continue

            # Port is available, reserve it
            _reserved_ports[port] = job_id
            logger.info(f"Reserved port {port} for job {job_id}")
            return port


def release_port(port: int) -> None:
    """Release a previously reserved port.

    Args:
        port: The port number to release
    """
    job_id = _reserved_ports.pop(port, None)
    if job_id:
        logger.info(f"Released port {port} (was used by job {job_id})")
    else:
        logger.debug(f"Port {port} was not in the reserved ports registry")


def get_reserved_ports() -> Dict[int, str]:
    """Get a copy of the current reserved ports mapping.

    Returns:
        Dict mapping port numbers to job IDs
    """
    return dict(_reserved_ports)


def get_s3_client():
    """Get S3 client from environment variables."""
    aws_access_key_id = os.getenv("AWS_ACCESS_KEY_ID")
    aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY")
    aws_region = os.getenv("AWS_REGION", "ap-south-1")

    if aws_access_key_id and aws_secret_access_key:
        return boto3.client(
            "s3",
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            region_name=aws_region,
        )

    return boto3.client("s3", region_name=aws_region)


def get_s3_output_config():
    """Get S3 output configuration from environment variables."""
    bucket = os.getenv("S3_OUTPUT_BUCKET")

    if not bucket:
        raise ValueError("S3_OUTPUT_BUCKET environment variable is required")

    return bucket


def get_max_concurrent_jobs() -> int:
    """Get the maximum number of concurrent jobs from environment variable.

    Defaults to 2 if not set.
    """
    return int(os.getenv("MAX_CONCURRENT_JOBS"))


# Job queue lock to ensure thread-safe queue operations
_job_queue_lock = threading.Lock()

# Registry of job starter callbacks by job type
_job_starters: Dict[str, callable] = {}


def register_job_starter(job_type: str, starter_callback: callable) -> None:
    """Register a callback function for starting jobs of a specific type.

    Args:
        job_type: The job type (e.g., "stt-eval", "tts-eval")
        starter_callback: Function that takes a job dict and starts the job.
    """
    _job_starters[job_type] = starter_callback
    logger.info(f"Registered job starter for type: {job_type}")


def try_start_queued_job(job_types: List[str]) -> bool:
    """Try to start the next queued job if there's capacity.

    Args:
        job_types: List of job types to consider (e.g., ["stt-eval", "tts-eval"])

    Returns:
        True if a job was started, False otherwise.
    """
    # Import here to avoid circular imports
    from db import count_running_jobs, get_queued_jobs, update_job

    with _job_queue_lock:
        max_jobs = get_max_concurrent_jobs()
        running_count = count_running_jobs(job_types)

        logger.info(f"Job queue check: {running_count}/{max_jobs} jobs running")

        if running_count >= max_jobs:
            logger.info("Max concurrent jobs reached, not starting new job")
            return False

        # Get the oldest queued job
        queued_jobs = get_queued_jobs(job_types)
        if not queued_jobs:
            logger.info("No queued jobs to start")
            return False

        job = queued_jobs[0]
        job_id = job["uuid"]
        job_type = job.get("type")

        # Find the appropriate starter callback
        starter_callback = _job_starters.get(job_type)
        if not starter_callback:
            logger.error(f"No job starter registered for type: {job_type}")
            return False

        # Update status to in_progress before starting
        update_job(job_id, status=TaskStatus.IN_PROGRESS.value)
        logger.info(f"Starting queued job {job_id} of type {job_type}")

        try:
            # Start the job (this should spawn a thread)
            starter_callback(job)
            return True
        except Exception as e:
            # If starting fails, mark as done with error
            logger.error(f"Failed to start job {job_id}: {e}")
            update_job(
                job_id,
                status=TaskStatus.DONE.value,
                results={"error": f"Failed to start job: {str(e)}"},
            )
            return False


def can_start_job(job_types: List[str]) -> bool:
    """Check if there's capacity to start a new job immediately.

    Args:
        job_types: List of job types to consider for counting running jobs.

    Returns:
        True if a new job can be started, False otherwise.
    """
    from db import count_running_jobs

    with _job_queue_lock:
        max_jobs = get_max_concurrent_jobs()
        running_count = count_running_jobs(job_types)
        return running_count < max_jobs


# ============ Agent Test Job Queue Functions ============


def try_start_queued_agent_test_job(job_types: List[str]) -> bool:
    """Try to start the next queued agent test job if there's capacity.

    Args:
        job_types: List of job types to consider (e.g., ["llm-unit-test", "llm-benchmark"])

    Returns:
        True if a job was started, False otherwise.
    """
    from db import (
        count_running_agent_test_jobs,
        get_queued_agent_test_jobs,
        update_agent_test_job,
    )

    with _job_queue_lock:
        max_jobs = get_max_concurrent_jobs()
        running_count = count_running_agent_test_jobs(job_types)

        logger.info(
            f"Agent test job queue check: {running_count}/{max_jobs} jobs running"
        )

        if running_count >= max_jobs:
            logger.info("Max concurrent jobs reached, not starting new agent test job")
            return False

        # Get the oldest queued job
        queued_jobs = get_queued_agent_test_jobs(job_types)
        if not queued_jobs:
            logger.info("No queued agent test jobs to start")
            return False

        job = queued_jobs[0]
        job_id = job["uuid"]
        job_type = job.get("type")

        # Find the appropriate starter callback
        starter_callback = _job_starters.get(job_type)
        if not starter_callback:
            logger.error(f"No job starter registered for type: {job_type}")
            return False

        # Update status to in_progress before starting
        update_agent_test_job(job_id, status=TaskStatus.IN_PROGRESS.value)
        logger.info(f"Starting queued agent test job {job_id} of type {job_type}")

        try:
            # Start the job (this should spawn a thread)
            starter_callback(job)
            return True
        except Exception as e:
            # If starting fails, mark as done with error
            logger.error(f"Failed to start agent test job {job_id}: {e}")
            update_agent_test_job(
                job_id,
                status=TaskStatus.DONE.value,
                results={"error": f"Failed to start job: {str(e)}"},
            )
            return False


def can_start_agent_test_job(job_types: List[str]) -> bool:
    """Check if there's capacity to start a new agent test job immediately.

    Args:
        job_types: List of job types to consider for counting running jobs.

    Returns:
        True if a new job can be started, False otherwise.
    """
    from db import count_running_agent_test_jobs

    with _job_queue_lock:
        max_jobs = get_max_concurrent_jobs()
        running_count = count_running_agent_test_jobs(job_types)
        return running_count < max_jobs


# ============ Simulation Job Queue Functions ============


def try_start_queued_simulation_job(job_types: List[str]) -> bool:
    """Try to start the next queued simulation job if there's capacity.

    Args:
        job_types: List of job types to consider (e.g., ["text", "voice"])

    Returns:
        True if a job was started, False otherwise.
    """
    from db import (
        count_running_simulation_jobs,
        get_queued_simulation_jobs,
        update_simulation_job,
    )

    with _job_queue_lock:
        max_jobs = get_max_concurrent_jobs()
        running_count = count_running_simulation_jobs(job_types)

        logger.info(
            f"Simulation job queue check: {running_count}/{max_jobs} jobs running"
        )

        if running_count >= max_jobs:
            logger.info("Max concurrent jobs reached, not starting new simulation job")
            return False

        # Get the oldest queued job
        queued_jobs = get_queued_simulation_jobs(job_types)
        if not queued_jobs:
            logger.info("No queued simulation jobs to start")
            return False

        job = queued_jobs[0]
        job_id = job["uuid"]
        job_type = job.get("type")

        # Find the appropriate starter callback
        starter_callback = _job_starters.get(job_type)
        if not starter_callback:
            logger.error(f"No job starter registered for type: {job_type}")
            return False

        # Update status to in_progress before starting
        update_simulation_job(job_id, status=TaskStatus.IN_PROGRESS.value)
        logger.info(f"Starting queued simulation job {job_id} of type {job_type}")

        try:
            # Start the job (this should spawn a thread)
            starter_callback(job)
            return True
        except Exception as e:
            # If starting fails, mark as done with error
            logger.error(f"Failed to start simulation job {job_id}: {e}")
            update_simulation_job(
                job_id,
                status=TaskStatus.DONE.value,
                results={"error": f"Failed to start job: {str(e)}"},
            )
            return False


def can_start_simulation_job(job_types: List[str]) -> bool:
    """Check if there's capacity to start a new simulation job immediately.

    Args:
        job_types: List of job types to consider for counting running jobs.

    Returns:
        True if a new job can be started, False otherwise.
    """
    from db import count_running_simulation_jobs

    with _job_queue_lock:
        max_jobs = get_max_concurrent_jobs()
        running_count = count_running_simulation_jobs(job_types)
        return running_count < max_jobs
