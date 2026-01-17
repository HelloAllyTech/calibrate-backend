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
