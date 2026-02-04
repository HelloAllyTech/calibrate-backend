import os
import json
import subprocess
import time
import traceback
import threading
import logging
import tempfile
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field, field_validator

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
    remove_persona_from_simulation,
    remove_scenario_from_simulation,
    remove_metric_from_simulation,
    get_personas_for_simulation,
    get_scenarios_for_simulation,
    get_metrics_for_simulation,
    get_agent,
    get_tools_for_agent,
    create_simulation_job,
    get_simulation_job,
    update_simulation_job,
    get_simulation_jobs_for_simulation,
    delete_simulation_job,
)
from utils import (
    TaskStatus,
    TaskCreateResponse,
    get_s3_client,
    get_s3_output_config,
    reserve_port,
    release_port,
    can_start_simulation_job,
    try_start_queued_simulation_job,
    register_job_starter,
    generate_presigned_download_url,
    kill_process_group,
    is_job_timed_out,
    capture_exception_to_sentry,
    build_tool_configs,
    PRESIGNED_URL_EXPIRY_SECONDS,
    PRESIGNED_URL_REFRESH_BUFFER_SECONDS,
)
from auth_utils import get_current_user_id
from datetime import datetime

# Job types that share the same queue
SIMULATION_JOB_TYPES = ["text", "voice"]


def _start_simulation_job_from_queue(job: dict) -> bool:
    """Start a simulation job from the queue."""
    job_id = job["uuid"]
    job_type = job.get("type")  # 'text' or 'voice'
    details = job.get("details", {})

    simulation_uuid = details.get("simulation_uuid")
    agent_uuid = details.get("agent_uuid")
    s3_bucket = details.get("s3_bucket", "")

    # Get simulation details
    simulation = get_simulation(simulation_uuid)
    if not simulation:
        return False

    # Get agent
    agent = get_agent(agent_uuid)
    if not agent:
        return False

    # Get linked entities
    personas = get_personas_for_simulation(simulation_uuid)
    scenarios = get_scenarios_for_simulation(simulation_uuid)
    metrics = get_metrics_for_simulation(simulation_uuid)

    if not personas or not scenarios:
        return False

    # Start background task in a separate thread
    thread = threading.Thread(
        target=run_simulation_task,
        args=(job_id, agent, personas, scenarios, metrics, s3_bucket, job_type),
        daemon=True,
    )
    thread.start()

    return True


# Register the job starters for simulation jobs
register_job_starter("text", _start_simulation_job_from_queue)
register_job_starter("voice", _start_simulation_job_from_queue)

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/simulations", tags=["simulations"])


def _should_regenerate_presigned_urls(
    presigned_urls_generated_at: Optional[str],
) -> bool:
    """
    Check if presigned URLs need to be regenerated based on when they were created.

    Args:
        presigned_urls_generated_at: ISO timestamp when URLs were generated, or None

    Returns:
        True if URLs should be regenerated (expired or about to expire or never generated)
    """
    if not presigned_urls_generated_at:
        return True

    try:
        generated_at = datetime.fromisoformat(
            presigned_urls_generated_at.replace("Z", "+00:00")
        )
        # Remove timezone info for comparison with utcnow
        if generated_at.tzinfo is not None:
            generated_at = generated_at.replace(tzinfo=None)

        now = datetime.utcnow()
        elapsed_seconds = (now - generated_at).total_seconds()

        # Regenerate if elapsed time exceeds expiry minus buffer
        threshold = PRESIGNED_URL_EXPIRY_SECONDS - PRESIGNED_URL_REFRESH_BUFFER_SECONDS
        return elapsed_seconds >= threshold
    except Exception as e:
        logger.warning(f"Failed to parse presigned_urls_generated_at: {e}")
        return True


def _get_audio_urls_from_s3_key(s3_key_prefix: str, s3_bucket: str) -> List[str]:
    """
    List all audio files in an S3 key prefix and generate presigned URLs for them.

    Args:
        s3_key_prefix: S3 key prefix (e.g., "simulations/runs/task_id/simulation_persona_1_scenario_1/audios")
        s3_bucket: S3 bucket name

    Returns:
        List of presigned URLs for audio files, sorted by filename
    """
    try:
        s3 = get_s3_client()

        # Ensure prefix ends with / for directory listing
        if s3_key_prefix and not s3_key_prefix.endswith("/"):
            s3_key_prefix += "/"

        # List objects in the S3 prefix
        audio_extensions = {".wav", ".mp3", ".ogg"}
        audio_files = []

        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=s3_bucket, Prefix=s3_key_prefix):
            if "Contents" in page:
                for obj in page["Contents"]:
                    key = obj["Key"]
                    # Skip if it's a directory marker
                    if key.endswith("/"):
                        continue
                    # Check if it's an audio file
                    file_ext = Path(key).suffix.lower()
                    if file_ext in audio_extensions:
                        audio_files.append(key)

        # Sort audio files by filename (natural sort for numbered files)
        # Files are typically named like: 0_user.wav, 1_bot.wav, 1_user.wav, 2_bot.wav
        def natural_sort_key(key: str) -> tuple:
            """Extract numeric parts for natural sorting"""
            filename = Path(key).name
            # Extract leading number if present
            parts = filename.split("_", 1)
            if len(parts) > 1 and parts[0].isdigit():
                # Has numeric prefix: sort by number first, then by rest of filename
                return (int(parts[0]), parts[1])
            else:
                # No numeric prefix: sort alphabetically
                return (float("inf"), filename)

        audio_files.sort(key=natural_sort_key)

        # Generate presigned URLs
        presigned_urls = []
        for audio_key in audio_files:
            presigned_url = generate_presigned_download_url(audio_key, bucket=s3_bucket)
            if presigned_url:
                presigned_urls.append(presigned_url)
                logger.info(f"Generated presigned URL for {audio_key}")
            else:
                # Fallback to S3 path if presigned URL generation fails
                presigned_urls.append(f"s3://{s3_bucket}/{audio_key}")

        return presigned_urls

    except Exception as e:
        logger.error(
            f"Error listing audio files from S3 key prefix {s3_key_prefix}: {str(e)}"
        )
        return []


class SimulationCreate(BaseModel):
    name: str
    agent_uuid: Optional[str] = None
    persona_uuids: Optional[List[str]] = None
    scenario_uuids: Optional[List[str]] = None
    metric_uuids: Optional[List[str]] = None


class SimulationUpdate(BaseModel):
    name: Optional[str] = None
    agent_uuid: Optional[str] = None
    persona_uuids: Optional[List[str]] = None
    scenario_uuids: Optional[List[str]] = None
    metric_uuids: Optional[List[str]] = None


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


class AgentSummaryResponse(BaseModel):
    uuid: str
    name: str
    config: Optional[Dict[str, Any]] = None
    created_at: str
    updated_at: str


class SimulationListResponse(BaseModel):
    uuid: str
    name: str
    agent: Optional[AgentSummaryResponse] = None
    created_at: str
    updated_at: str


class SimulationDetailResponse(BaseModel):
    uuid: str
    name: str
    agent: Optional[AgentSummaryResponse] = None
    created_at: str
    updated_at: str
    personas: List[PersonaResponse]
    scenarios: List[ScenarioResponse]
    metrics: List[MetricResponse]


class SimulationCreateResponse(BaseModel):
    uuid: str
    message: str


class RunSimulationRequest(BaseModel):
    type: str = Field(..., description="Type of simulation run: 'text' or 'voice'")

    @field_validator("type")
    @classmethod
    def validate_type(cls, v):
        if v not in ["text", "voice"]:
            raise ValueError("type must be either 'text' or 'voice'")
        return v


class EvaluationCriterionResult(BaseModel):
    name: str
    value: float
    reasoning: str


class SimulationCaseResult(BaseModel):
    """Result for a single persona-scenario simulation"""

    simulation_name: str
    persona: Optional[Dict[str, Any]] = (
        None  # Full persona object from config.json (with label, characteristics, gender, language)
    )
    scenario: Optional[Dict[str, Any]] = (
        None  # Full scenario object from config.json (with name/label and description)
    )
    evaluation_results: Optional[List[EvaluationCriterionResult]] = None
    transcript: Optional[List[Dict[str, Any]]] = None

    audio_urls: Optional[List[str]] = (
        None  # List of presigned URLs for audio files in order (for voice simulations)
    )
    conversation_wav_url: Optional[str] = (
        None  # Presigned URL for the combined conversation.wav file (for voice simulations)
    )


class SimulationRunStatusResponse(BaseModel):
    task_id: str
    name: str  # Format: "Run {index}"
    status: str
    type: str
    updated_at: str
    total_simulations: Optional[int] = None
    completed_simulations: Optional[int] = (
        None  # Number of completed simulations (for in_progress voice simulations)
    )
    metrics: Optional[Dict[str, Any]] = None
    simulation_results: Optional[List[SimulationCaseResult]] = None
    error: Optional[str] = None


class SimulationRunListItem(BaseModel):
    uuid: str
    name: str  # Format: "Run {index}"
    status: str
    type: str
    updated_at: str


class SimulationRunsResponse(BaseModel):
    runs: List[SimulationRunListItem]


@router.post("", response_model=SimulationCreateResponse)
async def create_simulation_endpoint(
    simulation: SimulationCreate, user_id: str = Depends(get_current_user_id)
):
    """Create a new simulation with optional linked agent, personas, scenarios, and metrics."""
    # Verify agent exists if provided
    if simulation.agent_uuid:
        agent = get_agent(simulation.agent_uuid)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")

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
    simulation_uuid = create_simulation(
        name=simulation.name, agent_id=simulation.agent_uuid, user_id=user_id
    )

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
async def list_simulations(user_id: str = Depends(get_current_user_id)):
    """List all simulations for the authenticated user."""
    simulations = get_all_simulations(user_id=user_id)
    result = []
    for sim in simulations:
        agent = None
        if sim.get("agent_id"):
            agent_data = get_agent(sim["agent_id"])
            if agent_data:
                agent = AgentSummaryResponse(
                    uuid=agent_data["uuid"],
                    name=agent_data["name"],
                    config=agent_data.get("config"),
                    created_at=agent_data["created_at"],
                    updated_at=agent_data["updated_at"],
                )
        result.append(
            SimulationListResponse(
                uuid=sim["uuid"],
                name=sim["name"],
                agent=agent,
                created_at=sim["created_at"],
                updated_at=sim["updated_at"],
            )
        )
    return result


@router.get("/run/{task_id}", response_model=SimulationRunStatusResponse)
async def get_simulation_run_status(
    task_id: str, user_id: str = Depends(get_current_user_id)
):
    """
    Get the status of a simulation run.

    Returns the current status and, if done, the simulation results.
    """
    job = get_simulation_job(task_id)
    if not job:
        raise HTTPException(status_code=404, detail="Task not found")

    # Verify user owns the parent simulation
    simulation_id = job.get("simulation_id")
    if simulation_id:
        simulation = get_simulation(simulation_id)
        if not simulation or simulation.get("user_id") != user_id:
            raise HTTPException(status_code=404, detail="Task not found")

    status = job["status"]
    results = job.get("results") or {}
    details = job.get("details") or {}

    # Check for timeout on in-progress jobs
    if status == TaskStatus.IN_PROGRESS.value:
        updated_at = job.get("updated_at")
        if updated_at and is_job_timed_out(updated_at, timeout_minutes=15):
            logger.warning(f"Simulation job {task_id} timed out, marking as failed")

            # Kill running process
            pid = details.get("pid") or details.get("pgid")
            if pid:
                kill_process_group(pid, task_id)

            # Release port if allocated
            port = details.get("port")
            if port:
                release_port(port)

            # Mark job as failed (preserve existing results, add error)
            results["error"] = "Job timed out after 5 minutes of inactivity"
            update_simulation_job(
                task_id,
                status=TaskStatus.FAILED.value,
                results=results,
            )
            status = TaskStatus.FAILED.value

            # Try to start the next queued job
            try_start_queued_simulation_job(SIMULATION_JOB_TYPES)

    # Calculate run index based on creation order
    run_name = "Run 1"  # Default
    if simulation_id:
        all_jobs = get_simulation_jobs_for_simulation(simulation_id)
        # Sort by created_at ASC to get oldest first (Run 1 is the oldest)
        sorted_jobs = sorted(all_jobs, key=lambda j: j.get("created_at", ""))
        # Find the index of current job (1-indexed)
        for idx, j in enumerate(sorted_jobs, start=1):
            if j["uuid"] == task_id:
                run_name = f"Run {idx}"
                break

    simulation_results = results.get("simulation_results") or []

    # If this is a voice simulation, handle presigned URLs based on status
    if job.get("type") == "voice" and simulation_results:
        if status == TaskStatus.DONE.value:
            # For done status: generate presigned URLs on-the-fly from S3 paths
            # Don't cache them in the database
            try:
                s3_bucket = get_s3_output_config()

                for sim_result in simulation_results:
                    # Generate audio URLs from S3 path
                    audios_s3_key_prefix = sim_result.get("audios_s3_path")
                    if audios_s3_key_prefix:
                        audio_urls = _get_audio_urls_from_s3_key(
                            audios_s3_key_prefix, s3_bucket
                        )
                        sim_result["audio_urls"] = audio_urls
                        logger.info(
                            f"Generated {len(audio_urls)} presigned URLs on-the-fly for simulation {sim_result.get('simulation_name')}"
                        )

                    # Generate presigned URL for conversation.wav
                    conversation_wav_s3_key = sim_result.get("conversation_wav_s3_key")
                    if conversation_wav_s3_key:
                        conversation_wav_url = generate_presigned_download_url(
                            conversation_wav_s3_key, bucket=s3_bucket
                        )
                        sim_result["conversation_wav_url"] = (
                            conversation_wav_url if conversation_wav_url else ""
                        )
                        logger.info(
                            f"Generated presigned URL on-the-fly for conversation.wav for simulation {sim_result.get('simulation_name')}"
                        )
                    else:
                        sim_result["conversation_wav_url"] = ""

            except Exception as e:
                logger.warning(f"Failed to generate audio URLs: {str(e)}")
                # Continue without audio URLs if generation fails
        # For in-progress status: presigned URLs are already stored in results during monitoring
        # Just return them as-is (they were generated when the audio files were uploaded)

    return SimulationRunStatusResponse(
        task_id=task_id,
        name=run_name,
        status=status,
        type=job["type"],
        updated_at=job["updated_at"],
        total_simulations=results.get("total_simulations"),
        completed_simulations=results.get("completed_simulations"),
        metrics=results.get("metrics"),
        simulation_results=simulation_results,
        error=results.get("error"),
    )


@router.get("/{simulation_uuid}/runs", response_model=SimulationRunsResponse)
async def get_simulation_runs(
    simulation_uuid: str, user_id: str = Depends(get_current_user_id)
):
    """
    Get all runs for a simulation.

    Returns a list of all simulation runs with their UUID, status, type, and name.
    """
    # Verify simulation exists
    simulation = get_simulation(simulation_uuid)
    if not simulation:
        raise HTTPException(status_code=404, detail="Simulation not found")

    # Verify user owns this simulation
    if simulation.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Get all jobs for this simulation
    jobs = get_simulation_jobs_for_simulation(simulation_uuid)

    # Sort by created_at ASC to calculate run index (Run 1 is the oldest)
    sorted_by_created = sorted(jobs, key=lambda j: j.get("created_at", ""))

    # Create a mapping of job UUID to run index
    job_to_index = {
        job["uuid"]: idx for idx, job in enumerate(sorted_by_created, start=1)
    }

    # Sort by updated_at DESC for response (most recently updated first)
    sorted_by_updated = sorted(
        jobs, key=lambda j: j.get("updated_at", ""), reverse=True
    )

    runs = [
        SimulationRunListItem(
            uuid=job["uuid"],
            name=f"Run {job_to_index[job['uuid']]}",  # Use the index from creation order
            status=job["status"],
            type=job["type"],
            updated_at=job["updated_at"],
        )
        for job in sorted_by_updated
    ]

    return SimulationRunsResponse(runs=runs)


@router.get("/{simulation_uuid}", response_model=SimulationDetailResponse)
async def get_simulation_endpoint(
    simulation_uuid: str, user_id: str = Depends(get_current_user_id)
):
    """Get a simulation by UUID with all linked agent, personas, scenarios, and metrics."""
    simulation = get_simulation(simulation_uuid)
    if not simulation:
        raise HTTPException(status_code=404, detail="Simulation not found")

    # Verify user owns this simulation
    if simulation.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Get linked agent
    agent = None
    if simulation.get("agent_id"):
        agent_data = get_agent(simulation["agent_id"])
        if agent_data:
            agent = AgentSummaryResponse(
                uuid=agent_data["uuid"],
                name=agent_data["name"],
                config=agent_data.get("config"),
                created_at=agent_data["created_at"],
                updated_at=agent_data["updated_at"],
            )

    # Get linked entities
    personas = get_personas_for_simulation(simulation_uuid)
    scenarios = get_scenarios_for_simulation(simulation_uuid)
    metrics = get_metrics_for_simulation(simulation_uuid)

    return SimulationDetailResponse(
        uuid=simulation["uuid"],
        name=simulation["name"],
        agent=agent,
        created_at=simulation["created_at"],
        updated_at=simulation["updated_at"],
        personas=personas,
        scenarios=scenarios,
        metrics=metrics,
    )


@router.put("/{simulation_uuid}", response_model=SimulationDetailResponse)
async def update_simulation_endpoint(
    simulation_uuid: str,
    simulation: SimulationUpdate,
    user_id: str = Depends(get_current_user_id),
):
    """Update a simulation with optional linked agent, personas, scenarios, and metrics."""
    existing_simulation = get_simulation(simulation_uuid)
    if not existing_simulation:
        raise HTTPException(status_code=404, detail="Simulation not found")

    # Verify user owns this simulation
    if existing_simulation.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Verify agent exists if provided
    if simulation.agent_uuid is not None and simulation.agent_uuid != "":
        agent = get_agent(simulation.agent_uuid)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")

    # Verify all new personas exist
    if simulation.persona_uuids is not None:
        for persona_uuid in simulation.persona_uuids:
            persona = get_persona(persona_uuid)
            if not persona:
                raise HTTPException(
                    status_code=404, detail=f"Persona {persona_uuid} not found"
                )

    # Verify all new scenarios exist
    if simulation.scenario_uuids is not None:
        for scenario_uuid in simulation.scenario_uuids:
            scenario = get_scenario(scenario_uuid)
            if not scenario:
                raise HTTPException(
                    status_code=404, detail=f"Scenario {scenario_uuid} not found"
                )

    # Verify all new metrics exist
    if simulation.metric_uuids is not None:
        for metric_uuid in simulation.metric_uuids:
            metric = get_metric(metric_uuid)
            if not metric:
                raise HTTPException(
                    status_code=404, detail=f"Metric {metric_uuid} not found"
                )

    # Update simulation name and/or agent if provided
    if simulation.name is not None or simulation.agent_uuid is not None:
        # Empty string means clear the agent
        if simulation.agent_uuid == "":
            update_simulation(
                simulation_uuid=simulation_uuid,
                name=simulation.name,
                clear_agent=True,
            )
        else:
            update_simulation(
                simulation_uuid=simulation_uuid,
                name=simulation.name,
                agent_id=simulation.agent_uuid,
            )

    # Update personas if provided (replace existing)
    if simulation.persona_uuids is not None:
        # Remove existing personas
        existing_personas = get_personas_for_simulation(simulation_uuid)
        for persona in existing_personas:
            remove_persona_from_simulation(simulation_uuid, persona["uuid"])
        # Add new personas
        for persona_uuid in simulation.persona_uuids:
            add_persona_to_simulation(simulation_uuid, persona_uuid)

    # Update scenarios if provided (replace existing)
    if simulation.scenario_uuids is not None:
        # Remove existing scenarios
        existing_scenarios = get_scenarios_for_simulation(simulation_uuid)
        for scenario in existing_scenarios:
            remove_scenario_from_simulation(simulation_uuid, scenario["uuid"])
        # Add new scenarios
        for scenario_uuid in simulation.scenario_uuids:
            add_scenario_to_simulation(simulation_uuid, scenario_uuid)

    # Update metrics if provided (replace existing)
    if simulation.metric_uuids is not None:
        # Remove existing metrics
        existing_metrics = get_metrics_for_simulation(simulation_uuid)
        for metric in existing_metrics:
            remove_metric_from_simulation(simulation_uuid, metric["uuid"])
        # Add new metrics
        for metric_uuid in simulation.metric_uuids:
            add_metric_to_simulation(simulation_uuid, metric_uuid)

    # Return full detail response
    updated_simulation = get_simulation(simulation_uuid)

    # Get linked agent
    agent = None
    if updated_simulation.get("agent_id"):
        agent_data = get_agent(updated_simulation["agent_id"])
        if agent_data:
            agent = AgentSummaryResponse(
                uuid=agent_data["uuid"],
                name=agent_data["name"],
                config=agent_data.get("config"),
                created_at=agent_data["created_at"],
                updated_at=agent_data["updated_at"],
            )

    personas = get_personas_for_simulation(simulation_uuid)
    scenarios = get_scenarios_for_simulation(simulation_uuid)
    metrics = get_metrics_for_simulation(simulation_uuid)

    return SimulationDetailResponse(
        uuid=updated_simulation["uuid"],
        name=updated_simulation["name"],
        agent=agent,
        created_at=updated_simulation["created_at"],
        updated_at=updated_simulation["updated_at"],
        personas=personas,
        scenarios=scenarios,
        metrics=metrics,
    )


@router.delete("/{simulation_uuid}")
async def delete_simulation_endpoint(
    simulation_uuid: str, user_id: str = Depends(get_current_user_id)
):
    """Delete a simulation."""
    # Check if simulation exists and user owns it
    existing_simulation = get_simulation(simulation_uuid)
    if not existing_simulation:
        raise HTTPException(status_code=404, detail="Simulation not found")
    if existing_simulation.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    deleted = delete_simulation(simulation_uuid)
    if not deleted:
        raise HTTPException(status_code=404, detail="Simulation not found")
    return {"message": "Simulation deleted successfully"}


# ============ Run Simulation API ============


def _build_calibrate_simulation_config(
    agent: Dict[str, Any],
    personas: List[Dict[str, Any]],
    scenarios: List[Dict[str, Any]],
    metrics: List[Dict[str, Any]],
    simulation_type: str = "text",
) -> Dict[str, Any]:
    """
    Build the calibrate simulation config from agent, personas, scenarios, and metrics.

    Args:
        agent: Agent dict with config containing system_prompt and llm.model
        personas: List of persona dicts with description and config (containing gender, language)
        scenarios: List of scenario dicts with description
        metrics: List of metric dicts with name and description (for evaluation_criteria)
        simulation_type: Type of simulation - "text" or "voice"
    """
    agent_config = agent.get("config") or {}

    # Get model from agent config
    llm_config = agent_config.get("llm", {})
    model = llm_config.get("model", "gpt-4.1")

    # Get tools from agent_tools table
    agent_tools = get_tools_for_agent(agent["uuid"])
    tool_configs = build_tool_configs(agent_tools)

    # Build personas list as objects with label, characteristics, gender, and language
    persona_list = []
    for persona in personas:
        persona_config = persona.get("config") or {}
        persona_obj = {
            "label": persona.get("name", ""),  # Store persona name/label
            "characteristics": persona.get("description") or persona.get("name"),
            "gender": persona_config.get("gender", "female"),
            "language": persona_config.get("language", "english"),
        }
        # For voice simulations, add interruption_sensitivity if present
        if simulation_type == "voice":
            interruption_sensitivity = persona_config.get(
                "interruption_sensitivity", "medium"
            )
            persona_obj["interruption_sensitivity"] = interruption_sensitivity

        if persona_obj["characteristics"]:
            persona_list.append(persona_obj)

    # Build scenarios list as objects with name and description
    scenario_list = []
    for scenario in scenarios:
        scenario_obj = {
            "name": scenario.get("name", ""),  # Store scenario name/label
            "description": scenario.get("description", ""),
        }
        scenario_list.append(scenario_obj)

    # Build evaluation criteria from metrics
    evaluation_criteria = [
        {
            "name": metric.get("name"),
            "description": metric.get("description") or metric.get("name"),
        }
        for metric in metrics
        if metric.get("name")
    ]

    config = {
        "tools": tool_configs,
        "personas": persona_list,
        "scenarios": scenario_list,
        "evaluation_criteria": evaluation_criteria,
    }

    config["system_prompt"] = agent_config.get("system_prompt", "")

    # Copy settings from agent config, with defaults
    settings_config = agent_config.get("settings", {})
    config["settings"] = {
        "agent_speaks_first": settings_config.get("agent_speaks_first", True),
        "max_turns": settings_config.get("max_assistant_turns", 50),
    }

    if simulation_type == "text":
        config["params"] = {"model": model}
    else:
        # For voice simulations, include stt, tts, and llm configurations
        # Get STT config from agent config (default: google)
        stt_config = agent_config.get("stt", {})
        if stt_config:
            config["stt"] = stt_config

        # Get TTS config from agent config (default: google)
        tts_config = agent_config.get("tts", {})
        if tts_config:
            config["tts"] = tts_config

        # Get LLM config from agent config (includes provider and model)
        if llm_config:
            config["llm"] = llm_config

    return config


def _extract_persona_scenario_indices(sim_name: str) -> tuple:
    """
    Extract persona and scenario indices from simulation directory name.
    Format: simulation_persona_N_scenario_M (1-based indices)
    Returns (persona_index, scenario_index) as 0-based indices, or (None, None) if parsing fails.
    """
    import re

    match = re.match(r"simulation_persona_(\d+)_scenario_(\d+)", sim_name)
    if match:
        # Convert from 1-based (in folder name) to 0-based (for list indexing)
        return int(match.group(1)) - 1, int(match.group(2)) - 1
    return None, None


def _parse_text_simulation_directory(
    sim_dir: Path,
    personas_list: Optional[List[Dict[str, Any]]] = None,
    scenarios_list: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Parse a single text simulation directory.

    Returns results for both complete (has evaluation_results.csv) and
    in-progress (has transcript.json but no evaluation_results.csv) simulations.

    Args:
        sim_dir: Path to the simulation directory
        personas_list: Optional list of personas from calibrate config (used as fallback)
        scenarios_list: Optional list of scenarios from calibrate config (used as fallback)

    Returns:
        Dict with simulation result data, or None if directory doesn't exist
    """
    import csv

    if not sim_dir.exists():
        return None

    sim_name = sim_dir.name
    eval_results_file = sim_dir / "evaluation_results.csv"
    transcript_file = sim_dir / "transcript.json"
    config_file = sim_dir / "config.json"

    # Check if simulation is complete
    is_complete = eval_results_file.exists()

    eval_results = []
    if eval_results_file.exists():
        try:
            with open(eval_results_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    eval_results.append(
                        {
                            "name": row.get("name"),
                            "value": row.get("value"),
                            "reasoning": row.get("reasoning", ""),
                        }
                    )
        except Exception as e:
            logger.warning(
                f"Failed to parse evaluation_results.csv for {sim_name}: {e}"
            )

    # Parse transcript.json if it exists
    transcript = None
    if transcript_file.exists():
        try:
            with open(transcript_file, "r", encoding="utf-8") as f:
                transcript = json.load(f)
        except Exception as e:
            logger.warning(f"Failed to parse transcript.json for {sim_name}: {e}")

    # Parse config.json to get persona and scenario data
    persona_data = None
    scenario_data = None
    if config_file.exists():
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                config_data = json.load(f)
                persona_data = config_data.get("persona")
                scenario_data = config_data.get("scenario")
        except Exception as e:
            logger.warning(f"Failed to parse config.json for {sim_name}: {e}")

    # Fallback: if persona/scenario not in config.json, extract from directory name
    if (persona_data is None or scenario_data is None) and (
        personas_list or scenarios_list
    ):
        persona_idx, scenario_idx = _extract_persona_scenario_indices(sim_name)
        if persona_data is None and personas_list and persona_idx is not None:
            if 0 <= persona_idx < len(personas_list):
                persona_data = personas_list[persona_idx]
        if scenario_data is None and scenarios_list and scenario_idx is not None:
            if 0 <= scenario_idx < len(scenarios_list):
                scenario_data = scenarios_list[scenario_idx]

    # Only return if we have at least config.json or transcript.json (simulation has started)
    if not config_file.exists() and not transcript_file.exists():
        return None

    return {
        "simulation_name": sim_name,
        "persona": persona_data,
        "scenario": scenario_data,
        "evaluation_results": eval_results if is_complete else None,
        "transcript": transcript,
        "is_complete": is_complete,
    }


def _get_text_simulation_directories(output_dir: Path) -> List[Path]:
    """Get all simulation directories from output directory."""
    sim_dirs = []
    if not output_dir.exists():
        return sim_dirs
    for root, dirs, files in os.walk(output_dir):
        for dir_name in dirs:
            if dir_name.startswith("simulation_persona_"):
                sim_dirs.append(Path(root) / dir_name)
    return sim_dirs


def _update_text_simulation_intermediate_results(
    task_id: str,
    output_dir: Path,
    expected_total: int,
    s3_prefix: str,
    personas_list: Optional[List[Dict[str, Any]]] = None,
    scenarios_list: Optional[List[Dict[str, Any]]] = None,
    prev_state: Optional[tuple] = None,
) -> Optional[tuple]:
    """Update intermediate results for a text simulation job.

    Args:
        prev_state: Previous state tuple for change detection

    Returns:
        Current state tuple (to be passed as prev_state in next call)
    """
    simulation_results = []
    completed_count = 0
    transcript_lengths = []  # For change detection

    for sim_dir in _get_text_simulation_directories(output_dir):
        sim_result = _parse_text_simulation_directory(
            sim_dir, personas_list, scenarios_list
        )
        if sim_result:
            # Remove is_complete field before storing (internal use only)
            is_complete = sim_result.pop("is_complete", False)
            if is_complete:
                completed_count += 1
            simulation_results.append(sim_result)
            # Track transcript length for change detection
            transcript = sim_result.get("transcript") or []
            transcript_lengths.append((sim_dir.name, len(transcript)))

    if not simulation_results:
        return prev_state

    # Build current state for change detection
    # Note: Don't read metrics.json during in-progress - calibrate creates it incrementally
    # and reading it before all simulations complete will give incomplete metrics.
    # The final metrics are read after the process completes in _run_calibrate_text_simulation.
    current_state = (
        completed_count,
        tuple(sorted(transcript_lengths)),
    )

    # Only update DB if state changed
    if current_state != prev_state:
        update_simulation_job(
            task_id,
            status=TaskStatus.IN_PROGRESS.value,
            results={
                "total_simulations": expected_total,
                "completed_simulations": completed_count,
                "simulation_results": simulation_results,
                "results_s3_prefix": s3_prefix,
                "metrics": None,  # Don't include metrics during in-progress
            },
        )

    return current_state


def _run_calibrate_text_simulation(
    model: str,
    calibrate_config: Dict[str, Any],
    input_dir: Path,
    output_dir: Path,
    s3_bucket: str,
    s3_prefix: str,
    task_id: Optional[str] = None,
    log_prefix: str = "LLM simulation",
) -> Dict[str, Any]:
    """
    Run calibrate llm simulations run command and return parsed results.
    Updates the database incrementally as each simulation completes.

    Args:
        model: Model name to use
        calibrate_config: The calibrate config dict
        input_dir: Directory to write config files
        output_dir: Directory to write output files
        s3_bucket: S3 bucket name
        s3_prefix: S3 key prefix for uploading results
        task_id: Optional task ID for intermediate updates
        log_prefix: Prefix for log messages

    Returns:
        Dict with keys: success, total_simulations, metrics, simulation_results, error
    """
    s3 = get_s3_client()

    # Update config with model
    config = calibrate_config.copy()
    config["params"] = {"model": model}

    # Resolve directories to absolute paths
    input_dir = input_dir.resolve()
    output_dir = output_dir.resolve()

    # Create directories
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Write config to input directory
    config_file_name = "simulation_config"
    config_file = input_dir / f"{config_file_name}.json"
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    # Get personas and scenarios lists for intermediate results
    personas_list = calibrate_config.get("personas", [])
    scenarios_list = calibrate_config.get("scenarios", [])
    expected_total = len(personas_list) * len(scenarios_list)

    # Run calibrate llm simulations run command
    # Use absolute paths for config and output
    run_cmd = [
        "calibrate",
        "llm",
        "simulations",
        "run",
        "-c",
        str(config_file),
        "-o",
        str(output_dir),
        "-m",
        model,
        "-n",
        "4",
    ]

    logger.info(f"{log_prefix} command: {' '.join(run_cmd)}")

    # Use Popen with polling for intermediate updates
    stdout_file = tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".log")
    stderr_file = tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".log")

    try:
        process = subprocess.Popen(
            run_cmd,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,
        )

        # Poll for process completion while updating intermediate results
        poll_interval = 2  # seconds
        prev_state = None  # Track state to avoid unnecessary DB updates

        while process.poll() is None:
            if task_id:
                prev_state = _update_text_simulation_intermediate_results(
                    task_id,
                    output_dir,
                    expected_total,
                    s3_prefix,
                    personas_list,
                    scenarios_list,
                    prev_state,
                )
            time.sleep(poll_interval)

        # Final update after process completes
        if task_id:
            _update_text_simulation_intermediate_results(
                task_id,
                output_dir,
                expected_total,
                s3_prefix,
                personas_list,
                scenarios_list,
                prev_state,
            )

        # Read stdout/stderr
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout_content = stdout_file.read()
        stderr_content = stderr_file.read()

        if stdout_content:
            logger.info(f"{log_prefix} stdout: {stdout_content}")
        if stderr_content:
            logger.info(f"{log_prefix} stderr: {stderr_content}")

    finally:
        stdout_file.close()
        stderr_file.close()
        os.unlink(stdout_file.name)
        os.unlink(stderr_file.name)

    # Parse final results
    metrics_data = None
    simulation_results = []

    # Find metrics.json file
    metrics_file = output_dir / "metrics.json"
    if metrics_file.exists():
        with open(metrics_file, "r", encoding="utf-8") as f:
            metrics_data = json.load(f)

    # Parse all simulation directories
    for sim_dir in _get_text_simulation_directories(output_dir):
        sim_result = _parse_text_simulation_directory(
            sim_dir, personas_list, scenarios_list
        )
        if sim_result:
            # Remove is_complete field (internal use only)
            sim_result.pop("is_complete", None)
            simulation_results.append(sim_result)

    # Upload results to S3
    for root, dirs, files in os.walk(output_dir):
        for file in files:
            local_file_path = Path(root) / file
            relative_path = local_file_path.relative_to(output_dir)
            s3_key = f"{s3_prefix}/{relative_path}"
            s3.upload_file(str(local_file_path), s3_bucket, s3_key)

    # Upload the config file to S3
    if config_file.exists():
        config_s3_key = f"{s3_prefix}/simulation_config.json"
        s3.upload_file(str(config_file), s3_bucket, config_s3_key)
        logger.info(f"Uploaded config file to S3: {config_s3_key}")

    error = None
    # Check for failure: non-zero return code OR error traceback in stderr
    has_error_in_stderr = "Traceback (most recent call last):" in stderr_content
    is_failure = process.returncode != 0 or has_error_in_stderr

    if is_failure:
        if process.returncode != 0:
            error = (
                f"Command failed with exit code {process.returncode}: {stderr_content}"
            )
        else:
            error = f"Command failed with error in output: {stderr_content}"
        # Log CLI failure to Sentry
        logger.error(error)
        capture_exception_to_sentry(RuntimeError(error))

    return {
        "success": not is_failure,
        "total_simulations": len(simulation_results),
        "metrics": metrics_data,
        "simulation_results": simulation_results,
        "error": error,
    }


def _parse_simulation_directory(
    sim_dir: Path,
    output_dir: Path,
    s3_bucket: str,
    s3_prefix: str,
    uploaded_audio_files: set,
    include_presigned_urls: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Parse a single simulation directory and upload its audio files to S3.

    Args:
        sim_dir: Path to the simulation directory
        output_dir: Base output directory
        s3_bucket: S3 bucket name
        s3_prefix: S3 key prefix for uploading results
        uploaded_audio_files: Set to track uploaded audio files (modified in place)
        include_presigned_urls: If True, include presigned URLs in the result (for in-progress status)

    Returns:
        Dict with simulation result data, or None if parsing failed
    """
    sim_name = sim_dir.name
    eval_results_file = sim_dir / "evaluation_results.csv"
    transcript_file = sim_dir / "transcript.json"
    config_file = sim_dir / "config.json"

    eval_results = []
    if eval_results_file.exists():
        import csv

        with open(eval_results_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                eval_results.append(
                    {
                        "name": row.get("name"),
                        "value": row.get("value"),
                        "reasoning": row.get("reasoning", ""),
                    }
                )

    # Parse transcript.json if it exists
    transcript = None
    if transcript_file.exists():
        try:
            with open(transcript_file, "r", encoding="utf-8") as f:
                transcript = json.load(f)
        except Exception as e:
            logger.warning(f"Failed to parse transcript.json for {sim_name}: {e}")

    # Parse config.json to get persona and scenario data
    persona_data = None
    scenario_data = None
    if config_file.exists():
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                config_data = json.load(f)
                persona_data = config_data.get("persona")
                scenario_data = config_data.get("scenario")
        except Exception as e:
            logger.warning(f"Failed to parse config.json for {sim_name}: {e}")

    # Upload audio files and optionally generate presigned URLs
    audios_s3_path, conversation_wav_s3_key, audio_urls, conversation_wav_url = (
        _upload_audio_and_generate_urls(
            sim_dir, output_dir, s3_bucket, s3_prefix, uploaded_audio_files
        )
    )

    result = {
        "simulation_name": sim_name,
        "persona": persona_data,
        "scenario": scenario_data,
        "evaluation_results": eval_results,
        "transcript": transcript,
        "audios_s3_path": audios_s3_path,
        "conversation_wav_s3_key": conversation_wav_s3_key,
    }

    # Include presigned URLs only during in-progress status
    if include_presigned_urls:
        result["audio_urls"] = audio_urls if audio_urls else None
        result["conversation_wav_url"] = conversation_wav_url

    return result


def _is_simulation_complete(sim_dir: Path) -> bool:
    """
    Check if a simulation directory is complete.
    A simulation is considered complete when it has an evaluation_results.csv file,
    which is created after the evaluation step finishes.
    """
    eval_results_file = sim_dir / "evaluation_results.csv"
    return eval_results_file.exists()


def _is_simulation_started(sim_dir: Path) -> bool:
    """
    Check if a simulation has started (has config.json or transcript.json).
    """
    config_file = sim_dir / "config.json"
    transcript_file = sim_dir / "transcript.json"
    return config_file.exists() or transcript_file.exists()


def _upload_audio_and_generate_urls(
    sim_dir: Path,
    output_dir: Path,
    s3_bucket: str,
    s3_prefix: str,
    uploaded_audio_files: set,
) -> tuple:
    """
    Upload audio files for a simulation directory and generate presigned URLs.

    Args:
        sim_dir: Path to the simulation directory
        output_dir: Base output directory
        s3_bucket: S3 bucket name
        s3_prefix: S3 key prefix for uploading results
        uploaded_audio_files: Set to track uploaded audio files (modified in place)

    Returns:
        Tuple of (audios_s3_path, conversation_wav_s3_key, audio_urls, conversation_wav_url)
    """
    s3 = get_s3_client()
    sim_name = sim_dir.name
    audios_dir = sim_dir / "audios"

    audios_s3_path = None
    conversation_wav_s3_key = None
    audio_urls = []
    conversation_wav_url = None

    # Upload audios folder for this simulation to S3
    if audios_dir.exists() and audios_dir.is_dir():
        audios_s3_prefix = f"{s3_prefix}/{sim_name}/audios"
        audio_files_to_upload = []

        for audio_file in audios_dir.iterdir():
            if audio_file.is_file() and audio_file.suffix in {".wav", ".mp3", ".ogg"}:
                audio_files_to_upload.append(audio_file)

        # Sort audio files for consistent URL ordering
        def natural_sort_key(path: Path) -> tuple:
            filename = path.name
            parts = filename.split("_", 1)
            if len(parts) > 1 and parts[0].isdigit():
                return (int(parts[0]), parts[1])
            return (float("inf"), filename)

        audio_files_to_upload.sort(key=natural_sort_key)

        for audio_file in audio_files_to_upload:
            # Upload if not already uploaded
            if str(audio_file) not in uploaded_audio_files:
                relative_audio_path = audio_file.relative_to(output_dir)
                audio_s3_key = f"{s3_prefix}/{relative_audio_path}"
                s3.upload_file(str(audio_file), s3_bucket, audio_s3_key)
                uploaded_audio_files.add(str(audio_file))
                logger.info(
                    f"Uploaded audio file {audio_file.name} to S3: {audio_s3_key}"
                )

            # Generate presigned URL
            relative_audio_path = audio_file.relative_to(output_dir)
            audio_s3_key = f"{s3_prefix}/{relative_audio_path}"
            presigned_url = generate_presigned_download_url(
                audio_s3_key, bucket=s3_bucket
            )
            if presigned_url:
                audio_urls.append(presigned_url)

        if audio_files_to_upload:
            audios_s3_path = audios_s3_prefix

    # Upload conversation.wav if it exists
    conversation_wav_file = sim_dir / "conversation.wav"
    if conversation_wav_file.exists() and conversation_wav_file.is_file():
        conversation_wav_s3_key = f"{s3_prefix}/{sim_name}/conversation.wav"
        if str(conversation_wav_file) not in uploaded_audio_files:
            s3.upload_file(
                str(conversation_wav_file), s3_bucket, conversation_wav_s3_key
            )
            uploaded_audio_files.add(str(conversation_wav_file))
            logger.info(
                f"Uploaded conversation.wav for {sim_name} to s3://{s3_bucket}/{conversation_wav_s3_key}"
            )

        # Generate presigned URL
        conversation_wav_url = generate_presigned_download_url(
            conversation_wav_s3_key, bucket=s3_bucket
        )

    return audios_s3_path, conversation_wav_s3_key, audio_urls, conversation_wav_url


def _parse_voice_simulation_in_progress(
    sim_dir: Path,
    personas_list: Optional[List[Dict[str, Any]]] = None,
    scenarios_list: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Parse an in-progress voice simulation directory for intermediate results.
    Does NOT upload audio or generate presigned URLs - those are only available
    after evaluation_results are ready.

    Args:
        sim_dir: Path to the simulation directory
        personas_list: Optional list of personas from calibrate config (used as fallback)
        scenarios_list: Optional list of scenarios from calibrate config (used as fallback)

    Returns:
        Dict with simulation data (no audio URLs), or None if simulation hasn't started
    """
    if not sim_dir.exists():
        return None

    sim_name = sim_dir.name
    transcript_file = sim_dir / "transcript.json"
    config_file = sim_dir / "config.json"

    # Only return if simulation has started
    if not config_file.exists() and not transcript_file.exists():
        return None

    # Parse transcript.json if it exists
    transcript = None
    if transcript_file.exists():
        try:
            with open(transcript_file, "r", encoding="utf-8") as f:
                transcript = json.load(f)
        except Exception as e:
            logger.warning(f"Failed to parse transcript.json for {sim_name}: {e}")

    # Parse config.json to get persona and scenario data
    persona_data = None
    scenario_data = None
    if config_file.exists():
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                config_data = json.load(f)
                persona_data = config_data.get("persona")
                scenario_data = config_data.get("scenario")
        except Exception as e:
            logger.warning(f"Failed to parse config.json for {sim_name}: {e}")

    # Fallback: if persona/scenario not in config.json, extract from directory name
    if (persona_data is None or scenario_data is None) and (
        personas_list or scenarios_list
    ):
        persona_idx, scenario_idx = _extract_persona_scenario_indices(sim_name)
        if persona_data is None and personas_list and persona_idx is not None:
            if 0 <= persona_idx < len(personas_list):
                persona_data = personas_list[persona_idx]
        if scenario_data is None and scenarios_list and scenario_idx is not None:
            if 0 <= scenario_idx < len(scenarios_list):
                scenario_data = scenarios_list[scenario_idx]

    # Don't upload audio or generate URLs for in-progress simulations
    # Audio URLs are only returned after evaluation_results are available
    return {
        "simulation_name": sim_name,
        "persona": persona_data,
        "scenario": scenario_data,
        "evaluation_results": None,  # In-progress, no evaluation yet
        "transcript": transcript,
        "audios_s3_path": None,
        "conversation_wav_s3_key": None,
        "audio_urls": None,
        "conversation_wav_url": None,
    }


def _run_calibrate_voice_simulation(
    calibrate_config: Dict[str, Any],
    input_dir: Path,
    output_dir: Path,
    s3_bucket: str,
    s3_prefix: str,
    task_id: str,
    port: int,
    log_prefix: str = "Voice simulation",
) -> Dict[str, Any]:
    """
    Run calibrate agent simulation command and return parsed results.
    Updates the database incrementally as each simulation completes.

    Args:
        calibrate_config: The calibrate config dict (for voice simulations)
        input_dir: Directory to write config files
        output_dir: Directory to write output files
        s3_bucket: S3 bucket name
        s3_prefix: S3 key prefix for uploading results
        task_id: The task ID for updating the database with incremental results
        port: Port number for the simulation server
        log_prefix: Prefix for log messages

    Returns:
        Dict with keys: success, total_simulations, metrics, simulation_results, error, audios_s3_path
    """
    import time

    s3 = get_s3_client()

    # Resolve directories to absolute paths
    input_dir = input_dir.resolve()
    output_dir = output_dir.resolve()

    # Create directories
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Write config to input directory
    config_file_name = "simulation_config"
    config_file = input_dir / f"{config_file_name}.json"
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(calibrate_config, f, indent=2)

    # Run calibrate agent simulation command as a non-blocking process
    run_cmd = [
        "calibrate",
        "agent",
        "simulation",
        "-c",
        str(config_file),
        "-o",
        str(output_dir),
        "--port",
        str(port),
    ]

    logger.info(f"{log_prefix} command: {' '.join(run_cmd)}")

    # Open log files for stdout and stderr
    stdout_log_path = output_dir / "stdout.log"
    stderr_log_path = output_dir / "stderr.log"

    with (
        open(stdout_log_path, "w") as stdout_file,
        open(stderr_log_path, "w") as stderr_file,
    ):
        # Start the process without blocking, writing output to files
        process = subprocess.Popen(
            run_cmd,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,  # Detach from parent process group
            cwd=str(output_dir),
        )

        # Store the process PID, process group ID, and port in the job for cleanup on restart
        # The process group ID (pgid) equals the PID when start_new_session=True
        logger.info(f"{log_prefix}: Started process with PID {process.pid}")
        update_simulation_job(
            task_id,
            status=TaskStatus.IN_PROGRESS.value,
            details={
                "pid": process.pid,
                "pgid": process.pid,  # Same as PID when start_new_session=True
                "port": port,
            },
        )

        # Track processed simulations and uploaded files
        completed_simulations = set()
        uploaded_audio_files = set()
        completed_results = []  # Results for completed simulations

        # Get personas and scenarios lists for intermediate results
        personas_list = calibrate_config.get("personas", [])
        scenarios_list = calibrate_config.get("scenarios", [])
        expected_total = len(personas_list) * len(scenarios_list)
        logger.info(
            f"{log_prefix}: Expecting {expected_total} simulations ({len(personas_list)} personas x {len(scenarios_list)} scenarios)"
        )

        # Monitor for new simulation directories while the process runs
        poll_interval = 2  # seconds between checks
        prev_state = None  # Track state to avoid unnecessary DB updates

        while process.poll() is None:
            in_progress_results = []  # Rebuilt each iteration
            in_progress_transcript_lengths = (
                []
            )  # Track transcript lengths for change detection

            # Find all simulation directories
            for item in output_dir.iterdir():
                if item.is_dir() and item.name.startswith("simulation_persona_"):
                    if _is_simulation_complete(item):
                        # Simulation is complete
                        if item.name not in completed_simulations:
                            logger.info(
                                f"{log_prefix}: Found completed simulation directory: {item.name}"
                            )
                            # Parse and upload the completed simulation with presigned URLs (for in-progress display)
                            sim_result = _parse_simulation_directory(
                                sim_dir=item,
                                output_dir=output_dir,
                                s3_bucket=s3_bucket,
                                s3_prefix=s3_prefix,
                                uploaded_audio_files=uploaded_audio_files,
                                include_presigned_urls=True,  # Include URLs during in-progress
                            )
                            if sim_result:
                                completed_results.append(sim_result)
                                completed_simulations.add(item.name)
                    elif _is_simulation_started(item):
                        # Simulation in progress - get intermediate data (no audio URLs)
                        if item.name not in completed_simulations:
                            sim_result = _parse_voice_simulation_in_progress(
                                sim_dir=item,
                                personas_list=personas_list,
                                scenarios_list=scenarios_list,
                            )
                            if sim_result:
                                in_progress_results.append(sim_result)
                                # Track transcript length for change detection
                                transcript = sim_result.get("transcript") or []
                                in_progress_transcript_lengths.append(
                                    (item.name, len(transcript))
                                )

            # Build current state for change detection
            current_state = (
                len(completed_results),
                tuple(sorted(in_progress_transcript_lengths)),
            )

            # Only update DB if state changed
            if current_state != prev_state:
                all_results = completed_results + in_progress_results
                if all_results:
                    results_dict = {
                        "total_simulations": expected_total,
                        "completed_simulations": len(completed_results),
                        "simulation_results": all_results,
                        "results_s3_prefix": s3_prefix,
                    }
                    update_simulation_job(
                        task_id,
                        status=TaskStatus.IN_PROGRESS.value,
                        results=results_dict,
                    )
                    logger.info(
                        f"{log_prefix}: Updated DB with {len(completed_results)} completed + {len(in_progress_results)} in-progress simulations"
                    )
                prev_state = current_state

            time.sleep(poll_interval)

        # Process finished, wait for it to complete
        process.wait()

    # Read logs from files
    stdout = ""
    stderr = ""
    if stdout_log_path.exists():
        with open(stdout_log_path, "r") as f:
            stdout = f.read()
        if stdout:
            logger.info(f"{log_prefix} stdout: {stdout}")
    if stderr_log_path.exists():
        with open(stderr_log_path, "r") as f:
            stderr = f.read()
        if stderr:
            logger.info(f"{log_prefix} stderr: {stderr}")

    # Final pass: check for any remaining simulation directories that weren't processed
    # Don't include presigned URLs since status will be done
    for item in output_dir.iterdir():
        if (
            item.is_dir()
            and item.name.startswith("simulation_persona_")
            and item.name not in completed_simulations
        ):
            if _is_simulation_complete(item):
                logger.info(
                    f"{log_prefix}: Found remaining completed simulation directory: {item.name}"
                )
                sim_result = _parse_simulation_directory(
                    sim_dir=item,
                    output_dir=output_dir,
                    s3_bucket=s3_bucket,
                    s3_prefix=s3_prefix,
                    uploaded_audio_files=uploaded_audio_files,
                    include_presigned_urls=False,  # Don't include URLs for final done status
                )
                if sim_result:
                    completed_results.append(sim_result)
                    completed_simulations.add(item.name)

    # Strip presigned URLs from all completed_results before storing (for done status)
    # Only keep S3 paths for on-the-fly URL generation when status is fetched
    for sim_result in completed_results:
        sim_result.pop("audio_urls", None)
        sim_result.pop("conversation_wav_url", None)

    # Parse final results (metrics.json and results.csv)
    metrics_data = None
    metrics_file = output_dir / "metrics.json"
    results_file = output_dir / "results.csv"

    if metrics_file.exists():
        with open(metrics_file, "r", encoding="utf-8") as f:
            metrics_data = json.load(f)

    # Parse results.csv for aggregated scores
    if results_file.exists():
        import csv

        with open(results_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            results_data = list(reader)

    # Upload all other results to S3 (excluding audios which are already uploaded)
    for root, dirs, files in os.walk(output_dir):
        # Skip audios directories as they're already uploaded
        if "audios" in root.split(os.sep):
            continue
        for file in files:
            local_file_path = Path(root) / file
            # Skip audio files that were already uploaded
            if str(local_file_path) in uploaded_audio_files:
                continue
            relative_path = local_file_path.relative_to(output_dir)
            s3_key = f"{s3_prefix}/{relative_path}"
            s3.upload_file(str(local_file_path), s3_bucket, s3_key)

    # Upload the config file to S3
    if config_file.exists():
        config_s3_key = f"{s3_prefix}/simulation_config.json"
        s3.upload_file(str(config_file), s3_bucket, config_s3_key)
        logger.info(f"Uploaded config file to S3: {config_s3_key}")

    error = None
    # Check for failure: non-zero return code OR error traceback in stderr
    has_error_in_stderr = "Traceback (most recent call last):" in stderr
    is_failure = process.returncode != 0 or has_error_in_stderr

    if is_failure:
        if process.returncode != 0:
            error = f"Command failed with exit code {process.returncode}: {stderr}"
        else:
            error = f"Command failed with error in output: {stderr}"
        # Log CLI failure to Sentry
        logger.error(error)
        capture_exception_to_sentry(RuntimeError(error))

    return {
        "success": not is_failure,
        "total_simulations": len(completed_results),
        "metrics": metrics_data,
        "simulation_results": completed_results,
        "error": error,
    }


def run_simulation_task(
    task_id: str,
    agent: Dict[str, Any],
    personas: List[Dict[str, Any]],
    scenarios: List[Dict[str, Any]],
    metrics: List[Dict[str, Any]],
    s3_bucket: str,
    simulation_type: str = "text",
):
    """Run the simulation in the background (text or voice)."""
    reserved_port = None  # Track reserved port for cleanup
    try:
        logger.info(
            f"Running {simulation_type} simulation task {task_id} for agent {agent['uuid']} "
            f"with {len(personas)} persona(s), {len(scenarios)} scenario(s), "
            f"and {len(metrics)} metric(s)"
        )
        update_simulation_job(task_id, status=TaskStatus.IN_PROGRESS.value)

        # Reserve a port for voice simulations
        if simulation_type == "voice":
            reserved_port = reserve_port(task_id, start_port=8765)
            logger.info(f"Reserved port {reserved_port} for voice simulation {task_id}")

        # Create temporary directory for processing (automatically cleaned up after use)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            try:
                # Build calibrate config
                calibrate_config = _build_calibrate_simulation_config(
                    agent, personas, scenarios, metrics, simulation_type=simulation_type
                )

                # Create input and output directories
                input_dir = temp_path / "input"
                output_dir = temp_path / "output"

                # Run calibrate simulation based on type
                results_prefix = f"simulations/runs/{task_id}"
                if simulation_type == "voice":
                    result = _run_calibrate_voice_simulation(
                        calibrate_config=calibrate_config,
                        input_dir=input_dir,
                        output_dir=output_dir,
                        s3_bucket=s3_bucket,
                        s3_prefix=results_prefix,
                        task_id=task_id,
                        port=reserved_port,
                        log_prefix=f"Voice simulation {task_id}",
                    )
                else:
                    model_to_use = calibrate_config["params"]["model"]
                    result = _run_calibrate_text_simulation(
                        model=model_to_use,
                        calibrate_config=calibrate_config,
                        input_dir=input_dir,
                        output_dir=output_dir,
                        s3_bucket=s3_bucket,
                        s3_prefix=results_prefix,
                        task_id=task_id,
                        log_prefix=f"Chat simulation {task_id}",
                    )

                # Prepare results dict
                results_dict = {
                    "total_simulations": result["total_simulations"],
                    "metrics": result["metrics"],
                    "simulation_results": result["simulation_results"],
                    "results_s3_prefix": results_prefix,
                    "error": result.get("error"),
                }

                # Determine final status based on success
                final_status = (
                    TaskStatus.DONE.value
                    if result["success"]
                    else TaskStatus.FAILED.value
                )

                # Update job with results
                update_simulation_job(
                    task_id,
                    status=final_status,
                    results=results_dict,
                )

                logger.info(
                    f"{simulation_type.capitalize()} simulation task {task_id} completed: "
                    f"{result['total_simulations']} simulation(s) run, status={final_status}"
                )

            except Exception as e:
                traceback.print_exc()
                capture_exception_to_sentry(e)
                update_simulation_job(
                    task_id,
                    status=TaskStatus.FAILED.value,
                    results={"error": f"Unexpected error during simulation: {str(e)}"},
                )
        # Temporary directory is automatically cleaned up here

    except Exception as e:
        traceback.print_exc()
        capture_exception_to_sentry(e)
        update_simulation_job(
            task_id,
            status=TaskStatus.FAILED.value,
            results={"error": f"Task failed: {str(e)}"},
        )
    finally:
        # Release reserved port
        if reserved_port is not None:
            release_port(reserved_port)

        # Try to start the next queued job
        try_start_queued_simulation_job(SIMULATION_JOB_TYPES)


@router.post("/{simulation_uuid}/run", response_model=TaskCreateResponse)
async def run_simulation_endpoint(
    simulation_uuid: str,
    request: RunSimulationRequest,
    user_id: str = Depends(get_current_user_id),
):
    """
    Run a simulation with personas, scenarios, and metrics.

    This starts a background task that runs the calibrate LLM simulations command
    with the agent's config and the simulation's personas, scenarios, and metrics.

    Uses the agent linked to the simulation and its LLM model configuration.

    Returns a task ID that can be used to poll for status and results.
    """
    # Verify simulation exists
    simulation = get_simulation(simulation_uuid)
    if not simulation:
        raise HTTPException(status_code=404, detail="Simulation not found")

    # Verify user owns this simulation
    if simulation.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Get agent from simulation
    agent_uuid = simulation.get("agent_id")
    if not agent_uuid:
        raise HTTPException(
            status_code=400,
            detail="No agent linked to this simulation. Link an agent to the simulation first.",
        )

    # Verify agent exists
    agent = get_agent(agent_uuid)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Get linked entities
    personas = get_personas_for_simulation(simulation_uuid)
    scenarios = get_scenarios_for_simulation(simulation_uuid)
    metrics = get_metrics_for_simulation(simulation_uuid)

    if not personas:
        raise HTTPException(
            status_code=400,
            detail="Simulation has no personas. Add at least one persona.",
        )

    if not scenarios:
        raise HTTPException(
            status_code=400,
            detail="Simulation has no scenarios. Add at least one scenario.",
        )

    # Get S3 configuration
    try:
        s3_bucket = get_s3_output_config()
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Check if we can start immediately or need to queue
    can_start = can_start_simulation_job(SIMULATION_JOB_TYPES)
    initial_status = (
        TaskStatus.IN_PROGRESS.value if can_start else TaskStatus.QUEUED.value
    )

    # Create job in database with details for recovery
    job_id = create_simulation_job(
        simulation_id=simulation_uuid,
        job_type=request.type,
        status=initial_status,
        details={
            "simulation_uuid": simulation_uuid,
            "agent_uuid": agent_uuid,
            "s3_bucket": s3_bucket,
        },
        results=None,
    )

    if can_start:
        # Start background task in a separate thread
        thread = threading.Thread(
            target=run_simulation_task,
            args=(job_id, agent, personas, scenarios, metrics, s3_bucket, request.type),
            daemon=True,
        )
        thread.start()
        logger.info(f"Started {request.type} simulation job {job_id} immediately")
    else:
        logger.info(f"Queued {request.type} simulation job {job_id}")

    return TaskCreateResponse(task_id=job_id, status=initial_status)


@router.delete("/run/{job_uuid}")
async def delete_simulation_job_endpoint(
    job_uuid: str, user_id: str = Depends(get_current_user_id)
):
    """Delete a simulation job. Only the owner can delete their jobs."""
    # Check if job exists
    simulation_job = get_simulation_job(job_uuid)
    if not simulation_job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Check ownership via simulation
    simulation_id = simulation_job.get("simulation_id")
    if simulation_id:
        simulation = get_simulation(simulation_id)
        if not simulation or simulation.get("user_id") != user_id:
            raise HTTPException(status_code=404, detail="Job not found")
    else:
        raise HTTPException(status_code=404, detail="Job not found")

    # Check if this was a running job (to trigger next queued job after delete)
    was_running = simulation_job.get("status") == TaskStatus.IN_PROGRESS.value
    details = simulation_job.get("details") or {}

    # Kill running process if job is in progress
    if was_running:
        pid = details.get("pid") or details.get("pgid")
        if pid:
            kill_process_group(pid, job_uuid)

        # Release port if allocated
        port = details.get("port")
        if port:
            release_port(port)

    # Delete the job
    deleted = delete_simulation_job(job_uuid)
    if not deleted:
        raise HTTPException(status_code=404, detail="Job not found")

    # If the deleted job was running, try to start the next queued job
    if was_running:
        try_start_queued_simulation_job(SIMULATION_JOB_TYPES)

    return {"message": "Simulation job deleted successfully"}
