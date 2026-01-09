from typing import List, Dict, Any
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlite3 import IntegrityError

from db import (
    add_test_to_agent,
    remove_test_from_agent,
    get_tests_for_agent,
    get_agents_for_test,
    get_agent_test_link,
    get_all_agent_tests,
    get_agent,
    get_test,
)


router = APIRouter(prefix="/agent-tests", tags=["agent-tests"])


class AgentTestsCreate(BaseModel):
    agent_uuid: str
    test_uuids: List[str]


class AgentTestDelete(BaseModel):
    agent_uuid: str
    test_uuid: str


class AgentTestResponse(BaseModel):
    id: int
    agent_id: str
    test_id: str
    created_at: str


class AgentTestsCreateResponse(BaseModel):
    ids: List[int]
    message: str


class TestResponse(BaseModel):
    uuid: str
    name: str
    type: str
    config: Dict[str, Any] | None = None
    created_at: str
    updated_at: str


class AgentResponse(BaseModel):
    uuid: str
    name: str
    config: Dict[str, Any] | None = None
    created_at: str
    updated_at: str


@router.post("", response_model=AgentTestsCreateResponse)
async def create_agent_test_links(agent_tests: AgentTestsCreate):
    """Add tests to an agent."""
    # Verify agent exists
    agent = get_agent(agent_tests.agent_uuid)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Verify all tests exist
    for test_uuid in agent_tests.test_uuids:
        test = get_test(test_uuid)
        if not test:
            raise HTTPException(status_code=404, detail=f"Test {test_uuid} not found")

    link_ids = []
    for test_uuid in agent_tests.test_uuids:
        # Check if link already exists
        existing = get_agent_test_link(agent_tests.agent_uuid, test_uuid)
        if existing:
            continue  # Skip already linked tests

        try:
            link_id = add_test_to_agent(
                agent_id=agent_tests.agent_uuid,
                test_id=test_uuid,
            )
            link_ids.append(link_id)
        except IntegrityError:
            continue  # Skip if already linked

    return AgentTestsCreateResponse(
        ids=link_ids, message="Tests added to agent successfully"
    )


@router.get("", response_model=List[AgentTestResponse])
async def list_agent_tests():
    """List all agent-test links."""
    links = get_all_agent_tests()
    return links


@router.get("/agent/{agent_uuid}/tests", response_model=List[TestResponse])
async def get_agent_tests_endpoint(agent_uuid: str):
    """Get all tests for a specific agent."""
    # Verify agent exists
    agent = get_agent(agent_uuid)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    tests = get_tests_for_agent(agent_uuid)
    return tests


@router.get("/test/{test_uuid}/agents", response_model=List[AgentResponse])
async def get_test_agents(test_uuid: str):
    """Get all agents for a specific test."""
    # Verify test exists
    test = get_test(test_uuid)
    if not test:
        raise HTTPException(status_code=404, detail="Test not found")

    agents = get_agents_for_test(test_uuid)
    return agents


@router.delete("")
async def delete_agent_test_link(agent_test: AgentTestDelete):
    """Remove a test from an agent."""
    deleted = remove_test_from_agent(agent_test.agent_uuid, agent_test.test_uuid)
    if not deleted:
        raise HTTPException(status_code=404, detail="Agent-test link not found")
    return {"message": "Test removed from agent successfully"}
