import csv
import os
import json
import subprocess
import tempfile
import time
import traceback
import threading
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional

from fastapi import APIRouter, HTTPException, Depends
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
    delete_agent_test_job,
)
from auth_utils import get_current_user_id
from utils import (
    TaskStatus,
    TaskCreateResponse,
    get_s3_client,
    get_s3_output_config,
    can_start_agent_test_job,
    try_start_queued_agent_test_job,
    register_job_starter,
    is_job_timed_out,
    capture_exception_to_sentry,
    build_tool_configs,
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
    """Result for a single test case matching calibrate results.json structure"""

    name: Optional[str] = None  # Test name, present during in-progress and done states
    passed: Optional[bool] = None  # Only present when done
    output: Optional[TestOutput] = None  # Only present when done
    test_case: Optional[Dict[str, Any]] = None  # Only present when done


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


def _build_calibrate_config(
    agent: Dict[str, Any],
    tests: List[Dict[str, Any]],
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build the calibrate test config from agent and tests.

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
    tool_configs = build_tool_configs(agent_tools)

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


def _read_agent_test_results_json(output_dir: Path) -> Optional[List[dict]]:
    """Read results.json from agent test output directory if it exists."""
    if not output_dir or not output_dir.exists():
        return None
    for root, dirs, files in os.walk(output_dir):
        for file in files:
            if file == "results.json":
                file_path = Path(root) / file
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        return json.load(f)
                except Exception:
                    return None
    return None


def _read_agent_test_metrics_json(output_dir: Path) -> Optional[dict]:
    """Read metrics.json from agent test output directory if it exists."""
    if not output_dir or not output_dir.exists():
        return None
    for root, dirs, files in os.walk(output_dir):
        for file in files:
            if file == "metrics.json":
                file_path = Path(root) / file
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        return json.load(f)
                except Exception:
                    return None
    return None


def _parse_agent_test_results(results_data: Optional[List[dict]]) -> List[dict]:
    """Parse results.json data into the format expected by the API."""
    if not results_data or not isinstance(results_data, list):
        return []
    test_results = []
    for r in results_data:
        output_data = r.get("output", {})
        metrics = r.get("metrics", {})
        test_case = r.get("test_case", {})
        test_results.append(
            {
                "name": test_case.get("name"),
                "passed": metrics.get("passed", False),
                "output": {
                    "response": output_data.get("response"),
                    "tool_calls": output_data.get("tool_calls"),
                },
                "test_case": test_case,
            }
        )
    return test_results


def _find_all_results_in_output(output_dir: Path) -> Dict[str, tuple]:
    """
    Walk output_dir and find all results.json and metrics.json files.
    Returns a dict mapping folder names to (results_data, metrics_data) tuples.
    """
    found = {}
    if not output_dir.exists():
        return found

    for root, dirs, files in os.walk(output_dir):
        root_path = Path(root)
        results_data = None
        metrics_data = None

        if "results.json" in files:
            try:
                with open(root_path / "results.json", "r", encoding="utf-8") as f:
                    results_data = json.load(f)
            except Exception:
                pass

        if "metrics.json" in files:
            try:
                with open(root_path / "metrics.json", "r", encoding="utf-8") as f:
                    metrics_data = json.load(f)
            except Exception:
                pass

        if results_data is not None or metrics_data is not None:
            # Use the folder name as key (this contains the model name)
            found[root_path.name] = (results_data, metrics_data)

    return found


def _match_model_to_folder(model: str, folder_names: List[str]) -> Optional[str]:
    """Find folder name that matches the model."""
    # Normalize model name for matching
    model_normalized = model.replace("/", "_").replace(":", "_").lower()
    model_alt = model.replace("/", "-").replace(":", "-").lower()
    # Also try double underscore (some calibrate versions use this)
    model_double = model.replace("/", "__").replace(":", "__").lower()

    for folder in folder_names:
        folder_lower = folder.lower()
        if (
            model_normalized in folder_lower
            or model_alt in folder_lower
            or model_double in folder_lower
            or model.lower() in folder_lower
        ):
            return folder
    return None


def _read_leaderboard_csv(leaderboard_dir: Path) -> Optional[List[dict]]:
    """Read the leaderboard CSV from the leaderboard directory."""
    if not leaderboard_dir.exists():
        logger.warning(f"Leaderboard directory does not exist: {leaderboard_dir}")
        return None

    # Find CSV file in leaderboard directory
    csv_files = list(leaderboard_dir.glob("*.csv"))
    if not csv_files:
        logger.warning(
            f"No CSV files found in leaderboard directory: {leaderboard_dir}"
        )
        all_files = list(leaderboard_dir.iterdir())
        logger.info(f"Files in leaderboard directory: {[f.name for f in all_files]}")
        return None

    csv_file = csv_files[0]
    logger.info(f"Reading leaderboard from: {csv_file}")

    try:
        leaderboard_summary = []
        with open(csv_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                leaderboard_summary.append(dict(row))
        logger.info(f"Read {len(leaderboard_summary)} rows from leaderboard")
        return leaderboard_summary
    except Exception as e:
        logger.warning(f"Failed to read leaderboard CSV: {e}")
        return None


def _update_agent_test_intermediate_results(
    task_id: str,
    output_dir: Path,
    test_names: List[str],
) -> int:
    """
    Update intermediate results for an agent test job.
    Returns the number of completed tests.
    """
    results_data = _read_agent_test_results_json(output_dir)
    if results_data is None:
        return 0

    # Parse results
    test_results = _parse_agent_test_results(results_data)
    completed_count = len(test_results)

    # Create a dict of completed tests by name
    completed_tests = {r.get("name"): r for r in test_results if r.get("name")}

    # Build intermediate results: show completed tests with results, pending tests with just name
    intermediate_results = []
    for name in test_names:
        if name in completed_tests:
            intermediate_results.append(completed_tests[name])
        else:
            intermediate_results.append({"name": name})

    # Check if metrics.json exists (all tests complete)
    metrics_data = _read_agent_test_metrics_json(output_dir)

    update_agent_test_job(
        task_id,
        results={
            "total_tests": (
                metrics_data.get("total") if metrics_data else len(test_names)
            ),
            "passed": metrics_data.get("passed") if metrics_data else None,
            "failed": (
                (metrics_data.get("total", 0) - metrics_data.get("passed", 0))
                if metrics_data
                else None
            ),
            "test_results": intermediate_results,
        },
    )

    return completed_count


def run_llm_test_task(
    task_id: str,
    agent: Dict[str, Any],
    tests: List[Dict[str, Any]],
    s3_bucket: str,
):
    """Run the LLM tests in the background using a single CLI command with intermediate updates."""
    try:
        logger.info(
            f"Running LLM test task {task_id} for agent {agent['uuid']} with {len(tests)} test(s)"
        )

        # Extract test names for progress tracking
        test_names = [test.get("name") for test in tests if test.get("name")]

        update_agent_test_job(
            task_id,
            status=TaskStatus.IN_PROGRESS.value,
            results={"test_results": [{"name": name} for name in test_names]},
        )

        s3 = get_s3_client()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            try:
                # Build calibrate config
                calibrate_config = _build_calibrate_config(agent, tests)
                model = calibrate_config["params"]["model"]

                # Get provider from agent config (default to openrouter)
                agent_config = agent.get("config") or {}
                llm_config = agent_config.get("llm", {})
                provider = llm_config.get("provider", "openrouter")

                # Create directories
                input_dir = temp_path / "input"
                output_dir = temp_path / "output"
                input_dir.mkdir(parents=True, exist_ok=True)
                output_dir.mkdir(parents=True, exist_ok=True)

                # Write config file
                config_file = input_dir / "test_config.json"
                with open(config_file, "w", encoding="utf-8") as f:
                    json.dump(calibrate_config, f, indent=2)

                # Run calibrate llm command with single model
                run_cmd = [
                    "calibrate",
                    "llm",
                    "-c",
                    str(config_file),
                    "-m",
                    model,
                    "-p",
                    provider,
                    "-o",
                    str(output_dir),
                ]

                logger.info(f"Running LLM test command: {' '.join(run_cmd)}")

                # Create temp files for stdout/stderr
                stdout_path = output_dir / "stdout.log"
                stderr_path = output_dir / "stderr.log"

                with (
                    open(stdout_path, "w") as stdout_f,
                    open(stderr_path, "w") as stderr_f,
                ):
                    process = subprocess.Popen(
                        run_cmd,
                        stdout=stdout_f,
                        stderr=stderr_f,
                        text=True,
                        start_new_session=True,
                        cwd=str(temp_path),
                    )

                    # Poll for process completion while updating intermediate results
                    prev_completed = 0
                    while process.poll() is None:
                        completed = _update_agent_test_intermediate_results(
                            task_id, output_dir, test_names
                        )
                        if completed != prev_completed:
                            logger.info(
                                f"LLM test {task_id}: {completed}/{len(test_names)} tests completed"
                            )
                            prev_completed = completed
                        time.sleep(2)  # Poll every 2 seconds

                    # Final update after process completes
                    _update_agent_test_intermediate_results(
                        task_id, output_dir, test_names
                    )

                # Read stdout/stderr
                with open(stdout_path, "r") as f:
                    stdout = f.read()
                with open(stderr_path, "r") as f:
                    stderr = f.read()

                if stdout:
                    logger.info(f"LLM test stdout: {stdout}")
                if stderr:
                    logger.info(f"LLM test stderr: {stderr}")

                # Check for failure
                has_error_in_stderr = "Traceback (most recent call last):" in stderr
                is_failure = process.returncode != 0 or has_error_in_stderr

                if is_failure:
                    error_msg = (
                        f"LLM test failed with exit code {process.returncode}: {stderr}"
                    )
                    logger.error(error_msg)
                    capture_exception_to_sentry(RuntimeError(error_msg))
                    raise subprocess.CalledProcessError(
                        process.returncode, run_cmd, stdout, stderr
                    )

                logger.info("LLM test command completed successfully")

                # Log output directory contents for debugging
                logger.info(
                    f"Output directory contents: {[f.name for f in output_dir.iterdir()]}"
                )

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

                # Parse results
                test_results = _parse_agent_test_results(results_data)

                # Add name field for consistency
                for i, r in enumerate(test_results):
                    if not r.get("name") and results_data and i < len(results_data):
                        test_case = results_data[i].get("test_case", {})
                        r["name"] = test_case.get("name")

                # Parse metrics
                total_tests = 0
                passed = 0
                failed = 0

                if metrics_data and isinstance(metrics_data, dict):
                    total_tests = metrics_data.get("total", 0)
                    passed = metrics_data.get("passed", 0)
                    failed = total_tests - passed
                elif results_data:
                    # Compute from results if metrics.json not found
                    total_tests = len(results_data)
                    passed = sum(
                        1
                        for r in results_data
                        if r.get("metrics", {}).get("passed", False)
                    )
                    failed = total_tests - passed

                # Upload results to S3
                results_prefix = f"agent-tests/runs/{task_id}"
                for root, dirs, files in os.walk(output_dir):
                    for file in files:
                        local_file_path = Path(root) / file
                        relative_path = local_file_path.relative_to(output_dir)
                        s3_key = f"{results_prefix}/{relative_path}"
                        s3.upload_file(str(local_file_path), s3_bucket, s3_key)

                # Upload the config file to S3
                config_s3_key = f"{results_prefix}/test_config.json"
                s3.upload_file(str(config_file), s3_bucket, config_s3_key)
                logger.info(f"Uploaded config file to S3: {config_s3_key}")

                # Update job with results
                update_agent_test_job(
                    task_id,
                    status=TaskStatus.DONE.value,
                    results={
                        "total_tests": total_tests,
                        "passed": passed,
                        "failed": failed,
                        "test_results": test_results,
                        "results_s3_prefix": results_prefix,
                        "error": None,
                    },
                )

                logger.info(
                    f"LLM test task {task_id} completed: {passed}/{total_tests} passed"
                )

            except subprocess.CalledProcessError as e:
                traceback.print_exc()
                capture_exception_to_sentry(e)
                update_agent_test_job(
                    task_id,
                    status=TaskStatus.FAILED.value,
                    results={
                        "error": f"LLM test failed: {e.stderr if hasattr(e, 'stderr') else str(e)}",
                    },
                )
            except Exception as e:
                traceback.print_exc()
                capture_exception_to_sentry(e)
                # Preserve any existing results from the job
                existing_job = get_agent_test_job(task_id)
                existing_results = (
                    (existing_job.get("results") or {}) if existing_job else {}
                )
                existing_results["error"] = (
                    f"Unexpected error during LLM test: {str(e)}"
                )
                update_agent_test_job(
                    task_id,
                    status=TaskStatus.FAILED.value,
                    results=existing_results,
                )

    except Exception as e:
        traceback.print_exc()
        capture_exception_to_sentry(e)
        # Preserve any existing results from the job
        existing_job = get_agent_test_job(task_id)
        existing_results = (existing_job.get("results") or {}) if existing_job else {}
        existing_results["error"] = f"Task failed: {str(e)}"
        update_agent_test_job(
            task_id,
            status=TaskStatus.FAILED.value,
            results=existing_results,
        )
    finally:
        # Try to start the next queued job
        try_start_queued_agent_test_job(AGENT_TEST_JOB_TYPES)


@router.post("/agent/{agent_uuid}/run", response_model=TaskCreateResponse)
async def run_agent_test(agent_uuid: str, request: RunTestRequest):
    """
    Run one or more tests for an agent.

    This starts a background task that runs the calibrate LLM tests command
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

    # Extract test names for progress tracking
    test_names = [test.get("name") for test in tests if test.get("name")]

    # Create job in database with details for recovery
    job_id = create_agent_test_job(
        agent_id=agent_uuid,
        job_type="llm-unit-test",
        status=initial_status,
        details={
            "agent_uuid": agent_uuid,
            "test_uuids": request.test_uuids,
            "test_names": test_names,
            "s3_bucket": s3_bucket,
        },
        results={"test_results": [{"name": name} for name in test_names]},
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

    status = job["status"]
    results = job.get("results") or {}

    # Check for timeout on in-progress jobs
    # if status == TaskStatus.IN_PROGRESS.value:
    #     updated_at = job.get("updated_at")
    #     if updated_at and is_job_timed_out(updated_at):
    #         logger.warning(f"Agent test job {task_id} timed out, marking as failed")

    #         # Mark job as failed (preserve existing results, add error)
    #         results["error"] = "Job timed out after 5 minutes of inactivity"
    #         update_agent_test_job(
    #             task_id,
    #             status=TaskStatus.FAILED.value,
    #             results=results,
    #         )
    #         status = TaskStatus.FAILED.value

    #         # Try to start the next queued job
    #         try_start_queued_agent_test_job(AGENT_TEST_JOB_TYPES)

    return TestRunStatusResponse(
        task_id=task_id,
        status=status,
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
    success: Optional[bool] = None  # None while queued/processing, True/False when done
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


def _update_benchmark_intermediate_results(
    task_id: str,
    output_dir: Path,
    models: List[str],
) -> int:
    """
    Update intermediate results for a benchmark job.
    Returns the number of models with completed results.
    """
    # Find all results in output directory
    all_results = _find_all_results_in_output(output_dir)
    folder_names = list(all_results.keys())

    model_results = []
    completed_count = 0

    for model in models:
        matched_folder = _match_model_to_folder(model, folder_names)

        if matched_folder and matched_folder in all_results:
            results_data, metrics_data = all_results[matched_folder]

            # Parse results
            test_results = _parse_agent_test_results(results_data)

            # Add name field for consistency
            for i, r in enumerate(test_results):
                if not r.get("name") and results_data and i < len(results_data):
                    test_case = results_data[i].get("test_case", {})
                    r["name"] = test_case.get("name")

            if metrics_data:
                total = metrics_data.get("total", 0)
                passed = metrics_data.get("passed", 0)
                model_results.append(
                    {
                        "model": model,
                        "success": True,
                        "message": f"Completed",
                        "total_tests": total,
                        "passed": passed,
                        "failed": total - passed,
                        "test_results": test_results,
                    }
                )
                completed_count += 1
            elif test_results:
                # Has partial results but no metrics yet
                total = len(test_results)
                passed = sum(1 for r in test_results if r.get("passed", False))
                model_results.append(
                    {
                        "model": model,
                        "success": None,
                        "message": f"Running... ({len(test_results)} tests done)",
                        "total_tests": total,
                        "passed": passed,
                        "failed": total - passed,
                        "test_results": test_results,
                    }
                )
            else:
                # No results yet for this model
                model_results.append(
                    {
                        "model": model,
                        "success": None,
                        "message": "Queued...",
                        "total_tests": None,
                        "passed": None,
                        "failed": None,
                        "test_results": None,
                    }
                )
        else:
            # No folder found for this model yet
            model_results.append(
                {
                    "model": model,
                    "success": None,
                    "message": "Queued...",
                    "total_tests": None,
                    "passed": None,
                    "failed": None,
                    "test_results": None,
                }
            )

    update_agent_test_job(
        task_id,
        results={"model_results": model_results},
    )

    return completed_count


def run_benchmark_task(
    task_id: str,
    agent: Dict[str, Any],
    tests: List[Dict[str, Any]],
    models: List[str],
    s3_bucket: str,
):
    """Run the benchmark for multiple models using a single CLI command with intermediate updates.

    The calibrate CLI handles parallelization internally and generates the leaderboard.
    """
    try:
        logger.info(
            f"Running benchmark task {task_id} for agent {agent['uuid']} "
            f"with {len(tests)} test(s) and {len(models)} model(s)"
        )

        # Initialize with pending model results
        initial_model_results = [
            {"model": model, "success": None, "message": "Queued..."}
            for model in models
        ]
        update_agent_test_job(
            task_id,
            status=TaskStatus.IN_PROGRESS.value,
            results={"model_results": initial_model_results},
        )

        s3 = get_s3_client()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            try:
                # Build the calibrate config
                calibrate_config = _build_calibrate_config(
                    agent, tests, model=models[0]
                )
                calibrate_config["params"] = {}  # Clear model, will be set via CLI

                # Create directories
                input_dir = temp_path / "input"
                output_dir = temp_path / "output"
                input_dir.mkdir(parents=True, exist_ok=True)
                output_dir.mkdir(parents=True, exist_ok=True)

                # Write config file
                config_file = input_dir / "test_config.json"
                with open(config_file, "w", encoding="utf-8") as f:
                    json.dump(calibrate_config, f, indent=2)

                # Get provider from agent config (default to openrouter)
                agent_config = agent.get("config") or {}
                llm_config = agent_config.get("llm", {})
                provider = llm_config.get("provider", "openrouter")

                # Run calibrate llm command with all models at once
                # The CLI handles parallelization internally and generates leaderboard
                run_cmd = (
                    [
                        "calibrate",
                        "llm",
                        "-c",
                        str(config_file),
                        "-m",
                    ]
                    + models
                    + [
                        "-p",
                        provider,
                        "-o",
                        str(output_dir),
                    ]
                )

                logger.info(f"Running benchmark command: {' '.join(run_cmd)}")

                # Create temp files for stdout/stderr
                stdout_path = output_dir / "stdout.log"
                stderr_path = output_dir / "stderr.log"

                with (
                    open(stdout_path, "w") as stdout_f,
                    open(stderr_path, "w") as stderr_f,
                ):
                    process = subprocess.Popen(
                        run_cmd,
                        stdout=stdout_f,
                        stderr=stderr_f,
                        text=True,
                        start_new_session=True,
                        cwd=str(temp_path),
                    )

                    # Poll for process completion while updating intermediate results
                    prev_completed = 0
                    while process.poll() is None:
                        completed = _update_benchmark_intermediate_results(
                            task_id, output_dir, models
                        )
                        if completed != prev_completed:
                            logger.info(
                                f"Benchmark {task_id}: {completed}/{len(models)} models completed"
                            )
                            prev_completed = completed
                        time.sleep(2)  # Poll every 2 seconds

                    # Final update after process completes
                    _update_benchmark_intermediate_results(task_id, output_dir, models)

                # Read stdout/stderr
                with open(stdout_path, "r") as f:
                    stdout = f.read()
                with open(stderr_path, "r") as f:
                    stderr = f.read()

                if stdout:
                    logger.info(f"Benchmark stdout: {stdout}")
                if stderr:
                    logger.info(f"Benchmark stderr: {stderr}")

                # Check for failure
                has_error_in_stderr = "Traceback (most recent call last):" in stderr
                is_failure = process.returncode != 0 or has_error_in_stderr

                if is_failure:
                    error_msg = f"Benchmark failed with exit code {process.returncode}: {stderr}"
                    logger.error(error_msg)
                    capture_exception_to_sentry(RuntimeError(error_msg))
                    raise subprocess.CalledProcessError(
                        process.returncode, run_cmd, stdout, stderr
                    )

                logger.info("Benchmark command completed successfully")

                # Log output directory contents for debugging
                logger.info(
                    f"Output directory contents: {[f.name for f in output_dir.iterdir()]}"
                )

                # Read results for each model from output directory
                all_results = _find_all_results_in_output(output_dir)
                folder_names = list(all_results.keys())
                logger.info(f"Found result folders: {folder_names}")

                model_results = []
                for model in models:
                    matched_folder = _match_model_to_folder(model, folder_names)

                    if matched_folder and matched_folder in all_results:
                        results_data, metrics_data = all_results[matched_folder]

                        # Parse results
                        test_results = _parse_agent_test_results(results_data)

                        # Add name field for consistency
                        for i, r in enumerate(test_results):
                            if (
                                not r.get("name")
                                and results_data
                                and i < len(results_data)
                            ):
                                test_case = results_data[i].get("test_case", {})
                                r["name"] = test_case.get("name")

                        if metrics_data:
                            total = metrics_data.get("total", 0)
                            passed = metrics_data.get("passed", 0)
                            model_results.append(
                                ModelResult(
                                    model=model,
                                    success=True,
                                    message=f"Benchmark completed successfully for {model}",
                                    total_tests=total,
                                    passed=passed,
                                    failed=total - passed,
                                    test_results=test_results,
                                )
                            )
                        else:
                            # No metrics but has results - compute from results
                            total = len(test_results) if test_results else 0
                            passed = sum(
                                1 for r in test_results if r.get("passed", False)
                            )
                            model_results.append(
                                ModelResult(
                                    model=model,
                                    success=True,
                                    message=f"Benchmark completed for {model}",
                                    total_tests=total,
                                    passed=passed,
                                    failed=total - passed,
                                    test_results=test_results,
                                )
                            )
                    else:
                        logger.warning(f"No output found for model {model}")
                        model_results.append(
                            ModelResult(
                                model=model,
                                success=False,
                                message=f"No output found for model {model}",
                            )
                        )

                # Read leaderboard from output directory
                leaderboard_dir = output_dir / "leaderboard"
                leaderboard_summary = None
                if leaderboard_dir.exists():
                    logger.info(f"Leaderboard directory exists: {leaderboard_dir}")
                    leaderboard_summary = _read_leaderboard_csv(leaderboard_dir)

                    # Upload leaderboard to S3
                    results_prefix = f"agent-tests/benchmarks/{task_id}"
                    for root, dirs, files in os.walk(leaderboard_dir):
                        for file in files:
                            local_file_path = Path(root) / file
                            relative_path = local_file_path.relative_to(leaderboard_dir)
                            s3_key = f"{results_prefix}/leaderboard/{relative_path}"
                            s3.upload_file(str(local_file_path), s3_bucket, s3_key)
                else:
                    logger.warning(
                        f"Leaderboard directory does not exist: {leaderboard_dir}"
                    )

                results_prefix = f"agent-tests/benchmarks/{task_id}"

                # Upload output directory to S3
                for root, dirs, files in os.walk(output_dir):
                    for file in files:
                        local_file_path = Path(root) / file
                        relative_path = local_file_path.relative_to(output_dir)
                        s3_key = f"{results_prefix}/outputs/{relative_path}"
                        s3.upload_file(str(local_file_path), s3_bucket, s3_key)

                logger.info(
                    f"Uploaded benchmark outputs to s3://{s3_bucket}/{results_prefix}/outputs/"
                )

                # Create and upload benchmark config file to S3
                benchmark_config = {
                    **calibrate_config,
                    "models": models,
                }
                config_s3_key = f"{results_prefix}/benchmark_config.json"
                with open(config_file, "w", encoding="utf-8") as f:
                    json.dump(benchmark_config, f, indent=2)
                s3.upload_file(str(config_file), s3_bucket, config_s3_key)
                logger.info(f"Uploaded benchmark config file to S3: {config_s3_key}")

                # Check if all models succeeded
                all_succeeded = all(r.success for r in model_results)
                final_status = (
                    TaskStatus.DONE.value if all_succeeded else TaskStatus.FAILED.value
                )

                error_msg = None
                if not all_succeeded:
                    failed = [r.model for r in model_results if not r.success]
                    error_msg = f"Some models failed: {', '.join(failed)}"

                # Update job with results
                update_agent_test_job(
                    task_id,
                    status=final_status,
                    results={
                        "model_results": [r.model_dump() for r in model_results],
                        "leaderboard_summary": leaderboard_summary,
                        "results_s3_prefix": results_prefix,
                        "error": error_msg,
                    },
                )

                logger.info(
                    f"Benchmark task {task_id} completed, status={final_status}"
                )

            except subprocess.CalledProcessError as e:
                traceback.print_exc()
                capture_exception_to_sentry(e)
                update_agent_test_job(
                    task_id,
                    status=TaskStatus.FAILED.value,
                    results={
                        "error": f"Benchmark failed: {e.stderr if hasattr(e, 'stderr') else str(e)}",
                    },
                )
            except Exception as e:
                traceback.print_exc()
                capture_exception_to_sentry(e)
                # Preserve any existing results from the job
                existing_job = get_agent_test_job(task_id)
                existing_results = (
                    (existing_job.get("results") or {}) if existing_job else {}
                )
                existing_results["error"] = (
                    f"Unexpected error during benchmark: {str(e)}"
                )
                update_agent_test_job(
                    task_id,
                    status=TaskStatus.FAILED.value,
                    results=existing_results,
                )

    except Exception as e:
        traceback.print_exc()
        capture_exception_to_sentry(e)
        # Preserve any existing results from the job
        existing_job = get_agent_test_job(task_id)
        existing_results = (existing_job.get("results") or {}) if existing_job else {}
        existing_results["error"] = f"Task failed: {str(e)}"
        update_agent_test_job(
            task_id,
            status=TaskStatus.FAILED.value,
            results=existing_results,
        )
    finally:
        # Try to start the next queued job
        try_start_queued_agent_test_job(AGENT_TEST_JOB_TYPES)


@router.post("/agent/{agent_uuid}/benchmark", response_model=TaskCreateResponse)
async def run_agent_benchmark(agent_uuid: str, request: BenchmarkRequest):
    """
    Run a benchmark comparing multiple models on the same tests.

    This starts a background task that runs the calibrate LLM tests command
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

    # Extract test names for progress tracking
    test_names = [test.get("name") for test in tests if test.get("name")]

    # Create job in database with details for recovery
    job_id = create_agent_test_job(
        agent_id=agent_uuid,
        job_type="llm-benchmark",
        status=initial_status,
        details={
            "agent_uuid": agent_uuid,
            "test_uuids": request.test_uuids,
            "test_names": test_names,
            "models": request.models,
            "s3_bucket": s3_bucket,
        },
        results={"test_results": [{"name": name} for name in test_names]},
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

    status = job["status"]
    results = job.get("results") or {}

    # Check for timeout on in-progress jobs
    # if status == TaskStatus.IN_PROGRESS.value:
    #     updated_at = job.get("updated_at")
    #     if updated_at and is_job_timed_out(updated_at):
    #         logger.warning(f"Benchmark job {task_id} timed out, marking as failed")

    #         # Mark job as failed (preserve existing results, add error)
    #         results["error"] = "Job timed out after 5 minutes of inactivity"
    #         update_agent_test_job(
    #             task_id,
    #             status=TaskStatus.FAILED.value,
    #             results=results,
    #         )
    #         status = TaskStatus.FAILED.value

    #         # Try to start the next queued job
    #         try_start_queued_agent_test_job(AGENT_TEST_JOB_TYPES)

    return BenchmarkStatusResponse(
        task_id=task_id,
        status=status,
        model_results=results.get("model_results"),
        leaderboard_summary=results.get("leaderboard_summary"),
        results_s3_prefix=results.get("results_s3_prefix"),
        error=results.get("error"),
    )


@router.delete("/job/{job_uuid}")
async def delete_agent_test_job_endpoint(
    job_uuid: str, user_id: str = Depends(get_current_user_id)
):
    """Delete an agent test job. Only the owner can delete their jobs."""
    # Check if job exists
    job = get_agent_test_job(job_uuid)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Check ownership via agent
    agent_id = job.get("agent_id")
    if agent_id:
        agent = get_agent(agent_id)
        if not agent or agent.get("user_id") != user_id:
            raise HTTPException(status_code=404, detail="Job not found")
    else:
        raise HTTPException(status_code=404, detail="Job not found")

    # Check if this was a running job (to trigger next queued job after delete)
    was_running = job.get("status") == TaskStatus.IN_PROGRESS.value

    # Delete the job
    deleted = delete_agent_test_job(job_uuid)
    if not deleted:
        raise HTTPException(status_code=404, detail="Job not found")

    # If the deleted job was running, try to start the next queued job
    if was_running:
        try_start_queued_agent_test_job(AGENT_TEST_JOB_TYPES)

    return {"message": "Agent test job deleted successfully"}
