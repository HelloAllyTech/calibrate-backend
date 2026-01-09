from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import create_test, get_test, get_all_tests, update_test, delete_test


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
async def create_test_endpoint(test: TestCreate):
    """Create a new test."""
    test_uuid = create_test(
        name=test.name,
        type=test.type,
        config=test.config,
    )
    return TestCreateResponse(uuid=test_uuid, message="Test created successfully")


@router.get("", response_model=List[TestResponse])
async def list_tests():
    """List all tests."""
    tests = get_all_tests()
    return tests


@router.get("/{test_uuid}", response_model=TestResponse)
async def get_test_endpoint(test_uuid: str):
    """Get a test by UUID."""
    test = get_test(test_uuid)
    if not test:
        raise HTTPException(status_code=404, detail="Test not found")
    return test


@router.put("/{test_uuid}", response_model=TestResponse)
async def update_test_endpoint(test_uuid: str, test: TestUpdate):
    """Update a test."""
    existing_test = get_test(test_uuid)
    if not existing_test:
        raise HTTPException(status_code=404, detail="Test not found")

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
async def delete_test_endpoint(test_uuid: str):
    """Delete a test."""
    deleted = delete_test(test_uuid)
    if not deleted:
        raise HTTPException(status_code=404, detail="Test not found")
    return {"message": "Test deleted successfully"}
