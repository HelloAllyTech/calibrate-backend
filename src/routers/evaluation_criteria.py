from typing import Optional, List
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import (
    create_evaluation_criteria,
    get_evaluation_criteria,
    get_evaluation_criteria_for_agent,
    update_evaluation_criteria,
    delete_evaluation_criteria,
)


router = APIRouter(prefix="/evaluation-criteria", tags=["evaluation-criteria"])


class EvaluationCriteriaCreate(BaseModel):
    name: str
    description: Optional[str] = None
    agent_id: str


class EvaluationCriteriaUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class EvaluationCriteriaResponse(BaseModel):
    uuid: str
    name: str
    description: Optional[str] = None
    agent_id: str
    created_at: str
    updated_at: str


class EvaluationCriteriaCreateResponse(BaseModel):
    uuid: str
    message: str


@router.post("", response_model=EvaluationCriteriaCreateResponse)
async def create_evaluation_criteria_endpoint(criteria: EvaluationCriteriaCreate):
    """Create a new evaluation criteria."""
    criteria_uuid = create_evaluation_criteria(
        name=criteria.name,
        description=criteria.description,
        agent_id=criteria.agent_id,
    )
    return EvaluationCriteriaCreateResponse(
        uuid=criteria_uuid, message="Evaluation criteria created successfully"
    )


@router.get("/agent/{agent_id}", response_model=List[EvaluationCriteriaResponse])
async def list_evaluation_criteria_for_agent(agent_id: str):
    """List all evaluation criteria for an agent."""
    criteria_list = get_evaluation_criteria_for_agent(agent_id)
    return criteria_list


@router.get("/{criteria_uuid}", response_model=EvaluationCriteriaResponse)
async def get_evaluation_criteria_endpoint(criteria_uuid: str):
    """Get an evaluation criteria by UUID."""
    criteria = get_evaluation_criteria(criteria_uuid)
    if not criteria:
        raise HTTPException(status_code=404, detail="Evaluation criteria not found")
    return criteria


@router.put("/{criteria_uuid}", response_model=EvaluationCriteriaResponse)
async def update_evaluation_criteria_endpoint(
    criteria_uuid: str, criteria: EvaluationCriteriaUpdate
):
    """Update an evaluation criteria."""
    existing_criteria = get_evaluation_criteria(criteria_uuid)
    if not existing_criteria:
        raise HTTPException(status_code=404, detail="Evaluation criteria not found")

    updated = update_evaluation_criteria(
        criteria_uuid=criteria_uuid,
        name=criteria.name,
        description=criteria.description,
    )

    if not updated:
        raise HTTPException(status_code=400, detail="No fields to update")

    updated_criteria = get_evaluation_criteria(criteria_uuid)
    return updated_criteria


@router.delete("/{criteria_uuid}")
async def delete_evaluation_criteria_endpoint(criteria_uuid: str):
    """Delete an evaluation criteria."""
    deleted = delete_evaluation_criteria(criteria_uuid)
    if not deleted:
        raise HTTPException(status_code=404, detail="Evaluation criteria not found")
    return {"message": "Evaluation criteria deleted successfully"}
