import copy
import logging
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import (
    create_agent,
    get_agent,
    get_all_agents,
    update_agent,
    delete_agent,
    get_tools_for_agent,
    get_tests_for_agent,
    add_tool_to_agent,
    add_test_to_agent,
)

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/agents", tags=["agents"])


class AgentCreate(BaseModel):
    name: str
    config: Optional[Dict[str, Any]] = None


class AgentUpdate(BaseModel):
    name: Optional[str] = None
    config: Optional[Dict[str, Any]] = None


class AgentResponse(BaseModel):
    uuid: str
    name: str
    config: Optional[Dict[str, Any]] = None
    created_at: str
    updated_at: str


class AgentCreateResponse(BaseModel):
    uuid: str
    message: str


class AgentDuplicateRequest(BaseModel):
    name: str


class AgentDuplicateResponse(BaseModel):
    uuid: str
    message: str


@router.post("", response_model=AgentCreateResponse)
async def create_agent_endpoint(agent: AgentCreate):
    """Create a new agent."""
    agent_uuid = create_agent(
        name=agent.name,
        config=agent.config,
    )
    return AgentCreateResponse(uuid=agent_uuid, message="Agent created successfully")


@router.get("", response_model=List[AgentResponse])
async def list_agents():
    """List all agents."""
    agents = get_all_agents()
    return agents


@router.get("/{agent_uuid}", response_model=AgentResponse)
async def get_agent_endpoint(agent_uuid: str):
    """Get an agent by UUID."""
    agent = get_agent(agent_uuid)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@router.put("/{agent_uuid}", response_model=AgentResponse)
async def update_agent_endpoint(agent_uuid: str, agent: AgentUpdate):
    """Update an agent."""
    # Check if agent exists
    existing_agent = get_agent(agent_uuid)
    if not existing_agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Update only provided fields
    updated = update_agent(
        agent_uuid=agent_uuid,
        name=agent.name,
        config=agent.config,
    )

    if not updated:
        raise HTTPException(status_code=400, detail="No fields to update")

    # Return updated agent
    updated_agent = get_agent(agent_uuid)
    return updated_agent


@router.delete("/{agent_uuid}")
async def delete_agent_endpoint(agent_uuid: str):
    """Delete an agent."""
    deleted = delete_agent(agent_uuid)
    if not deleted:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"message": "Agent deleted successfully"}


@router.post("/{agent_uuid}/duplicate", response_model=AgentDuplicateResponse)
async def duplicate_agent_endpoint(agent_uuid: str, request: AgentDuplicateRequest):
    """
    Duplicate an agent with all its linked data.
    
    This will:
    - Copy the agent (with the provided name, config including speaks_first, data extraction fields, etc.)
    - Copy all linked tools
    - Copy all linked tests
    - Return the new agent UUID
    """
    # Get the original agent
    original_agent = get_agent(agent_uuid)
    if not original_agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Use the provided name
    new_name = request.name
    
    # Copy the entire config (includes speaks_first, data extraction fields, llm config, etc.)
    new_config = original_agent.get("config")
    if new_config:
        # Deep copy the config to avoid reference issues
        new_config = copy.deepcopy(new_config)

    # Create the new agent
    new_agent_uuid = create_agent(
        name=new_name,
        config=new_config,
    )

    # Copy all linked tools
    linked_tools = get_tools_for_agent(agent_uuid)
    for tool in linked_tools:
        try:
            add_tool_to_agent(new_agent_uuid, tool["uuid"])
        except Exception as e:
            # Log but continue - don't fail the entire duplication
            logger.warning(f"Failed to link tool {tool['uuid']} to duplicated agent: {e}")

    # Copy all linked tests
    linked_tests = get_tests_for_agent(agent_uuid)
    for test in linked_tests:
        try:
            add_test_to_agent(new_agent_uuid, test["uuid"])
        except Exception as e:
            # Log but continue - don't fail the entire duplication
            logger.warning(f"Failed to link test {test['uuid']} to duplicated agent: {e}")

    return AgentDuplicateResponse(
        uuid=new_agent_uuid,
        message="Agent duplicated successfully with all linked tools and tests",
    )
