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
    """Find an available port starting from start_port."""
    port = start_port
    while True:
        logger.debug(f"Checking port {port}")
        if is_port_in_use(port):
            port += 1
            if port > 65535:
                raise RuntimeError("No available ports found")
            continue

        return port


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
