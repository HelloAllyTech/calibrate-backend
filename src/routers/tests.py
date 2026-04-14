from typing import ClassVar, Optional, List, Dict, Any, Literal
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, model_validator

from db import create_test, get_test, get_all_tests, update_test, delete_test, bulk_create_tests, get_agent, add_test_to_agent
from auth_utils import get_current_user_id

import logging

logger = logging.getLogger(__name__)


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


# --- Bulk upload models ---

class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None


class ExpectedToolCall(BaseModel):
    tool: str
    arguments: Optional[Dict[str, Any]] = None
    accept_any_arguments: bool = False


class BulkTestItem(BaseModel):
    name: str
    conversation_history: List[ChatMessage]
    criteria: Optional[str] = None
    tool_calls: Optional[List[ExpectedToolCall]] = None


class BulkTestUpload(BaseModel):
    type: Literal["response", "tool_call"]
    tests: List[BulkTestItem]
    agent_uuids: Optional[List[str]] = None
    language: Optional[str] = None

    MAX_BATCH_SIZE: ClassVar[int] = 500

    @model_validator(mode="after")
    def validate_tests(self):
        if not self.tests:
            raise ValueError("tests list must not be empty")

        if len(self.tests) > self.MAX_BATCH_SIZE:
            raise ValueError(f"Batch size {len(self.tests)} exceeds maximum of {self.MAX_BATCH_SIZE}")

        names = [t.name for t in self.tests]
        if len(names) != len(set(names)):
            seen = set()
            dupes = sorted({n for n in names if n in seen or seen.add(n)})
            raise ValueError(f"Duplicate test names in request: {', '.join(dupes)}")

        for t in self.tests:
            if not t.conversation_history:
                raise ValueError(f"Test '{t.name}' must have at least one message in conversation_history")
            if self.type == "response":
                if not t.criteria:
                    raise ValueError(f"Test '{t.name}' must have 'criteria' for response type")
            elif self.type == "tool_call":
                if not t.tool_calls:
                    raise ValueError(f"Test '{t.name}' must have 'tool_calls' for tool_call type")

        return self


class BulkTestUploadResponse(BaseModel):
    uuids: List[str]
    count: int
    message: str
    warnings: Optional[List[str]] = None


@router.post("/bulk", response_model=BulkTestUploadResponse)
async def bulk_upload_tests(
    payload: BulkTestUpload, user_id: str = Depends(get_current_user_id)
):
    """Bulk upload LLM tests. All tests must be the same type (response or tool_call)."""
    if payload.agent_uuids:
        for agent_uuid in payload.agent_uuids:
            agent = get_agent(agent_uuid)
            if not agent:
                raise HTTPException(status_code=404, detail=f"Agent {agent_uuid} not found")
            if agent.get("user_id") != user_id:
                raise HTTPException(status_code=403, detail=f"Access denied for agent {agent_uuid}")

    db_tests = []
    for t in payload.tests:
        evaluation: Dict[str, Any] = {"type": payload.type}
        if payload.type == "response":
            evaluation["criteria"] = t.criteria
        else:
            evaluation["tool_calls"] = [tc.model_dump() for tc in t.tool_calls]

        config: Dict[str, Any] = {
            "history": [msg.model_dump(exclude_none=True) for msg in t.conversation_history],
            "evaluation": evaluation,
        }
        if payload.language:
            config["settings"] = {"language": payload.language}

        db_tests.append({
            "name": t.name,
            "type": payload.type,
            "config": config,
        })

    try:
        uuids = bulk_create_tests(tests=db_tests, user_id=user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    warnings: List[str] = []
    if payload.agent_uuids:
        linked_agents = set()
        for agent_uuid in payload.agent_uuids:
            agent_failed = False
            for test_uuid in uuids:
                try:
                    add_test_to_agent(agent_uuid, test_uuid)
                    linked_agents.add(agent_uuid)
                except Exception as e:
                    agent_failed = True
                    logger.warning(f"Failed to link test {test_uuid} to agent {agent_uuid}: {e}")
            if agent_failed:
                warnings.append(f"Some tests could not be linked to agent {agent_uuid}")

    message = f"Successfully created {len(uuids)} tests"
    if payload.agent_uuids:
        message += f" and linked to {len(linked_agents)} agent(s)"

    return BulkTestUploadResponse(
        uuids=uuids,
        count=len(uuids),
        message=message,
        warnings=warnings or None,
    )


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
