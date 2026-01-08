from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import create_agent, get_agent, get_all_agents, update_agent, delete_agent


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
