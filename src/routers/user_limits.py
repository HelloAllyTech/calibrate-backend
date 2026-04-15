import os
import sqlite3

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field

from db import (
    create_user_limits,
    get_user,
    get_user_limits,
    update_user_limits,
    delete_user_limits,
)
from auth_utils import get_current_user_id, require_superadmin

router = APIRouter(prefix="/user-limits", tags=["user-limits"])

DEFAULT_MAX_ROWS_PER_EVAL = int(os.getenv("DEFAULT_MAX_ROWS_PER_EVAL", "20"))


class UserLimits(BaseModel):
    max_rows_per_eval: int = Field(gt=0, le=10000)


class UserLimitsCreate(BaseModel):
    user_id: str
    limits: UserLimits


class UserLimitsUpdate(BaseModel):
    limits: UserLimits


class UserLimitsResponse(BaseModel):
    uuid: str
    user_id: str
    limits: UserLimits
    created_at: str
    updated_at: str


class UserLimitsCreateResponse(BaseModel):
    uuid: str
    message: str


@router.get("/me/max-rows-per-eval")
async def get_max_rows_per_eval(user_id: str = Depends(get_current_user_id)):
    """Get the max rows per eval for the authenticated user.

    Returns the user-specific value from user_limits if set,
    otherwise falls back to DEFAULT_MAX_ROWS_PER_EVAL.
    """
    limits = get_user_limits(user_id)
    if limits and "max_rows_per_eval" in limits.get("limits", {}):
        return {"max_rows_per_eval": limits["limits"]["max_rows_per_eval"]}
    return {"max_rows_per_eval": DEFAULT_MAX_ROWS_PER_EVAL}


@router.post("", response_model=UserLimitsCreateResponse)
async def create_user_limits_endpoint(
    data: UserLimitsCreate, user_id: str = Depends(require_superadmin)
):
    """Create limits for a user."""
    if not get_user(data.user_id):
        raise HTTPException(status_code=404, detail="User not found")
    existing = get_user_limits(data.user_id)
    if existing:
        raise HTTPException(
            status_code=409,
            detail="Limits already exist for this user. Use PUT to update.",
        )
    try:
        row_uuid = create_user_limits(user_id=data.user_id, limits=data.limits)
    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=409,
            detail="Limits already exist for this user. Use PUT to update.",
        )
    return UserLimitsCreateResponse(
        uuid=row_uuid, message="User limits created successfully"
    )


@router.get("/{target_user_id}", response_model=UserLimitsResponse)
async def get_user_limits_endpoint(
    target_user_id: str, user_id: str = Depends(get_current_user_id)
):
    """Get limits for a user."""
    limits = get_user_limits(target_user_id)
    if not limits:
        raise HTTPException(status_code=404, detail="User limits not found")
    return limits


@router.put("/{target_user_id}", response_model=UserLimitsResponse)
async def update_user_limits_endpoint(
    target_user_id: str,
    data: UserLimitsUpdate,
    user_id: str = Depends(require_superadmin),
):
    """Update limits for a user."""
    updated = update_user_limits(user_id=target_user_id, limits=data.limits)
    if not updated:
        raise HTTPException(status_code=404, detail="User limits not found")
    return updated


@router.delete("/{target_user_id}")
async def delete_user_limits_endpoint(
    target_user_id: str, user_id: str = Depends(require_superadmin)
):
    """Delete limits for a user."""
    deleted = delete_user_limits(target_user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="User limits not found")
    return {"message": "User limits deleted successfully"}
