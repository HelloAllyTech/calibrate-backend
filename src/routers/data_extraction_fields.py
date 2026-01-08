from typing import Optional, List
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import (
    create_data_extraction_field,
    get_data_extraction_field,
    get_data_extraction_fields_for_agent,
    update_data_extraction_field,
    delete_data_extraction_field,
)


router = APIRouter(prefix="/data-extraction-fields", tags=["data-extraction-fields"])


class DataExtractionFieldCreate(BaseModel):
    type: str
    name: str
    description: Optional[str] = None
    agent_id: str


class DataExtractionFieldUpdate(BaseModel):
    type: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None


class DataExtractionFieldResponse(BaseModel):
    uuid: str
    type: str
    name: str
    description: Optional[str] = None
    agent_id: str
    created_at: str
    updated_at: str


class DataExtractionFieldCreateResponse(BaseModel):
    uuid: str
    message: str


@router.post("", response_model=DataExtractionFieldCreateResponse)
async def create_data_extraction_field_endpoint(field: DataExtractionFieldCreate):
    """Create a new data extraction field."""
    field_uuid = create_data_extraction_field(
        type=field.type,
        name=field.name,
        description=field.description,
        agent_id=field.agent_id,
    )
    return DataExtractionFieldCreateResponse(
        uuid=field_uuid, message="Data extraction field created successfully"
    )


@router.get("/agent/{agent_id}", response_model=List[DataExtractionFieldResponse])
async def list_data_extraction_fields_for_agent(agent_id: str):
    """List all data extraction fields for an agent."""
    fields = get_data_extraction_fields_for_agent(agent_id)
    return fields


@router.get("/{field_uuid}", response_model=DataExtractionFieldResponse)
async def get_data_extraction_field_endpoint(field_uuid: str):
    """Get a data extraction field by UUID."""
    field = get_data_extraction_field(field_uuid)
    if not field:
        raise HTTPException(status_code=404, detail="Data extraction field not found")
    return field


@router.put("/{field_uuid}", response_model=DataExtractionFieldResponse)
async def update_data_extraction_field_endpoint(
    field_uuid: str, field: DataExtractionFieldUpdate
):
    """Update a data extraction field."""
    existing_field = get_data_extraction_field(field_uuid)
    if not existing_field:
        raise HTTPException(status_code=404, detail="Data extraction field not found")

    updated = update_data_extraction_field(
        field_uuid=field_uuid,
        type=field.type,
        name=field.name,
        description=field.description,
    )

    if not updated:
        raise HTTPException(status_code=400, detail="No fields to update")

    updated_field = get_data_extraction_field(field_uuid)
    return updated_field


@router.delete("/{field_uuid}")
async def delete_data_extraction_field_endpoint(field_uuid: str):
    """Delete a data extraction field."""
    deleted = delete_data_extraction_field(field_uuid)
    if not deleted:
        raise HTTPException(status_code=404, detail="Data extraction field not found")
    return {"message": "Data extraction field deleted successfully"}
