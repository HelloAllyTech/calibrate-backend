import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from auth_utils import get_current_user_id
from utils import generate_presigned_download_url
from db import (
    create_dataset,
    get_dataset,
    get_all_datasets,
    get_dataset_item_counts,
    get_dataset_eval_counts,
    update_dataset_name,
    delete_dataset,
    add_dataset_items,
    get_dataset_items,
    update_dataset_item,
    delete_dataset_item,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/datasets", tags=["datasets"])


# ── Request / Response models ────────────────────────────────────────────────


class DatasetCreateRequest(BaseModel):
    name: str
    dataset_type: str  # 'stt' | 'tts'


class DatasetRenameRequest(BaseModel):
    name: str


class DatasetItemIn(BaseModel):
    audio_path: Optional[str] = None  # required for STT datasets
    text: str


class DatasetItemUpdate(BaseModel):
    audio_path: Optional[str] = None
    text: Optional[str] = None


class DatasetItemResponse(BaseModel):
    uuid: str
    audio_path: Optional[str]
    text: str
    order_index: int
    created_at: str
    updated_at: Optional[str] = None


class DatasetResponse(BaseModel):
    uuid: str
    name: str
    dataset_type: str
    item_count: int
    eval_count: int
    created_at: str
    updated_at: str


class DatasetDetailResponse(DatasetResponse):
    items: List[DatasetItemResponse]


# ── Helpers ──────────────────────────────────────────────────────────────────


def _validate_items_for_type(dataset_type: str, items: List[DatasetItemIn]) -> None:
    """Raise HTTPException if items are inconsistent with the dataset type."""
    for item in items:
        if dataset_type == "stt" and not item.audio_path:
            raise HTTPException(
                status_code=400,
                detail="STT dataset items must include audio_path",
            )
        if dataset_type == "tts" and item.audio_path:
            raise HTTPException(
                status_code=400,
                detail="TTS dataset items must not include audio_path",
            )


def _presign_audio_path(audio_path: Optional[str]) -> Optional[str]:
    """Convert an s3://bucket/key path to a presigned download URL."""
    if not audio_path:
        return audio_path
    if audio_path.startswith("s3://"):
        parts = audio_path[5:].split("/", 1)
        bucket = parts[0]
        key = parts[1] if len(parts) > 1 else ""
        return generate_presigned_download_url(key, bucket=bucket) or audio_path
    if audio_path.startswith("http"):
        return audio_path
    return generate_presigned_download_url(audio_path) or audio_path


def _item_row_to_response(row: dict) -> DatasetItemResponse:
    return DatasetItemResponse(
        uuid=row["uuid"],
        audio_path=_presign_audio_path(row.get("audio_path")),
        text=row["text"],
        order_index=row["order_index"],
        created_at=row["created_at"],
        updated_at=row.get("updated_at"),
    )


def _dataset_row_to_response(row: dict, item_count: int, eval_count: int = 0) -> DatasetResponse:
    return DatasetResponse(
        uuid=row["uuid"],
        name=row["name"],
        dataset_type=row["type"],
        item_count=item_count,
        eval_count=eval_count,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ── Routes ───────────────────────────────────────────────────────────────────


@router.post("", response_model=DatasetResponse, status_code=201)
async def create_new_dataset(
    request: DatasetCreateRequest,
    user_id: str = Depends(get_current_user_id),
):
    """Create a new empty dataset."""
    if request.dataset_type not in ("stt", "tts"):
        raise HTTPException(status_code=400, detail="dataset_type must be 'stt' or 'tts'")

    dataset_uuid = create_dataset(name=request.name, dataset_type=request.dataset_type, user_id=user_id)
    row = get_dataset(dataset_uuid, user_id=user_id)
    return _dataset_row_to_response(row, item_count=0, eval_count=0)


@router.get("", response_model=List[DatasetResponse])
async def list_datasets(
    dataset_type: Optional[str] = None,
    user_id: str = Depends(get_current_user_id),
):
    """List all datasets for the current user, optionally filtered by type."""
    if dataset_type and dataset_type not in ("stt", "tts"):
        raise HTTPException(status_code=400, detail="dataset_type must be 'stt' or 'tts'")

    rows = get_all_datasets(user_id=user_id, dataset_type=dataset_type)
    uuids = [row["uuid"] for row in rows]
    counts = get_dataset_item_counts(uuids)
    eval_counts = get_dataset_eval_counts(uuids)
    return [
        _dataset_row_to_response(
            row,
            item_count=counts.get(row["uuid"], 0),
            eval_count=eval_counts.get(row["uuid"], 0),
        )
        for row in rows
    ]


@router.get("/{dataset_id}", response_model=DatasetDetailResponse)
async def get_dataset_detail(
    dataset_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Get a dataset with all its items."""
    row = get_dataset(dataset_id, user_id=user_id)
    if not row:
        raise HTTPException(status_code=404, detail="Dataset not found")

    items = get_dataset_items(dataset_id)
    eval_counts = get_dataset_eval_counts([dataset_id])
    return DatasetDetailResponse(
        uuid=row["uuid"],
        name=row["name"],
        dataset_type=row["type"],
        item_count=len(items),
        eval_count=eval_counts.get(dataset_id, 0),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        items=[_item_row_to_response(i) for i in items],
    )


@router.patch("/{dataset_id}", response_model=DatasetResponse)
async def rename_dataset(
    dataset_id: str,
    request: DatasetRenameRequest,
    user_id: str = Depends(get_current_user_id),
):
    """Rename a dataset."""
    row = get_dataset(dataset_id, user_id=user_id)
    if not row:
        raise HTTPException(status_code=404, detail="Dataset not found")

    update_dataset_name(dataset_id, user_id=user_id, name=request.name)
    row = get_dataset(dataset_id, user_id=user_id)
    counts = get_dataset_item_counts([dataset_id])
    eval_counts = get_dataset_eval_counts([dataset_id])
    return _dataset_row_to_response(
        row,
        item_count=counts.get(dataset_id, 0),
        eval_count=eval_counts.get(dataset_id, 0),
    )


@router.delete("/{dataset_id}", status_code=204)
async def remove_dataset(
    dataset_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Soft delete a dataset and all its items."""
    row = get_dataset(dataset_id, user_id=user_id)
    if not row:
        raise HTTPException(status_code=404, detail="Dataset not found")

    delete_dataset(dataset_id, user_id=user_id)


@router.post("/{dataset_id}/items", response_model=List[DatasetItemResponse], status_code=201)
async def add_items(
    dataset_id: str,
    items: List[DatasetItemIn],
    user_id: str = Depends(get_current_user_id),
):
    """Add one or more items to a dataset."""
    if not items:
        raise HTTPException(status_code=400, detail="items list cannot be empty")

    row = get_dataset(dataset_id, user_id=user_id)
    if not row:
        raise HTTPException(status_code=404, detail="Dataset not found")

    _validate_items_for_type(row["type"], items)

    item_dicts = [{"audio_path": i.audio_path, "text": i.text} for i in items]
    new_uuids = add_dataset_items(dataset_id, item_dicts)

    all_items = get_dataset_items(dataset_id)
    new_uuid_set = set(new_uuids)
    return [_item_row_to_response(i) for i in all_items if i["uuid"] in new_uuid_set]


@router.patch("/{dataset_id}/items/{item_uuid}", response_model=DatasetItemResponse)
async def update_item(
    dataset_id: str,
    item_uuid: str,
    request: DatasetItemUpdate,
    user_id: str = Depends(get_current_user_id),
):
    """Update a dataset item's text or audio_path."""
    row = get_dataset(dataset_id, user_id=user_id)
    if not row:
        raise HTTPException(status_code=404, detail="Dataset not found")

    if request.text is None and request.audio_path is None:
        raise HTTPException(status_code=400, detail="Nothing to update")

    updated = update_dataset_item(
        item_uuid,
        dataset_id,
        text=request.text,
        audio_path=request.audio_path if request.audio_path is not None else ...,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Item not found")

    items = get_dataset_items(dataset_id)
    for item in items:
        if item["uuid"] == item_uuid:
            return _item_row_to_response(item)

    raise HTTPException(status_code=404, detail="Item not found")


@router.delete("/{dataset_id}/items/{item_uuid}", status_code=204)
async def remove_item(
    dataset_id: str,
    item_uuid: str,
    user_id: str = Depends(get_current_user_id),
):
    """Soft delete a single item from a dataset."""
    row = get_dataset(dataset_id, user_id=user_id)
    if not row:
        raise HTTPException(status_code=404, detail="Dataset not found")

    deleted = delete_dataset_item(item_uuid, dataset_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Item not found")
