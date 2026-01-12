from typing import Optional, List
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import (
    create_scenario,
    get_scenario,
    get_all_scenarios,
    update_scenario,
    delete_scenario,
)


router = APIRouter(prefix="/scenarios", tags=["scenarios"])


class ScenarioCreate(BaseModel):
    name: str
    description: Optional[str] = None


class ScenarioUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class ScenarioResponse(BaseModel):
    uuid: str
    name: str
    description: Optional[str] = None
    created_at: str
    updated_at: str


class ScenarioCreateResponse(BaseModel):
    uuid: str
    message: str


@router.post("", response_model=ScenarioCreateResponse)
async def create_scenario_endpoint(scenario: ScenarioCreate):
    """Create a new scenario."""
    scenario_uuid = create_scenario(
        name=scenario.name,
        description=scenario.description,
    )
    return ScenarioCreateResponse(uuid=scenario_uuid, message="Scenario created successfully")


@router.get("", response_model=List[ScenarioResponse])
async def list_scenarios():
    """List all scenarios."""
    scenarios = get_all_scenarios()
    return scenarios


@router.get("/{scenario_uuid}", response_model=ScenarioResponse)
async def get_scenario_endpoint(scenario_uuid: str):
    """Get a scenario by UUID."""
    scenario = get_scenario(scenario_uuid)
    if not scenario:
        raise HTTPException(status_code=404, detail="Scenario not found")
    return scenario


@router.put("/{scenario_uuid}", response_model=ScenarioResponse)
async def update_scenario_endpoint(scenario_uuid: str, scenario: ScenarioUpdate):
    """Update a scenario."""
    existing_scenario = get_scenario(scenario_uuid)
    if not existing_scenario:
        raise HTTPException(status_code=404, detail="Scenario not found")

    updated = update_scenario(
        scenario_uuid=scenario_uuid,
        name=scenario.name,
        description=scenario.description,
    )

    if not updated:
        raise HTTPException(status_code=400, detail="No fields to update")

    updated_scenario = get_scenario(scenario_uuid)
    return updated_scenario


@router.delete("/{scenario_uuid}")
async def delete_scenario_endpoint(scenario_uuid: str):
    """Delete a scenario."""
    deleted = delete_scenario(scenario_uuid)
    if not deleted:
        raise HTTPException(status_code=404, detail="Scenario not found")
    return {"message": "Scenario deleted successfully"}
