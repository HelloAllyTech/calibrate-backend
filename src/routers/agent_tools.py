from typing import List, Dict, Any, Literal
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlite3 import IntegrityError

from db import (
    add_tool_to_agent,
    remove_tool_from_agent,
    get_tools_for_agent,
    get_agents_for_tool,
    get_agent_tool_link,
    get_all_agent_tools,
    get_agent,
    get_tool,
)


router = APIRouter(prefix="/agent-tools", tags=["agent-tools"])


class AgentToolsCreate(BaseModel):
    agent_uuid: str
    tool_uuids: List[str]


class AgentToolDelete(BaseModel):
    agent_uuid: str
    tool_uuid: str


class AgentToolResponse(BaseModel):
    id: int
    agent_id: str
    tool_id: str
    created_at: str


class AgentToolsCreateResponse(BaseModel):
    ids: List[int]
    message: str


class ToolResponse(BaseModel):
    uuid: str
    name: str
    description: str
    config: Dict[str, Any] | None = None
    created_at: str
    updated_at: str


class AgentResponse(BaseModel):
    uuid: str
    name: str
    type: Literal["agent", "connection"]
    config: Dict[str, Any] | None = None
    created_at: str
    updated_at: str


@router.post("", response_model=AgentToolsCreateResponse)
async def create_agent_tool_links(agent_tools: AgentToolsCreate):
    """Add tools to an agent."""
    # Verify agent exists
    agent = get_agent(agent_tools.agent_uuid)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Verify all tools exist
    for tool_uuid in agent_tools.tool_uuids:
        tool = get_tool(tool_uuid)
        if not tool:
            raise HTTPException(status_code=404, detail=f"Tool {tool_uuid} not found")

    link_ids = []
    for tool_uuid in agent_tools.tool_uuids:
        # Check if link already exists
        existing = get_agent_tool_link(agent_tools.agent_uuid, tool_uuid)
        if existing:
            continue  # Skip already linked tools

        try:
            link_id = add_tool_to_agent(
                agent_id=agent_tools.agent_uuid,
                tool_id=tool_uuid,
            )
            link_ids.append(link_id)
        except IntegrityError:
            continue  # Skip if already linked

    return AgentToolsCreateResponse(
        ids=link_ids, message="Tools added to agent successfully"
    )


@router.get("", response_model=List[AgentToolResponse])
async def list_agent_tools():
    """List all agent-tool links."""
    links = get_all_agent_tools()
    return links


@router.get("/agent/{agent_uuid}/tools", response_model=List[ToolResponse])
async def get_agent_tools(agent_uuid: str):
    """Get all tools for a specific agent."""
    # Verify agent exists
    agent = get_agent(agent_uuid)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    tools = get_tools_for_agent(agent_uuid)
    return tools


@router.get("/tool/{tool_uuid}/agents", response_model=List[AgentResponse])
async def get_tool_agents(tool_uuid: str):
    """Get all agents for a specific tool."""
    # Verify tool exists
    tool = get_tool(tool_uuid)
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    agents = get_agents_for_tool(tool_uuid)
    return agents


@router.delete("")
async def delete_agent_tool_link(agent_tool: AgentToolDelete):
    """Remove a tool from an agent."""
    deleted = remove_tool_from_agent(agent_tool.agent_uuid, agent_tool.tool_uuid)
    if not deleted:
        raise HTTPException(status_code=404, detail="Agent-tool link not found")
    return {"message": "Tool removed from agent successfully"}
