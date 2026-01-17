from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from db import (
    create_metric,
    get_metric,
    get_all_metrics,
    update_metric,
    delete_metric,
)
from auth_utils import get_current_user_id


router = APIRouter(prefix="/metrics", tags=["metrics"])


class MetricCreate(BaseModel):
    name: str
    description: Optional[str] = None
    config: Optional[Dict[str, Any]] = None


class MetricUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    config: Optional[Dict[str, Any]] = None


class MetricResponse(BaseModel):
    uuid: str
    name: str
    description: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    created_at: str
    updated_at: str


class MetricCreateResponse(BaseModel):
    uuid: str
    message: str


class MetricDuplicateRequest(BaseModel):
    name: str


@router.post("", response_model=MetricCreateResponse)
async def create_metric_endpoint(
    metric: MetricCreate, user_id: str = Depends(get_current_user_id)
):
    """Create a new metric."""
    metric_uuid = create_metric(
        name=metric.name,
        description=metric.description,
        config=metric.config,
        user_id=user_id,
    )
    return MetricCreateResponse(uuid=metric_uuid, message="Metric created successfully")


@router.get("", response_model=List[MetricResponse])
async def list_metrics(user_id: str = Depends(get_current_user_id)):
    """List all metrics for the authenticated user."""
    metrics = get_all_metrics(user_id=user_id)
    return metrics


@router.get("/{metric_uuid}", response_model=MetricResponse)
async def get_metric_endpoint(
    metric_uuid: str, user_id: str = Depends(get_current_user_id)
):
    """Get a metric by UUID."""
    metric = get_metric(metric_uuid)
    if not metric:
        raise HTTPException(status_code=404, detail="Metric not found")
    # Verify user owns this metric
    if metric.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    return metric


@router.put("/{metric_uuid}", response_model=MetricResponse)
async def update_metric_endpoint(
    metric_uuid: str, metric: MetricUpdate, user_id: str = Depends(get_current_user_id)
):
    """Update a metric."""
    existing_metric = get_metric(metric_uuid)
    if not existing_metric:
        raise HTTPException(status_code=404, detail="Metric not found")

    # Verify user owns this metric
    if existing_metric.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    updated = update_metric(
        metric_uuid=metric_uuid,
        name=metric.name,
        description=metric.description,
        config=metric.config,
    )

    if not updated:
        raise HTTPException(status_code=400, detail="No fields to update")

    updated_metric = get_metric(metric_uuid)
    return updated_metric


@router.delete("/{metric_uuid}")
async def delete_metric_endpoint(
    metric_uuid: str, user_id: str = Depends(get_current_user_id)
):
    """Delete a metric."""
    # Check if metric exists and user owns it
    existing_metric = get_metric(metric_uuid)
    if not existing_metric:
        raise HTTPException(status_code=404, detail="Metric not found")
    if existing_metric.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    deleted = delete_metric(metric_uuid)
    if not deleted:
        raise HTTPException(status_code=404, detail="Metric not found")
    return {"message": "Metric deleted successfully"}


@router.post("/{metric_uuid}/duplicate", response_model=MetricCreateResponse)
async def duplicate_metric_endpoint(
    metric_uuid: str,
    request: MetricDuplicateRequest,
    user_id: str = Depends(get_current_user_id),
):
    """Duplicate a metric and return the new metric UUID."""
    existing_metric = get_metric(metric_uuid)
    if not existing_metric:
        raise HTTPException(status_code=404, detail="Metric not found")

    # Verify user owns this metric
    if existing_metric.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    new_metric_uuid = create_metric(
        name=request.name,
        description=existing_metric.get("description"),
        config=existing_metric.get("config"),
        user_id=user_id,
    )
    return MetricCreateResponse(
        uuid=new_metric_uuid, message="Metric duplicated successfully"
    )
