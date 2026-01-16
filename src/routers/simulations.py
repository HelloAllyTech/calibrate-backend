import os
import json
import subprocess
import traceback
import threading
import logging
import tempfile
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, HTTPException
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
)
from utils import (
    TaskStatus,
    TaskCreateResponse,
    get_s3_client,
    get_s3_output_config,
)

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/simulations", tags=["simulations"])


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
        expiration = 3600  # 1 hour expiration
        presigned_urls = []
        for audio_key in audio_files:
            try:
                presigned_url = s3.generate_presigned_url(
                    "get_object",
                    Params={
                        "Bucket": s3_bucket,
                        "Key": audio_key,
                    },
                    ExpiresIn=expiration,
                )
                presigned_urls.append(presigned_url)
                logger.info(f"Generated presigned URL for {audio_key}")
            except Exception as e:
                logger.warning(
                    f"Failed to generate presigned URL for {audio_key}: {str(e)}"
                )
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
    type: str = Field(..., description="Type of simulation run: 'chat' or 'voice'")

    @field_validator("type")
    @classmethod
    def validate_type(cls, v):
        if v not in ["chat", "voice"]:
            raise ValueError("type must be either 'chat' or 'voice'")
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
async def create_simulation_endpoint(simulation: SimulationCreate):
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
        name=simulation.name, agent_id=simulation.agent_uuid
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
async def list_simulations():
    """List all simulations with their linked agents."""
    simulations = get_all_simulations()
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
async def get_simulation_run_status(task_id: str):
    """
    Get the status of a simulation run.

    Returns the current status and, if done, the simulation results.
    """
    job = get_simulation_job(task_id)
    if not job:
        raise HTTPException(status_code=404, detail="Task not found")

    # Calculate run index based on creation order
    simulation_id = job.get("simulation_id")
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

    results = job.get("results") or {}
    simulation_results = results.get("simulation_results") or []

    # If this is a voice simulation, generate presigned URLs for audio files
    if job.get("type") == "voice" and simulation_results:
        try:
            s3_bucket = get_s3_output_config()
            # Update each simulation result with audio URLs if audios_s3_path (S3 key prefix) is present
            for sim_result in simulation_results:
                audios_s3_key_prefix = sim_result.get("audios_s3_path")
                if audios_s3_key_prefix:
                    audio_urls = _get_audio_urls_from_s3_key(
                        audios_s3_key_prefix, s3_bucket
                    )
                    sim_result["audio_urls"] = audio_urls
                    logger.info(
                        f"Generated {len(audio_urls)} presigned URLs for simulation {sim_result.get('simulation_name')}"
                    )
        except Exception as e:
            logger.warning(f"Failed to generate audio URLs: {str(e)}")
            # Continue without audio URLs if generation fails

    return SimulationRunStatusResponse(
        task_id=task_id,
        name=run_name,
        status=job["status"],
        type=job["type"],
        updated_at=job["updated_at"],
        total_simulations=results.get("total_simulations"),
        completed_simulations=results.get("completed_simulations"),
        metrics=results.get("metrics"),
        simulation_results=simulation_results,
        error=results.get("error"),
    )


@router.get("/{simulation_uuid}/runs", response_model=SimulationRunsResponse)
async def get_simulation_runs(simulation_uuid: str):
    """
    Get all runs for a simulation.

    Returns a list of all simulation runs with their UUID, status, type, and name.
    """
    # Verify simulation exists
    simulation = get_simulation(simulation_uuid)
    if not simulation:
        raise HTTPException(status_code=404, detail="Simulation not found")

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
async def get_simulation_endpoint(simulation_uuid: str):
    """Get a simulation by UUID with all linked agent, personas, scenarios, and metrics."""
    simulation = get_simulation(simulation_uuid)
    if not simulation:
        raise HTTPException(status_code=404, detail="Simulation not found")

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
    simulation_uuid: str, simulation: SimulationUpdate
):
    """Update a simulation with optional linked agent, personas, scenarios, and metrics."""
    existing_simulation = get_simulation(simulation_uuid)
    if not existing_simulation:
        raise HTTPException(status_code=404, detail="Simulation not found")

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
async def delete_simulation_endpoint(simulation_uuid: str):
    """Delete a simulation."""
    deleted = delete_simulation(simulation_uuid)
    if not deleted:
        raise HTTPException(status_code=404, detail="Simulation not found")
    return {"message": "Simulation deleted successfully"}


# ============ Run Simulation API ============


def _build_pense_simulation_config(
    agent: Dict[str, Any],
    personas: List[Dict[str, Any]],
    scenarios: List[Dict[str, Any]],
    metrics: List[Dict[str, Any]],
    simulation_type: str = "chat",
) -> Dict[str, Any]:
    """
    Build the pense simulation config from agent, personas, scenarios, and metrics.

    Args:
        agent: Agent dict with config containing system_prompt and llm.model
        personas: List of persona dicts with description and config (containing gender, language)
        scenarios: List of scenario dicts with description
        metrics: List of metric dicts with name and description (for evaluation_criteria)
        simulation_type: Type of simulation - "chat" or "voice"
    """
    agent_config = agent.get("config") or {}

    # Get model from agent config
    llm_config = agent_config.get("llm", {})
    model = llm_config.get("model", "gpt-4.1")

    # Get tools from agent_tools table
    agent_tools = get_tools_for_agent(agent["uuid"])
    tool_configs = [
        {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool.get("config", {}).get("parameters", []),
        }
        for tool in agent_tools
    ]

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

    if simulation_type == "chat":
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


def _run_pense_chat_simulation(
    model: str,
    pense_config: Dict[str, Any],
    output_dir: Path,
    s3_bucket: str,
    s3_prefix: str,
    log_prefix: str = "LLM simulation",
) -> Dict[str, Any]:
    """
    Run pense llm simulations run command and return parsed results.

    Args:
        model: Model name to use
        pense_config: The pense config dict
        output_dir: Directory to write output files
        s3_bucket: S3 bucket name
        s3_prefix: S3 key prefix for uploading results
        log_prefix: Prefix for log messages

    Returns:
        Dict with keys: success, total_simulations, metrics, simulation_results, error
    """
    s3 = get_s3_client()

    # Update config with model
    config = pense_config.copy()
    config["params"] = {"model": model}

    # Resolve output directory to absolute path
    output_dir = output_dir.resolve()

    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)

    # Write config to temp file
    config_file_name = "simulation_config"
    config_file = output_dir / f"{config_file_name}.json"
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    # Run pense llm simulations run command
    # Use absolute paths for config and output
    run_cmd = [
        "pense",
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

    result = subprocess.run(
        run_cmd,
        capture_output=True,
        text=True,
        cwd=str(output_dir),
    )

    if result.stdout:
        logger.info(f"{log_prefix} stdout: {result.stdout}")
    if result.stderr:
        logger.info(f"{log_prefix} stderr: {result.stderr}")

    # Parse results
    metrics_data = None
    results_data = None
    simulation_results = []

    # Find metrics.json and results.csv files
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

    # Find simulation directories and parse their results
    # Search recursively for simulation directories (they might be nested)
    for root, dirs, files in os.walk(output_dir):
        for dir_name in dirs:
            if dir_name.startswith("simulation_persona_"):
                sim_dir = Path(root) / dir_name
                sim_name = dir_name
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
                        logger.warning(
                            f"Failed to parse transcript.json for {sim_name}: {e}"
                        )

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
                        logger.warning(
                            f"Failed to parse config.json for {sim_name}: {e}"
                        )

                simulation_results.append(
                    {
                        "simulation_name": sim_name,
                        "persona": persona_data,
                        "scenario": scenario_data,
                        "evaluation_results": eval_results,
                        "transcript": transcript,
                    }
                )

    # Upload results to S3
    for root, dirs, files in os.walk(output_dir):
        for file in files:
            local_file_path = Path(root) / file
            relative_path = local_file_path.relative_to(output_dir)
            s3_key = f"{s3_prefix}/{relative_path}"
            s3.upload_file(str(local_file_path), s3_bucket, s3_key)

    error = None
    if result.returncode != 0:
        error = f"Command failed: {result.stderr}"

    return {
        "success": result.returncode == 0,
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
) -> Optional[Dict[str, Any]]:
    """
    Parse a single simulation directory and upload its audio files to S3.

    Args:
        sim_dir: Path to the simulation directory
        output_dir: Base output directory
        s3_bucket: S3 bucket name
        s3_prefix: S3 key prefix for uploading results
        uploaded_audio_files: Set to track uploaded audio files (modified in place)

    Returns:
        Dict with simulation result data, or None if parsing failed
    """
    s3 = get_s3_client()
    sim_name = sim_dir.name
    eval_results_file = sim_dir / "evaluation_results.csv"
    transcript_file = sim_dir / "transcript.json"
    config_file = sim_dir / "config.json"
    audios_dir = sim_dir / "audios"

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

    # Upload audios folder for this simulation to S3
    audios_s3_path = None
    if audios_dir.exists() and audios_dir.is_dir():
        audios_s3_prefix = f"{s3_prefix}/{sim_name}/audios"
        audio_files_uploaded = 0
        for audio_file in audios_dir.iterdir():
            if audio_file.is_file() and (
                audio_file.suffix == ".wav"
                or audio_file.suffix == ".mp3"
                or audio_file.suffix == ".ogg"
            ):
                # Skip if already uploaded
                if str(audio_file) in uploaded_audio_files:
                    continue
                relative_audio_path = audio_file.relative_to(output_dir)
                audio_s3_key = f"{s3_prefix}/{relative_audio_path}"
                s3.upload_file(str(audio_file), s3_bucket, audio_s3_key)
                uploaded_audio_files.add(str(audio_file))
                audio_files_uploaded += 1
                logger.info(
                    f"Uploaded audio file {audio_file.name} to S3: {audio_s3_key}"
                )
        if audio_files_uploaded > 0:
            # Store just the S3 key prefix, not the full s3:// path
            audios_s3_path = audios_s3_prefix
            logger.info(
                f"Uploaded {audio_files_uploaded} audio file(s) for {sim_name} to s3://{s3_bucket}/{audios_s3_prefix}"
            )

    return {
        "simulation_name": sim_name,
        "persona": persona_data,
        "scenario": scenario_data,
        "evaluation_results": eval_results,
        "transcript": transcript,
        "audios_s3_path": audios_s3_path,
    }


def _is_simulation_complete(sim_dir: Path) -> bool:
    """
    Check if a simulation directory is complete.
    A simulation is considered complete when it has an evaluation_results.csv file,
    which is created after the evaluation step finishes.
    """
    eval_results_file = sim_dir / "evaluation_results.csv"
    return eval_results_file.exists()


def _run_pense_voice_simulation(
    pense_config: Dict[str, Any],
    output_dir: Path,
    s3_bucket: str,
    s3_prefix: str,
    task_id: str,
    log_prefix: str = "Voice simulation",
) -> Dict[str, Any]:
    """
    Run pense agent simulation command and return parsed results.
    Updates the database incrementally as each simulation completes.

    Args:
        pense_config: The pense config dict (for voice simulations)
        output_dir: Directory to write output files
        s3_bucket: S3 bucket name
        s3_prefix: S3 key prefix for uploading results
        task_id: The task ID for updating the database with incremental results
        log_prefix: Prefix for log messages

    Returns:
        Dict with keys: success, total_simulations, metrics, simulation_results, error, audios_s3_path
    """
    import time

    s3 = get_s3_client()

    # Resolve output directory to absolute path
    output_dir = output_dir.resolve()

    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)

    # Write config to temp file
    config_file_name = "simulation_config"
    config_file = output_dir / f"{config_file_name}.json"
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(pense_config, f, indent=2)

    # Run pense agent simulation command as a non-blocking process
    run_cmd = [
        "pense",
        "agent",
        "simulation",
        "-c",
        str(config_file),
        "-o",
        str(output_dir),
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

        # Store the process PID and process group ID in the job for cleanup on restart
        # The process group ID (pgid) equals the PID when start_new_session=True
        logger.info(f"{log_prefix}: Started process with PID {process.pid}")
        update_simulation_job(
            task_id,
            status=TaskStatus.IN_PROGRESS.value,
            details={
                "pid": process.pid,
                "pgid": process.pid,  # Same as PID when start_new_session=True
            },
        )

        # Track processed simulations and uploaded files
        processed_simulations = set()
        uploaded_audio_files = set()
        simulation_results = []

        # Calculate expected number of simulations
        num_personas = len(pense_config.get("personas", []))
        num_scenarios = len(pense_config.get("scenarios", []))
        expected_total = num_personas * num_scenarios
        logger.info(
            f"{log_prefix}: Expecting {expected_total} simulations ({num_personas} personas x {num_scenarios} scenarios)"
        )

        # Monitor for new simulation directories while the process runs
        poll_interval = 2  # seconds between checks
        while process.poll() is None:
            # Find all simulation directories
            for item in output_dir.iterdir():
                if (
                    item.is_dir()
                    and item.name.startswith("simulation_persona_")
                    and item.name not in processed_simulations
                ):
                    # Check if this simulation is complete
                    if _is_simulation_complete(item):
                        logger.info(
                            f"{log_prefix}: Found completed simulation directory: {item.name}"
                        )
                        # Parse the simulation directory
                        sim_result = _parse_simulation_directory(
                            sim_dir=item,
                            output_dir=output_dir,
                            s3_bucket=s3_bucket,
                            s3_prefix=s3_prefix,
                            uploaded_audio_files=uploaded_audio_files,
                        )
                        if sim_result:
                            simulation_results.append(sim_result)
                            processed_simulations.add(item.name)

                            # Update the database with incremental results
                            results_dict = {
                                "total_simulations": expected_total,
                                "completed_simulations": len(simulation_results),
                                "simulation_results": simulation_results,
                                "results_s3_prefix": s3_prefix,
                            }
                            update_simulation_job(
                                task_id,
                                status=TaskStatus.IN_PROGRESS.value,
                                results=results_dict,
                            )
                            logger.info(
                                f"{log_prefix}: Updated DB with {len(simulation_results)}/{expected_total} completed simulations"
                            )

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
    for item in output_dir.iterdir():
        if (
            item.is_dir()
            and item.name.startswith("simulation_persona_")
            and item.name not in processed_simulations
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
                )
                if sim_result:
                    simulation_results.append(sim_result)
                    processed_simulations.add(item.name)

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

    error = None
    if process.returncode != 0:
        error = f"Command failed: {stderr}"

    return {
        "success": process.returncode == 0,
        "total_simulations": len(simulation_results),
        "metrics": metrics_data,
        "simulation_results": simulation_results,
        "error": error,
    }


def run_simulation_task(
    task_id: str,
    agent: Dict[str, Any],
    personas: List[Dict[str, Any]],
    scenarios: List[Dict[str, Any]],
    metrics: List[Dict[str, Any]],
    s3_bucket: str,
    simulation_type: str = "chat",
):
    """Run the simulation in the background (chat or voice)."""
    try:
        logger.info(
            f"Running {simulation_type} simulation task {task_id} for agent {agent['uuid']} "
            f"with {len(personas)} persona(s), {len(scenarios)} scenario(s), "
            f"and {len(metrics)} metric(s)"
        )
        update_simulation_job(task_id, status=TaskStatus.IN_PROGRESS.value)

        # Create temporary directory for processing (automatically cleaned up after use)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            try:
                # Build pense config
                pense_config = _build_pense_simulation_config(
                    agent, personas, scenarios, metrics, simulation_type=simulation_type
                )

                # Create output directory
                output_dir = temp_path / "output"
                output_dir = output_dir.resolve()

                # Run pense simulation based on type
                results_prefix = f"simulations/runs/{task_id}"
                if simulation_type == "voice":
                    result = _run_pense_voice_simulation(
                        pense_config=pense_config,
                        output_dir=output_dir,
                        s3_bucket=s3_bucket,
                        s3_prefix=results_prefix,
                        task_id=task_id,
                        log_prefix=f"Voice simulation {task_id}",
                    )
                else:
                    model_to_use = pense_config["params"]["model"]
                    result = _run_pense_chat_simulation(
                        model=model_to_use,
                        pense_config=pense_config,
                        output_dir=output_dir,
                        s3_bucket=s3_bucket,
                        s3_prefix=results_prefix,
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

                # Update job with results
                update_simulation_job(
                    task_id,
                    status=TaskStatus.DONE.value,
                    results=results_dict,
                )

                logger.info(
                    f"{simulation_type.capitalize()} simulation task {task_id} completed: "
                    f"{result['total_simulations']} simulation(s) run"
                )

            except Exception as e:
                traceback.print_exc()
                update_simulation_job(
                    task_id,
                    status=TaskStatus.DONE.value,
                    results={"error": f"Unexpected error during simulation: {str(e)}"},
                )
        # Temporary directory is automatically cleaned up here

    except Exception as e:
        traceback.print_exc()
        update_simulation_job(
            task_id,
            status=TaskStatus.DONE.value,
            results={"error": f"Task failed: {str(e)}"},
        )


@router.post("/{simulation_uuid}/run", response_model=TaskCreateResponse)
async def run_simulation_endpoint(simulation_uuid: str, request: RunSimulationRequest):
    """
    Run a simulation with personas, scenarios, and metrics.

    This starts a background task that runs the pense LLM simulations command
    with the agent's config and the simulation's personas, scenarios, and metrics.

    Uses the agent linked to the simulation and its LLM model configuration.

    Returns a task ID that can be used to poll for status and results.
    """
    # Verify simulation exists
    simulation = get_simulation(simulation_uuid)
    if not simulation:
        raise HTTPException(status_code=404, detail="Simulation not found")

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

    # Create job in database with details for recovery
    job_id = create_simulation_job(
        simulation_id=simulation_uuid,
        job_type=request.type,
        status=TaskStatus.IN_PROGRESS.value,
        details={
            "simulation_uuid": simulation_uuid,
            "agent_uuid": agent_uuid,
            "s3_bucket": s3_bucket,
        },
        results=None,
    )

    # Start background task in a separate thread
    thread = threading.Thread(
        target=run_simulation_task,
        args=(job_id, agent, personas, scenarios, metrics, s3_bucket, request.type),
        daemon=True,
    )
    thread.start()

    return TaskCreateResponse(task_id=job_id, status=TaskStatus.IN_PROGRESS.value)
