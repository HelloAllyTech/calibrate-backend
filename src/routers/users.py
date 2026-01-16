import logging
from typing import List
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import get_user, get_all_users

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


class UserResponse(BaseModel):
    """User response model."""

    uuid: str
    first_name: str
    last_name: str
    email: str
    created_at: str
    updated_at: str


@router.get("", response_model=List[UserResponse])
async def list_users():
    """List all users."""
    users = get_all_users()
    return [
        UserResponse(
            uuid=user["uuid"],
            first_name=user["first_name"],
            last_name=user["last_name"],
            email=user["email"],
            created_at=user["created_at"],
            updated_at=user["updated_at"],
        )
        for user in users
    ]


@router.get("/{user_uuid}", response_model=UserResponse)
async def get_user_endpoint(user_uuid: str):
    """
    Get user information by UUID.

    Args:
        user_uuid: The user's UUID

    Returns:
        User information
    """
    user = get_user(user_uuid)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return UserResponse(
        uuid=user["uuid"],
        first_name=user["first_name"],
        last_name=user["last_name"],
        email=user["email"],
        created_at=user["created_at"],
        updated_at=user["updated_at"],
    )
