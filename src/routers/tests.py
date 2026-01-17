from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from db import create_test, get_test, get_all_tests, update_test, delete_test
from auth_utils import get_current_user_id


router = APIRouter(prefix="/tests", tags=["tests"])


class TestCreate(BaseModel):
    name: str
    type: str
    config: Optional[Dict[str, Any]] = None


class TestUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    config: Optional[Dict[str, Any]] = None


class TestResponse(BaseModel):
    uuid: str
    name: str
    type: str
    config: Optional[Dict[str, Any]] = None
    created_at: str
    updated_at: str


class TestCreateResponse(BaseModel):
    uuid: str
    message: str


@router.post("", response_model=TestCreateResponse)
async def create_test_endpoint(
    test: TestCreate, user_id: str = Depends(get_current_user_id)
):
    """Create a new test."""
    test_uuid = create_test(
        name=test.name,
        type=test.type,
        config=test.config,
        user_id=user_id,
    )
    return TestCreateResponse(uuid=test_uuid, message="Test created successfully")


@router.get("", response_model=List[TestResponse])
async def list_tests(user_id: str = Depends(get_current_user_id)):
    """List all tests for the authenticated user."""
    tests = get_all_tests(user_id=user_id)
    return tests


@router.get("/{test_uuid}", response_model=TestResponse)
async def get_test_endpoint(
    test_uuid: str, user_id: str = Depends(get_current_user_id)
):
    """Get a test by UUID."""
    test = get_test(test_uuid)
    if not test:
        raise HTTPException(status_code=404, detail="Test not found")
    # Verify user owns this test
    if test.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    return test


@router.put("/{test_uuid}", response_model=TestResponse)
async def update_test_endpoint(
    test_uuid: str, test: TestUpdate, user_id: str = Depends(get_current_user_id)
):
    """Update a test."""
    existing_test = get_test(test_uuid)
    if not existing_test:
        raise HTTPException(status_code=404, detail="Test not found")

    # Verify user owns this test
    if existing_test.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    updated = update_test(
        test_uuid=test_uuid,
        name=test.name,
        type=test.type,
        config=test.config,
    )

    if not updated:
        raise HTTPException(status_code=400, detail="No fields to update")

    updated_test = get_test(test_uuid)
    return updated_test


@router.delete("/{test_uuid}")
async def delete_test_endpoint(
    test_uuid: str, user_id: str = Depends(get_current_user_id)
):
    """Delete a test."""
    # Check if test exists and user owns it
    existing_test = get_test(test_uuid)
    if not existing_test:
        raise HTTPException(status_code=404, detail="Test not found")
    if existing_test.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    deleted = delete_test(test_uuid)
    if not deleted:
        raise HTTPException(status_code=404, detail="Test not found")
    return {"message": "Test deleted successfully"}
