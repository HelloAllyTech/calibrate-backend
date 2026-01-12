from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import (
    create_metric,
    get_metric,
    get_all_metrics,
    update_metric,
    delete_metric,
)


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


@router.post("", response_model=MetricCreateResponse)
async def create_metric_endpoint(metric: MetricCreate):
    """Create a new metric."""
    metric_uuid = create_metric(
        name=metric.name,
        description=metric.description,
        config=metric.config,
    )
    return MetricCreateResponse(uuid=metric_uuid, message="Metric created successfully")


@router.get("", response_model=List[MetricResponse])
async def list_metrics():
    """List all metrics."""
    metrics = get_all_metrics()
    return metrics


@router.get("/{metric_uuid}", response_model=MetricResponse)
async def get_metric_endpoint(metric_uuid: str):
    """Get a metric by UUID."""
    metric = get_metric(metric_uuid)
    if not metric:
        raise HTTPException(status_code=404, detail="Metric not found")
    return metric


@router.put("/{metric_uuid}", response_model=MetricResponse)
async def update_metric_endpoint(metric_uuid: str, metric: MetricUpdate):
    """Update a metric."""
    existing_metric = get_metric(metric_uuid)
    if not existing_metric:
        raise HTTPException(status_code=404, detail="Metric not found")

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
async def delete_metric_endpoint(metric_uuid: str):
    """Delete a metric."""
    deleted = delete_metric(metric_uuid)
    if not deleted:
        raise HTTPException(status_code=404, detail="Metric not found")
    return {"message": "Metric deleted successfully"}
