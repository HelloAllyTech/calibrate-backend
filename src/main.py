import os
import uuid
import logging
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware

from db import init_db
from routers.agents import router as agents_router
from routers.tools import router as tools_router
from routers.agent_tools import router as agent_tools_router
from routers.stt import router as stt_router
from routers.tts import router as tts_router
from routers.tests import router as tests_router
from utils import get_s3_client

load_dotenv()

# Set up logger
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

app = FastAPI()

# Initialize database
init_db()

# Include routers
app.include_router(agents_router)
app.include_router(tools_router)
app.include_router(agent_tools_router)
app.include_router(stt_router)
app.include_router(tts_router)
app.include_router(tests_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class PresignedURLRequest(BaseModel):
    task_type: Literal["stt", "tts", "agent"]
    content_type: str  # e.g., "audio/wav", "text/csv"
    extension: str  # e.g., "wav", "csv"


class PresignedURLResponse(BaseModel):
    presigned_url: str
    s3_path: str
    expires_in: int  # expiration time in seconds


@app.get("/")
def read_root():
    return {"message": "Health check successful!"}


@app.post("/presigned-url", response_model=PresignedURLResponse)
async def get_presigned_url(request: PresignedURLRequest):
    """
    Generate a presigned URL for uploading files to S3.

    The file will be stored at: bucket/task_type/media/UUID.extension

    Args:
        request: Contains task_type (stt, tts, agent) and file_extension

    Returns:
        Presigned URL, S3 path, and expiration time
    """
    # Validate file extension (remove leading dot if present)
    file_extension = request.extension
    if not file_extension:
        raise HTTPException(
            status_code=400,
            detail="File extension cannot be empty",
        )

    # Validate task type
    if request.task_type not in ["stt", "tts", "agent"]:
        raise HTTPException(
            status_code=400,
            detail="task_type must be one of: stt, tts, agent",
        )

    # Get S3 bucket from environment
    s3_bucket = os.getenv("S3_OUTPUT_BUCKET")
    if not s3_bucket:
        raise HTTPException(
            status_code=500,
            detail="S3_OUTPUT_BUCKET environment variable is required",
        )

    s3 = get_s3_client()

    # Generate UUID for unique file name
    file_uuid = str(uuid.uuid4())

    # Construct S3 key: task_type/media/UUID.extension (no prefix)
    s3_key = f"{request.task_type}/media/{file_uuid}.{file_extension}"

    # Generate presigned URL (expires in 1 hour)
    expiration = 3600  # 1 hour in seconds

    presigned_url = s3.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": s3_bucket,
            "Key": s3_key,
            "ContentType": request.content_type,
        },
        ExpiresIn=expiration,
    )

    return PresignedURLResponse(
        presigned_url=presigned_url,
        s3_path=f"s3://{s3_bucket}/{s3_key}",
        expires_in=expiration,
    )
