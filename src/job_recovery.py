"""Job recovery module - restarts in_progress jobs on app startup."""

import threading
import logging

from db import get_pending_jobs, get_agent, get_test, update_job
from utils import TaskStatus

logger = logging.getLogger(__name__)


def recover_pending_jobs():
    """Check for in_progress jobs and restart them."""
    pending_jobs = get_pending_jobs()

    if not pending_jobs:
        logger.info("No in_progress jobs to recover")
        return

    logger.info(f"Found {len(pending_jobs)} in_progress job(s) to recover")

    for job in pending_jobs:
        job_id = job["uuid"]
        job_type = job["type"]
        details = job.get("details")

        if not details:
            logger.warning(f"Job {job_id} has no details, marking as failed")
            update_job(
                job_id,
                status=TaskStatus.DONE.value,
                results={"error": "Job recovery failed: no details available"},
            )
            continue

        try:
            if job_type == "stt-eval":
                _recover_stt_job(job_id, details)
            elif job_type == "tts-eval":
                _recover_tts_job(job_id, details)
            elif job_type == "llm-unit-test":
                _recover_llm_unit_test_job(job_id, details)
            elif job_type == "llm-benchmark":
                _recover_llm_benchmark_job(job_id, details)
            else:
                logger.warning(f"Unknown job type: {job_type}, marking as failed")
                update_job(
                    job_id,
                    status=TaskStatus.DONE.value,
                    results={
                        "error": f"Job recovery failed: unknown job type {job_type}"
                    },
                )
        except Exception as e:
            logger.error(f"Failed to recover job {job_id}: {e}")
            update_job(
                job_id,
                status=TaskStatus.DONE.value,
                results={"error": f"Job recovery failed: {str(e)}"},
            )


def _recover_stt_job(job_id: str, details: dict):
    """Recover an STT evaluation job."""
    from routers.stt import run_evaluation_task, STTEvaluationRequest

    logger.info(f"Recovering STT job {job_id}")

    request = STTEvaluationRequest(
        audio_paths=details["audio_paths"],
        texts=details["texts"],
        providers=details["providers"],
        language=details["language"],
    )
    s3_bucket = details["s3_bucket"]

    thread = threading.Thread(
        target=run_evaluation_task,
        args=(job_id, request, s3_bucket),
        daemon=True,
    )
    thread.start()
    logger.info(f"STT job {job_id} recovery started")


def _recover_tts_job(job_id: str, details: dict):
    """Recover a TTS evaluation job."""
    from routers.tts import run_tts_evaluation_task, TTSEvaluationRequest

    logger.info(f"Recovering TTS job {job_id}")

    request = TTSEvaluationRequest(
        texts=details["texts"],
        providers=details["providers"],
        language=details["language"],
    )
    s3_bucket = details["s3_bucket"]

    thread = threading.Thread(
        target=run_tts_evaluation_task,
        args=(job_id, request, s3_bucket),
        daemon=True,
    )
    thread.start()
    logger.info(f"TTS job {job_id} recovery started")


def _recover_llm_unit_test_job(job_id: str, details: dict):
    """Recover an LLM unit test job."""
    from routers.agent_tests import run_llm_test_task

    logger.info(f"Recovering LLM unit test job {job_id}")

    agent_uuid = details["agent_uuid"]
    test_uuids = details["test_uuids"]
    s3_bucket = details["s3_bucket"]

    # Fetch agent and tests
    agent = get_agent(agent_uuid)
    if not agent:
        raise ValueError(f"Agent {agent_uuid} not found")

    tests = []
    for test_uuid in test_uuids:
        test = get_test(test_uuid)
        if not test:
            raise ValueError(f"Test {test_uuid} not found")
        tests.append(test)

    thread = threading.Thread(
        target=run_llm_test_task,
        args=(job_id, agent, tests, s3_bucket),
        daemon=True,
    )
    thread.start()
    logger.info(f"LLM unit test job {job_id} recovery started")


def _recover_llm_benchmark_job(job_id: str, details: dict):
    """Recover an LLM benchmark job."""
    from routers.agent_tests import run_benchmark_task

    logger.info(f"Recovering LLM benchmark job {job_id}")

    agent_uuid = details["agent_uuid"]
    test_uuids = details["test_uuids"]
    models = details["models"]
    s3_bucket = details["s3_bucket"]

    # Fetch agent and tests
    agent = get_agent(agent_uuid)
    if not agent:
        raise ValueError(f"Agent {agent_uuid} not found")

    tests = []
    for test_uuid in test_uuids:
        test = get_test(test_uuid)
        if not test:
            raise ValueError(f"Test {test_uuid} not found")
        tests.append(test)

    thread = threading.Thread(
        target=run_benchmark_task,
        args=(job_id, agent, tests, models, s3_bucket),
        daemon=True,
    )
    thread.start()
    logger.info(f"LLM benchmark job {job_id} recovery started")
