from typing import List, Optional, Any, Dict
from enum import Enum

from fastapi import APIRouter, Query
from pydantic import BaseModel

from db import get_all_jobs


router = APIRouter(prefix="/jobs", tags=["jobs"])


class JobType(str, Enum):
    STT = "stt"
    TTS = "tts"


class JobListItem(BaseModel):
    uuid: str
    type: str
    status: str
    details: Optional[Dict[str, Any]] = None
    results: Optional[Dict[str, Any]] = None
    created_at: str
    updated_at: str


class JobsListResponse(BaseModel):
    jobs: List[JobListItem]


# Map user-friendly job type to actual job type in database
JOB_TYPE_MAP = {
    JobType.STT: "stt-eval",
    JobType.TTS: "tts-eval",
}


@router.get("", response_model=JobsListResponse)
async def list_jobs(
    job_type: Optional[JobType] = Query(
        None, description="Filter jobs by type: 'stt' or 'tts'"
    ),
):
    """
    Get all jobs, optionally filtered by job type.

    Returns a list of all jobs with their UUID, type, status, details, results, and timestamps.
    Jobs are sorted by created_at descending (most recent first).
    """
    # Map the user-friendly job type to the actual database job type
    db_job_type = JOB_TYPE_MAP.get(job_type) if job_type else None

    jobs = get_all_jobs(job_type=db_job_type)

    job_items = [
        JobListItem(
            uuid=job["uuid"],
            type=job["type"],
            status=job["status"],
            details=job.get("details"),
            results=job.get("results"),
            created_at=job["created_at"],
            updated_at=job["updated_at"],
        )
        for job in jobs
    ]

    return JobsListResponse(jobs=job_items)
