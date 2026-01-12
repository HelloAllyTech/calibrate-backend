from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import (
    create_simulation,
    get_simulation,
    get_all_simulations,
    update_simulation,
    delete_simulation,
    get_persona,
    get_scenario,
    get_metric,
    add_persona_to_simulation,
    add_scenario_to_simulation,
    add_metric_to_simulation,
    get_personas_for_simulation,
    get_scenarios_for_simulation,
    get_metrics_for_simulation,
)


router = APIRouter(prefix="/simulations", tags=["simulations"])


class SimulationCreate(BaseModel):
    name: str
    persona_uuids: Optional[List[str]] = None
    scenario_uuids: Optional[List[str]] = None
    metric_uuids: Optional[List[str]] = None


class SimulationUpdate(BaseModel):
    name: Optional[str] = None


class PersonaResponse(BaseModel):
    uuid: str
    name: str
    description: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    created_at: str
    updated_at: str


class ScenarioResponse(BaseModel):
    uuid: str
    name: str
    description: Optional[str] = None
    created_at: str
    updated_at: str


class MetricResponse(BaseModel):
    uuid: str
    name: str
    description: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    created_at: str
    updated_at: str


class SimulationListResponse(BaseModel):
    uuid: str
    name: str
    created_at: str
    updated_at: str


class SimulationDetailResponse(BaseModel):
    uuid: str
    name: str
    created_at: str
    updated_at: str
    personas: List[PersonaResponse]
    scenarios: List[ScenarioResponse]
    metrics: List[MetricResponse]


class SimulationCreateResponse(BaseModel):
    uuid: str
    message: str


@router.post("", response_model=SimulationCreateResponse)
async def create_simulation_endpoint(simulation: SimulationCreate):
    """Create a new simulation with optional linked personas, scenarios, and metrics."""
    # Verify all personas exist
    if simulation.persona_uuids:
        for persona_uuid in simulation.persona_uuids:
            persona = get_persona(persona_uuid)
            if not persona:
                raise HTTPException(
                    status_code=404, detail=f"Persona {persona_uuid} not found"
                )

    # Verify all scenarios exist
    if simulation.scenario_uuids:
        for scenario_uuid in simulation.scenario_uuids:
            scenario = get_scenario(scenario_uuid)
            if not scenario:
                raise HTTPException(
                    status_code=404, detail=f"Scenario {scenario_uuid} not found"
                )

    # Verify all metrics exist
    if simulation.metric_uuids:
        for metric_uuid in simulation.metric_uuids:
            metric = get_metric(metric_uuid)
            if not metric:
                raise HTTPException(
                    status_code=404, detail=f"Metric {metric_uuid} not found"
                )

    # Create the simulation
    simulation_uuid = create_simulation(name=simulation.name)

    # Add personas to simulation
    if simulation.persona_uuids:
        for persona_uuid in simulation.persona_uuids:
            add_persona_to_simulation(simulation_uuid, persona_uuid)

    # Add scenarios to simulation
    if simulation.scenario_uuids:
        for scenario_uuid in simulation.scenario_uuids:
            add_scenario_to_simulation(simulation_uuid, scenario_uuid)

    # Add metrics to simulation
    if simulation.metric_uuids:
        for metric_uuid in simulation.metric_uuids:
            add_metric_to_simulation(simulation_uuid, metric_uuid)

    return SimulationCreateResponse(
        uuid=simulation_uuid, message="Simulation created successfully"
    )


@router.get("", response_model=List[SimulationListResponse])
async def list_simulations():
    """List all simulations."""
    simulations = get_all_simulations()
    return simulations


@router.get("/{simulation_uuid}", response_model=SimulationDetailResponse)
async def get_simulation_endpoint(simulation_uuid: str):
    """Get a simulation by UUID with all linked personas, scenarios, and metrics."""
    simulation = get_simulation(simulation_uuid)
    if not simulation:
        raise HTTPException(status_code=404, detail="Simulation not found")

    # Get linked entities
    personas = get_personas_for_simulation(simulation_uuid)
    scenarios = get_scenarios_for_simulation(simulation_uuid)
    metrics = get_metrics_for_simulation(simulation_uuid)

    return SimulationDetailResponse(
        uuid=simulation["uuid"],
        name=simulation["name"],
        created_at=simulation["created_at"],
        updated_at=simulation["updated_at"],
        personas=personas,
        scenarios=scenarios,
        metrics=metrics,
    )


@router.put("/{simulation_uuid}", response_model=SimulationDetailResponse)
async def update_simulation_endpoint(
    simulation_uuid: str, simulation: SimulationUpdate
):
    """Update a simulation."""
    existing_simulation = get_simulation(simulation_uuid)
    if not existing_simulation:
        raise HTTPException(status_code=404, detail="Simulation not found")

    updated = update_simulation(
        simulation_uuid=simulation_uuid,
        name=simulation.name,
    )

    if not updated:
        raise HTTPException(status_code=400, detail="No fields to update")

    # Return full detail response
    updated_simulation = get_simulation(simulation_uuid)
    personas = get_personas_for_simulation(simulation_uuid)
    scenarios = get_scenarios_for_simulation(simulation_uuid)
    metrics = get_metrics_for_simulation(simulation_uuid)

    return SimulationDetailResponse(
        uuid=updated_simulation["uuid"],
        name=updated_simulation["name"],
        created_at=updated_simulation["created_at"],
        updated_at=updated_simulation["updated_at"],
        personas=personas,
        scenarios=scenarios,
        metrics=metrics,
    )


@router.delete("/{simulation_uuid}")
async def delete_simulation_endpoint(simulation_uuid: str):
    """Delete a simulation."""
    deleted = delete_simulation(simulation_uuid)
    if not deleted:
        raise HTTPException(status_code=404, detail="Simulation not found")
    return {"message": "Simulation deleted successfully"}
