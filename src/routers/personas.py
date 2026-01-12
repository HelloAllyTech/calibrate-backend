from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import (
    create_persona,
    get_persona,
    get_all_personas,
    update_persona,
    delete_persona,
)


router = APIRouter(prefix="/personas", tags=["personas"])


class PersonaCreate(BaseModel):
    name: str
    description: Optional[str] = None
    config: Optional[Dict[str, Any]] = None


class PersonaUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    config: Optional[Dict[str, Any]] = None


class PersonaResponse(BaseModel):
    uuid: str
    name: str
    description: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    created_at: str
    updated_at: str


class PersonaCreateResponse(BaseModel):
    uuid: str
    message: str


@router.post("", response_model=PersonaCreateResponse)
async def create_persona_endpoint(persona: PersonaCreate):
    """Create a new persona."""
    persona_uuid = create_persona(
        name=persona.name,
        description=persona.description,
        config=persona.config,
    )
    return PersonaCreateResponse(uuid=persona_uuid, message="Persona created successfully")


@router.get("", response_model=List[PersonaResponse])
async def list_personas():
    """List all personas."""
    personas = get_all_personas()
    return personas


@router.get("/{persona_uuid}", response_model=PersonaResponse)
async def get_persona_endpoint(persona_uuid: str):
    """Get a persona by UUID."""
    persona = get_persona(persona_uuid)
    if not persona:
        raise HTTPException(status_code=404, detail="Persona not found")
    return persona


@router.put("/{persona_uuid}", response_model=PersonaResponse)
async def update_persona_endpoint(persona_uuid: str, persona: PersonaUpdate):
    """Update a persona."""
    existing_persona = get_persona(persona_uuid)
    if not existing_persona:
        raise HTTPException(status_code=404, detail="Persona not found")

    updated = update_persona(
        persona_uuid=persona_uuid,
        name=persona.name,
        description=persona.description,
        config=persona.config,
    )

    if not updated:
        raise HTTPException(status_code=400, detail="No fields to update")

    updated_persona = get_persona(persona_uuid)
    return updated_persona


@router.delete("/{persona_uuid}")
async def delete_persona_endpoint(persona_uuid: str):
    """Delete a persona."""
    deleted = delete_persona(persona_uuid)
    if not deleted:
        raise HTTPException(status_code=404, detail="Persona not found")
    return {"message": "Persona deleted successfully"}
