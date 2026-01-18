import os
import json
import subprocess
import tempfile
import traceback
import threading
import concurrent.futures
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlite3 import IntegrityError

from db import (
    add_test_to_agent,
    remove_test_from_agent,
    get_tests_for_agent,
    get_agents_for_test,
    get_agent_test_link,
    get_all_agent_tests,
    get_agent,
    get_test,
    get_tools_for_agent,
    create_agent_test_job,
    get_agent_test_job,
    update_agent_test_job,
    get_agent_test_jobs_for_agent,
)
from utils import (
    TaskStatus,
    TaskCreateResponse,
    get_s3_client,
    get_s3_output_config,
    can_start_agent_test_job,
    try_start_queued_agent_test_job,
    register_job_starter,
)

# Job types that share the same queue
AGENT_TEST_JOB_TYPES = ["llm-unit-test", "llm-benchmark"]


def _start_llm_unit_test_job_from_queue(job: dict) -> bool:
    """Start an LLM unit test job from the queue."""
    job_id = job["uuid"]
    details = job.get("details", {})

    agent_uuid = details.get("agent_uuid")
    test_uuids = details.get("test_uuids", [])
    s3_bucket = details.get("s3_bucket", "")

    # Get agent and tests
    agent = get_agent(agent_uuid)
    if not agent:
        return False

    tests = []
    for test_uuid in test_uuids:
        test = get_test(test_uuid)
        if test:
            tests.append(test)

    if not tests:
        return False

    # Start background task in a separate thread
    thread = threading.Thread(
        target=run_llm_test_task,
        args=(job_id, agent, tests, s3_bucket),
        daemon=True,
    )
    thread.start()

    return True


def _start_llm_benchmark_job_from_queue(job: dict) -> bool:
    """Start an LLM benchmark job from the queue."""
    job_id = job["uuid"]
    details = job.get("details", {})

    agent_uuid = details.get("agent_uuid")
    test_uuids = details.get("test_uuids", [])
    models = details.get("models", [])
    s3_bucket = details.get("s3_bucket", "")

    # Get agent and tests
    agent = get_agent(agent_uuid)
    if not agent:
        return False

    tests = []
    for test_uuid in test_uuids:
        test = get_test(test_uuid)
        if test:
            tests.append(test)

    if not tests or not models:
        return False

    # Start background task in a separate thread
    thread = threading.Thread(
        target=run_benchmark_task,
        args=(job_id, agent, tests, models, s3_bucket),
        daemon=True,
    )
    thread.start()

    return True


# Register the job starters for agent test jobs
register_job_starter("llm-unit-test", _start_llm_unit_test_job_from_queue)
register_job_starter("llm-benchmark", _start_llm_benchmark_job_from_queue)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent-tests", tags=["agent-tests"])


class AgentTestsCreate(BaseModel):
    agent_uuid: str
    test_uuids: List[str]


class AgentTestDelete(BaseModel):
    agent_uuid: str
    test_uuid: str


class AgentTestResponse(BaseModel):
    id: int
    agent_id: str
    test_id: str
    created_at: str


class AgentTestsCreateResponse(BaseModel):
    ids: List[int]
    message: str


class TestResponse(BaseModel):
    uuid: str
    name: str
    type: str
    config: Dict[str, Any] | None = None
    created_at: str
    updated_at: str


class AgentResponse(BaseModel):
    uuid: str
    name: str
    config: Dict[str, Any] | None = None
    created_at: str
    updated_at: str


class RunTestRequest(BaseModel):
    test_uuids: List[str]


class ToolCallOutput(BaseModel):
    tool: str
    arguments: Optional[Dict[str, Any]] = None


class TestOutput(BaseModel):
    response: Optional[str] = None
    tool_calls: Optional[List[ToolCallOutput]] = None


class TestCaseResult(BaseModel):
    """Result for a single test case matching pense results.json structure"""

    passed: bool
    output: TestOutput
    test_case: Dict[str, Any]


class TestRunStatusResponse(BaseModel):
    task_id: str
    status: str
    total_tests: Optional[int] = None
    passed: Optional[int] = None
    failed: Optional[int] = None
    results: Optional[List[TestCaseResult]] = None
    results_s3_prefix: Optional[str] = None
    error: Optional[str] = None


class AgentTestRunListItem(BaseModel):
    uuid: str
    name: str  # Format: "Run {index}" or "Benchmark {index}"
    status: str
    type: str
    updated_at: str
    # Unit test results (for llm-unit-test type)
    total_tests: Optional[int] = None
    passed: Optional[int] = None
    failed: Optional[int] = None
    results: Optional[List[TestCaseResult]] = None
    # Benchmark results (for llm-benchmark type)
    model_results: Optional[List[Dict[str, Any]]] = None
    leaderboard_summary: Optional[List[Dict[str, Any]]] = None
    # Common fields
    results_s3_prefix: Optional[str] = None
    error: Optional[str] = None


class AgentTestRunsResponse(BaseModel):
    runs: List[AgentTestRunListItem]


@router.post("", response_model=AgentTestsCreateResponse)
async def create_agent_test_links(agent_tests: AgentTestsCreate):
    """Add tests to an agent."""
    # Verify agent exists
    agent = get_agent(agent_tests.agent_uuid)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Verify all tests exist
    for test_uuid in agent_tests.test_uuids:
        test = get_test(test_uuid)
        if not test:
            raise HTTPException(status_code=404, detail=f"Test {test_uuid} not found")

    link_ids = []
    for test_uuid in agent_tests.test_uuids:
        # Check if link already exists
        existing = get_agent_test_link(agent_tests.agent_uuid, test_uuid)
        if existing:
            continue  # Skip already linked tests

        try:
            link_id = add_test_to_agent(
                agent_id=agent_tests.agent_uuid,
                test_id=test_uuid,
            )
            link_ids.append(link_id)
        except IntegrityError:
            continue  # Skip if already linked

    return AgentTestsCreateResponse(
        ids=link_ids, message="Tests added to agent successfully"
    )


@router.get("", response_model=List[AgentTestResponse])
async def list_agent_tests():
    """List all agent-test links."""
    links = get_all_agent_tests()
    return links


@router.get("/agent/{agent_uuid}/tests", response_model=List[TestResponse])
async def get_agent_tests_endpoint(agent_uuid: str):
    """Get all tests for a specific agent."""
    # Verify agent exists
    agent = get_agent(agent_uuid)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    tests = get_tests_for_agent(agent_uuid)
    return tests


@router.get("/agent/{agent_uuid}/runs", response_model=AgentTestRunsResponse)
async def get_agent_test_runs(agent_uuid: str):
    """
    Get all test runs for an agent.

    Returns a list of all test runs (unit tests and benchmarks) with their UUID, status, type, name, and results.
    """
    # Verify agent exists
    agent = get_agent(agent_uuid)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Get all jobs for this agent
    jobs = get_agent_test_jobs_for_agent(agent_uuid)

    # Group jobs by type to generate run names
    unit_test_count = 0
    benchmark_count = 0

    runs = []
    for job in jobs:
        job_type = job.get("type", "")
        if job_type == "llm-unit-test":
            unit_test_count += 1
            name = f"Run {unit_test_count}"
        elif job_type == "llm-benchmark":
            benchmark_count += 1
            name = f"Benchmark {benchmark_count}"
        else:
            name = f"Job {len(runs) + 1}"

        # Extract results from job
        job_results = job.get("results") or {}

        run_item = AgentTestRunListItem(
            uuid=job["uuid"],
            name=name,
            status=job["status"],
            type=job_type,
            updated_at=job.get("updated_at", job.get("created_at", "")),
            # Unit test results
            total_tests=job_results.get("total_tests"),
            passed=job_results.get("passed"),
            failed=job_results.get("failed"),
            results=job_results.get("test_results"),
            # Benchmark results
            model_results=job_results.get("model_results"),
            leaderboard_summary=job_results.get("leaderboard_summary"),
            # Common fields
            results_s3_prefix=job_results.get("results_s3_prefix"),
            error=job_results.get("error"),
        )
        runs.append(run_item)

    return AgentTestRunsResponse(runs=runs)


@router.get("/test/{test_uuid}/agents", response_model=List[AgentResponse])
async def get_test_agents(test_uuid: str):
    """Get all agents for a specific test."""
    # Verify test exists
    test = get_test(test_uuid)
    if not test:
        raise HTTPException(status_code=404, detail="Test not found")

    agents = get_agents_for_test(test_uuid)
    return agents


@router.delete("")
async def delete_agent_test_link(agent_test: AgentTestDelete):
    """Remove a test from an agent."""
    deleted = remove_test_from_agent(agent_test.agent_uuid, agent_test.test_uuid)
    if not deleted:
        raise HTTPException(status_code=404, detail="Agent-test link not found")
    return {"message": "Test removed from agent successfully"}


# ============ Shared Helper Functions ============


def _build_pense_config(
    agent: Dict[str, Any],
    tests: List[Dict[str, Any]],
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build the pense test config from agent and tests.

    Args:
        agent: Agent dict with config
        tests: List of test dicts with config
        model: Optional model override. If None, uses agent's llm.model or defaults to gpt-4.1
    """
    agent_config = agent.get("config") or {}

    # Get model from param or agent config
    if model is None:
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

    # Combine test cases from all tests
    all_test_cases = []
    for test in tests:
        test_name = test.get("name")
        test_config = test.get("config")
        if not test_config:
            continue

        test_config["name"] = test_name

        if test_config["evaluation"]["type"] == "tool_call":
            tool_calls = []
            for tool_call in test_config["evaluation"]["tool_calls"]:
                tool_calls.append(
                    {
                        "tool": tool_call["tool"],
                        "arguments": (
                            tool_call["arguments"]
                            if not tool_call.get("accept_any_arguments", False)
                            else None
                        ),
                    }
                )
            test_config["evaluation"]["tool_calls"] = tool_calls

        all_test_cases.append(test_config)

    return {
        "params": {"model": model},
        "system_prompt": agent_config.get("system_prompt", ""),
        "tools": tool_configs,
        "test_cases": all_test_cases,
    }


def _run_pense_test(
    model: str,
    pense_config: Dict[str, Any],
    input_dir: Path,
    output_dir: Path,
    s3_bucket: str,
    s3_prefix: str,
    log_prefix: str = "LLM test",
) -> Dict[str, Any]:
    """
    Run pense llm tests run command and return parsed results.

    Args:
        model: Model name to use
        pense_config: The pense config dict
        input_dir: Directory to write config files
        output_dir: Directory to write output files
        s3_bucket: S3 bucket name
        s3_prefix: S3 key prefix for uploading results
        log_prefix: Prefix for log messages

    Returns:
        Dict with keys: success, total_tests, passed, failed, test_results, error
    """
    s3 = get_s3_client()

    # Update config with model
    config = pense_config.copy()
    config["params"] = {"model": model}

    # Resolve directories to absolute paths
    input_dir = input_dir.resolve()
    output_dir = output_dir.resolve()

    # Create directories
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Write config to input directory
    config_file = input_dir / "test_config.json"
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    # Run pense llm tests run command (use absolute paths)
    run_cmd = [
        "pense",
        "llm",
        "tests",
        "run",
        "-c",
        str(config_file),
        "-o",
        str(output_dir),
        "-m",
        model,
    ]

    logger.info(f"{log_prefix} command: {' '.join(run_cmd)}")

    result = subprocess.run(
        run_cmd,
        capture_output=True,
        text=True,
    )

    if result.stdout:
        logger.info(f"{log_prefix} stdout: {result.stdout}")
    if result.stderr:
        logger.info(f"{log_prefix} stderr: {result.stderr}")

    # Find results.json and metrics.json files
    results_data = None
    metrics_data = None

    for root, dirs, files in os.walk(output_dir):
        for file in files:
            file_path = Path(root) / file
            if file == "results.json" and results_data is None:
                with open(file_path, "r", encoding="utf-8") as f:
                    results_data = json.load(f)
            elif file == "metrics.json" and metrics_data is None:
                with open(file_path, "r", encoding="utf-8") as f:
                    metrics_data = json.load(f)

    # Upload results to S3
    for root, dirs, files in os.walk(output_dir):
        for file in files:
            local_file_path = Path(root) / file
            relative_path = local_file_path.relative_to(output_dir)
            s3_key = f"{s3_prefix}/{relative_path}"
            s3.upload_file(str(local_file_path), s3_bucket, s3_key)

    # Parse metrics
    total_tests = 0
    passed = 0
    failed = 0

    if metrics_data and isinstance(metrics_data, dict):
        total_tests = metrics_data.get("total", 0)
        passed = metrics_data.get("passed", 0)
        failed = total_tests - passed

    # Parse results
    test_results = []
    if results_data and isinstance(results_data, list):
        for r in results_data:
            output_data = r.get("output", {})
            metrics = r.get("metrics", {})
            test_case = r.get("test_case", {})
            test_results.append(
                {
                    "passed": metrics.get("passed", False),
                    "output": {
                        "response": output_data.get("response"),
                        "tool_calls": output_data.get("tool_calls"),
                    },
                    "test_case": test_case,
                }
            )

        # If metrics.json wasn't found, compute from results
        if not metrics_data:
            total_tests = len(results_data)
            passed = sum(
                1 for r in results_data if r.get("metrics", {}).get("passed", False)
            )
            failed = total_tests - passed

    error = None
    if result.returncode != 0:
        error = f"Command failed: {result.stderr}"

    return {
        "success": result.returncode == 0,
        "total_tests": total_tests,
        "passed": passed,
        "failed": failed,
        "test_results": test_results,
        "error": error,
    }


def run_llm_test_task(
    task_id: str,
    agent: Dict[str, Any],
    tests: List[Dict[str, Any]],
    s3_bucket: str,
):
    """Run the LLM tests in the background."""
    try:
        logger.info(
            f"Running LLM test task {task_id} for agent {agent['uuid']} with {len(tests)} test(s)"
        )
        update_agent_test_job(task_id, status=TaskStatus.IN_PROGRESS.value)

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            try:
                # Build pense config
                pense_config = _build_pense_config(agent, tests)
                model = pense_config["params"]["model"]

                # Create input and output directories
                input_dir = temp_path / "input"
                output_dir = temp_path / "output"

                # Run pense test
                results_prefix = f"agent-tests/runs/{task_id}"
                result = _run_pense_test(
                    model=model,
                    pense_config=pense_config,
                    input_dir=input_dir,
                    output_dir=output_dir,
                    s3_bucket=s3_bucket,
                    s3_prefix=results_prefix,
                    log_prefix=f"LLM test {task_id}",
                )

                # Update job with results
                update_agent_test_job(
                    task_id,
                    status=TaskStatus.DONE.value,
                    results={
                        "total_tests": result["total_tests"],
                        "passed": result["passed"],
                        "failed": result["failed"],
                        "test_results": result["test_results"],
                        "results_s3_prefix": results_prefix,
                        "error": result["error"],
                    },
                )

                logger.info(
                    f"LLM test task {task_id} completed: {result['passed']}/{result['total_tests']} passed"
                )

            except Exception as e:
                traceback.print_exc()
                update_agent_test_job(
                    task_id,
                    status=TaskStatus.DONE.value,
                    results={"error": f"Unexpected error during LLM test: {str(e)}"},
                )

    except Exception as e:
        traceback.print_exc()
        update_agent_test_job(
            task_id,
            status=TaskStatus.DONE.value,
            results={"error": f"Task failed: {str(e)}"},
        )
    finally:
        # Try to start the next queued job
        try_start_queued_agent_test_job(AGENT_TEST_JOB_TYPES)


@router.post("/agent/{agent_uuid}/run", response_model=TaskCreateResponse)
async def run_agent_test(agent_uuid: str, request: RunTestRequest):
    """
    Run one or more tests for an agent.

    This starts a background task that runs the pense LLM tests command
    with the agent's config and the combined test cases from all specified tests.

    Returns a task ID that can be used to poll for status and results.
    """
    # Verify agent exists
    agent = get_agent(agent_uuid)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    if not request.test_uuids:
        raise HTTPException(
            status_code=400, detail="At least one test UUID is required"
        )

    # Verify all tests exist and are linked to the agent
    tests = []
    for test_uuid in request.test_uuids:
        test = get_test(test_uuid)
        if not test:
            raise HTTPException(status_code=404, detail=f"Test {test_uuid} not found")

        # Verify agent-test link exists
        link = get_agent_test_link(agent_uuid, test_uuid)
        if not link:
            raise HTTPException(
                status_code=400,
                detail=f"Test {test_uuid} is not linked to this agent. Link the test first.",
            )

        tests.append(test)

    # Get S3 configuration
    try:
        s3_bucket = get_s3_output_config()
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Check if we can start immediately or need to queue
    can_start = can_start_agent_test_job(AGENT_TEST_JOB_TYPES)
    initial_status = (
        TaskStatus.IN_PROGRESS.value if can_start else TaskStatus.QUEUED.value
    )

    # Create job in database with details for recovery
    job_id = create_agent_test_job(
        agent_id=agent_uuid,
        job_type="llm-unit-test",
        status=initial_status,
        details={
            "agent_uuid": agent_uuid,
            "test_uuids": request.test_uuids,
            "s3_bucket": s3_bucket,
        },
        results=None,
    )

    if can_start:
        # Start background task in a separate thread
        thread = threading.Thread(
            target=run_llm_test_task,
            args=(job_id, agent, tests, s3_bucket),
            daemon=True,
        )
        thread.start()
        logger.info(f"Started LLM unit test job {job_id} immediately")
    else:
        logger.info(f"Queued LLM unit test job {job_id}")

    return TaskCreateResponse(task_id=job_id, status=initial_status)


@router.get("/run/{task_id}", response_model=TestRunStatusResponse)
async def get_agent_test_run_status(task_id: str):
    """
    Get the status of an agent test run.

    Returns the current status and, if done, the test results.
    """
    job = get_agent_test_job(task_id)
    if not job:
        raise HTTPException(status_code=404, detail="Task not found")

    results = job.get("results") or {}

    return TestRunStatusResponse(
        task_id=task_id,
        status=job["status"],
        total_tests=results.get("total_tests"),
        passed=results.get("passed"),
        failed=results.get("failed"),
        results=results.get("test_results"),
        results_s3_prefix=results.get("results_s3_prefix"),
        error=results.get("error"),
    )


# ============ Benchmark API ============


class BenchmarkRequest(BaseModel):
    test_uuids: List[str]
    models: List[str]  # List of model names to benchmark


class ModelResult(BaseModel):
    model: str
    success: bool
    message: str
    total_tests: Optional[int] = None
    passed: Optional[int] = None
    failed: Optional[int] = None
    test_results: Optional[List[Dict[str, Any]]] = None


class BenchmarkStatusResponse(BaseModel):
    task_id: str
    status: str
    model_results: Optional[List[ModelResult]] = None
    leaderboard_summary: Optional[List[Dict[str, Any]]] = None
    results_s3_prefix: Optional[str] = None
    error: Optional[str] = None


def run_model_benchmark(
    task_id: str,
    model: str,
    pense_config: Dict[str, Any],
    input_dir: Path,
    output_dir: Path,
    s3_bucket: str,
) -> ModelResult:
    """Run benchmark for a single model."""
    try:
        # Create model-specific directories to avoid race conditions in parallel runs
        model_name = model.replace("/", "_")
        model_input_dir = input_dir / model_name
        model_input_dir.mkdir(parents=True, exist_ok=True)

        s3_prefix = f"agent-tests/benchmarks/{task_id}/outputs/{model_name}"

        result = _run_pense_test(
            model=model,
            pense_config=pense_config,
            input_dir=model_input_dir,
            output_dir=output_dir,
            s3_bucket=s3_bucket,
            s3_prefix=s3_prefix,
            log_prefix=f"Benchmark {task_id} model {model}",
        )

        if not result["success"]:
            return ModelResult(
                model=model,
                success=False,
                message=f"Benchmark failed: {result['error']}",
                total_tests=result["total_tests"],
                passed=result["passed"],
                failed=result["failed"],
                test_results=result["test_results"],
            )

        return ModelResult(
            model=model,
            success=True,
            message=f"Benchmark completed successfully for {model}",
            total_tests=result["total_tests"],
            passed=result["passed"],
            failed=result["failed"],
            test_results=result["test_results"],
        )

    except Exception as e:
        traceback.print_exc()
        return ModelResult(
            model=model,
            success=False,
            message=f"Unexpected error: {str(e)}",
        )


def run_benchmark_task(
    task_id: str,
    agent: Dict[str, Any],
    tests: List[Dict[str, Any]],
    models: List[str],
    s3_bucket: str,
):
    """Run the benchmark for multiple models in the background."""
    try:
        logger.info(
            f"Running benchmark task {task_id} for agent {agent['uuid']} "
            f"with {len(tests)} test(s) and {len(models)} model(s)"
        )
        update_agent_test_job(task_id, status=TaskStatus.IN_PROGRESS.value)

        s3 = get_s3_client()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            try:
                # Build the base pense config (model will be set per-run)
                pense_config = _build_pense_config(agent, tests, model=models[0])
                pense_config["params"] = {}  # Clear model, will be set per-run

                # Create input and output directories
                input_dir = temp_path / "input"
                output_dir = temp_path / "output"
                input_dir.mkdir(parents=True, exist_ok=True)
                output_dir.mkdir(parents=True, exist_ok=True)

                # Run benchmarks for all models in parallel
                model_results = []

                logger.info(f"Running {len(models)} models in parallel")

                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=len(models)
                ) as executor:
                    future_to_model = {
                        executor.submit(
                            run_model_benchmark,
                            task_id,
                            model,
                            pense_config,
                            input_dir,
                            output_dir,
                            s3_bucket,
                        ): model
                        for model in models
                    }

                    for future in concurrent.futures.as_completed(future_to_model):
                        result = future.result()
                        model_results.append(result)

                logger.info("Completed running all models in parallel")

                # Check if all models succeeded
                all_succeeded = all(r.success for r in model_results)
                if not all_succeeded:
                    failed_models = [r.model for r in model_results if not r.success]
                    logger.warning(f"Some models failed: {', '.join(failed_models)}")

                # Run leaderboard command
                leaderboard_dir = temp_path / "leaderboard"
                leaderboard_dir.mkdir(parents=True, exist_ok=True)

                leaderboard_cmd = [
                    "pense",
                    "llm",
                    "tests",
                    "leaderboard",
                    "-o",
                    str(output_dir),
                    "-s",
                    str(leaderboard_dir),
                ]

                leaderboard_summary = None
                results_prefix = f"agent-tests/benchmarks/{task_id}"

                logger.info(f"Running leaderboard command: {' '.join(leaderboard_cmd)}")

                try:
                    leaderboard_result = subprocess.run(
                        leaderboard_cmd,
                        capture_output=True,
                        text=True,
                        check=True,
                    )

                    logger.info("Leaderboard command completed successfully")

                    if leaderboard_result.stdout:
                        logger.info(f"Leaderboard stdout: {leaderboard_result.stdout}")
                    if leaderboard_result.stderr:
                        logger.info(f"Leaderboard stderr: {leaderboard_result.stderr}")

                    # Upload leaderboard results
                    for root, dirs, files in os.walk(leaderboard_dir):
                        for file in files:
                            local_file_path = Path(root) / file
                            relative_path = local_file_path.relative_to(leaderboard_dir)
                            s3_key = f"{results_prefix}/leaderboard/{relative_path}"
                            s3.upload_file(str(local_file_path), s3_bucket, s3_key)

                            # Try to read leaderboard CSV for summary
                            if file == "llm_leaderboard.csv":
                                logger.info(
                                    f"Found leaderboard file: {local_file_path}"
                                )
                                try:
                                    import csv

                                    leaderboard_summary = []
                                    with open(
                                        local_file_path, "r", encoding="utf-8"
                                    ) as f:
                                        reader = csv.DictReader(f)
                                        for row in reader:
                                            leaderboard_summary.append(dict(row))
                                    logger.info(
                                        f"Prepared leaderboard summary with {len(leaderboard_summary)} rows"
                                    )
                                except Exception as e:
                                    logger.warning(
                                        f"Failed to read leaderboard CSV: {e}"
                                    )

                except subprocess.CalledProcessError as e:
                    logger.warning(f"Leaderboard command failed: {e.stderr}")

                # Update job with results
                update_agent_test_job(
                    task_id,
                    status=TaskStatus.DONE.value,
                    results={
                        "model_results": [r.model_dump() for r in model_results],
                        "leaderboard_summary": leaderboard_summary,
                        "results_s3_prefix": results_prefix,
                        "error": None if all_succeeded else f"Some models failed",
                    },
                )

                logger.info(f"Benchmark task {task_id} completed")

            except Exception as e:
                traceback.print_exc()
                update_agent_test_job(
                    task_id,
                    status=TaskStatus.DONE.value,
                    results={"error": f"Unexpected error during benchmark: {str(e)}"},
                )

    except Exception as e:
        traceback.print_exc()
        update_agent_test_job(
            task_id,
            status=TaskStatus.DONE.value,
            results={"error": f"Task failed: {str(e)}"},
        )
    finally:
        # Try to start the next queued job
        try_start_queued_agent_test_job(AGENT_TEST_JOB_TYPES)


@router.post("/agent/{agent_uuid}/benchmark", response_model=TaskCreateResponse)
async def run_agent_benchmark(agent_uuid: str, request: BenchmarkRequest):
    """
    Run a benchmark comparing multiple models on the same tests.

    This starts a background task that runs the pense LLM tests command
    for each model in parallel, then generates a leaderboard comparing results.

    Returns a task ID that can be used to poll for status and results.
    """
    # Verify agent exists
    agent = get_agent(agent_uuid)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    if not request.test_uuids:
        raise HTTPException(
            status_code=400, detail="At least one test UUID is required"
        )

    if not request.models:
        raise HTTPException(status_code=400, detail="At least one model is required")

    # Verify all tests exist and are linked to the agent
    tests = []
    for test_uuid in request.test_uuids:
        test = get_test(test_uuid)
        if not test:
            raise HTTPException(status_code=404, detail=f"Test {test_uuid} not found")

        link = get_agent_test_link(agent_uuid, test_uuid)
        if not link:
            raise HTTPException(
                status_code=400,
                detail=f"Test {test_uuid} is not linked to this agent. Link the test first.",
            )

        tests.append(test)

    # Get S3 configuration
    try:
        s3_bucket = get_s3_output_config()
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Check if we can start immediately or need to queue
    can_start = can_start_agent_test_job(AGENT_TEST_JOB_TYPES)
    initial_status = (
        TaskStatus.IN_PROGRESS.value if can_start else TaskStatus.QUEUED.value
    )

    # Create job in database with details for recovery
    job_id = create_agent_test_job(
        agent_id=agent_uuid,
        job_type="llm-benchmark",
        status=initial_status,
        details={
            "agent_uuid": agent_uuid,
            "test_uuids": request.test_uuids,
            "models": request.models,
            "s3_bucket": s3_bucket,
        },
        results=None,
    )

    if can_start:
        # Start background task
        thread = threading.Thread(
            target=run_benchmark_task,
            args=(job_id, agent, tests, request.models, s3_bucket),
            daemon=True,
        )
        thread.start()
        logger.info(f"Started LLM benchmark job {job_id} immediately")
    else:
        logger.info(f"Queued LLM benchmark job {job_id}")

    return TaskCreateResponse(task_id=job_id, status=initial_status)


@router.get("/benchmark/{task_id}", response_model=BenchmarkStatusResponse)
async def get_benchmark_status(task_id: str):
    """
    Get the status of a benchmark run.

    Returns the current status and, if done, results for each model and leaderboard.
    """
    job = get_agent_test_job(task_id)
    if not job:
        raise HTTPException(status_code=404, detail="Task not found")

    results = job.get("results") or {}

    return BenchmarkStatusResponse(
        task_id=task_id,
        status=job["status"],
        model_results=results.get("model_results"),
        leaderboard_summary=results.get("leaderboard_summary"),
        results_s3_prefix=results.get("results_s3_prefix"),
        error=results.get("error"),
    )
