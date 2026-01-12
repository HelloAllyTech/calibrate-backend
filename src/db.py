import sqlite3
import json
import logging
import uuid
from os.path import join
import os
from pathlib import Path
from typing import Optional, List, Dict, Any
from contextlib import contextmanager
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Database path
DB_PATH = Path(join(os.getenv("DB_ROOT_DIR"), "pense.db"))


@contextmanager
def get_db_connection():
    """Context manager for database connections."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    """Initialize the database and create tables if they don't exist."""
    # Ensure the data directory exists
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS agents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uuid TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                config TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                deleted_at TIMESTAMP DEFAULT NULL
            )
        """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS tools (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uuid TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                description TEXT,
                config TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                deleted_at TIMESTAMP DEFAULT NULL
            )
        """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_tools (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                tool_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                deleted_at TIMESTAMP DEFAULT NULL,
                UNIQUE(agent_id, tool_id),
                FOREIGN KEY (agent_id) REFERENCES agents(uuid),
                FOREIGN KEY (tool_id) REFERENCES tools(uuid)
            )
        """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS tests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uuid TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                config TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                deleted_at TIMESTAMP DEFAULT NULL
            )
        """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_tests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                test_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                deleted_at TIMESTAMP DEFAULT NULL,
                UNIQUE(agent_id, test_id),
                FOREIGN KEY (agent_id) REFERENCES agents(uuid),
                FOREIGN KEY (test_id) REFERENCES tests(uuid)
            )
        """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uuid TEXT NOT NULL UNIQUE,
                type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'in_progress',
                details TEXT,
                results TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        )

        # Add details column to jobs table if not present (migration)
        try:
            cursor.execute("ALTER TABLE jobs ADD COLUMN details TEXT")
        except sqlite3.OperationalError:
            # Column already exists
            pass

        # Add deleted_at column to existing tables if not present (migration)
        tables_to_migrate = [
            "agents",
            "tools",
            "agent_tools",
            "tests",
            "agent_tests",
        ]
        for table in tables_to_migrate:
            try:
                cursor.execute(
                    f"ALTER TABLE {table} ADD COLUMN deleted_at TIMESTAMP DEFAULT NULL"
                )
            except sqlite3.OperationalError:
                # Column already exists
                pass

        conn.commit()
        logger.info("Database initialized successfully")


def create_agent(name: str, config: Optional[Dict[str, Any]] = None) -> str:
    """Create a new agent and return its UUID."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        # Generate UUID for the agent
        agent_uuid = str(uuid.uuid4())
        # Serialize config to JSON string for storage
        config_json = json.dumps(config) if config is not None else None
        cursor.execute(
            """
            INSERT INTO agents (uuid, name, config)
            VALUES (?, ?, ?)
            """,
            (agent_uuid, name, config_json),
        )
        conn.commit()
        logger.info(f"Created agent with UUID: {agent_uuid}")
        return agent_uuid


def _parse_agent_row(row: sqlite3.Row) -> Dict[str, Any]:
    """Parse a database row and deserialize JSON fields."""
    agent = dict(row)
    # Deserialize config from JSON string
    if agent.get("config"):
        agent["config"] = json.loads(agent["config"])

    return agent


def get_agent(agent_uuid: str) -> Optional[Dict[str, Any]]:
    """Get an agent by UUID."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM agents WHERE uuid = ? AND deleted_at IS NULL", (agent_uuid,)
        )
        row = cursor.fetchone()
        if row:
            return _parse_agent_row(row)
        return None


def get_all_agents() -> List[Dict[str, Any]]:
    """Get all agents."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM agents WHERE deleted_at IS NULL ORDER BY created_at DESC"
        )
        rows = cursor.fetchall()
        return [_parse_agent_row(row) for row in rows]


def update_agent(
    agent_uuid: str,
    name: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> bool:
    """Update an agent. Returns True if the agent was found and updated."""
    # Build dynamic update query
    updates = []
    params = []

    if name is not None:
        updates.append("name = ?")
        params.append(name)
    if config is not None:
        updates.append("config = ?")
        # Serialize config to JSON string for storage
        params.append(json.dumps(config))

    if not updates:
        return False

    updates.append("updated_at = CURRENT_TIMESTAMP")
    params.append(agent_uuid)

    query = (
        f"UPDATE agents SET {', '.join(updates)} WHERE uuid = ? AND deleted_at IS NULL"
    )

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        conn.commit()
        updated = cursor.rowcount > 0
        if updated:
            logger.info(f"Updated agent with UUID: {agent_uuid}")
        return updated


def delete_agent(agent_uuid: str) -> bool:
    """Soft delete an agent. Returns True if the agent was found and deleted.
    Also soft deletes related agent_tools and agent_tests.
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE agents SET deleted_at = CURRENT_TIMESTAMP WHERE uuid = ? AND deleted_at IS NULL",
            (agent_uuid,),
        )
        deleted = cursor.rowcount > 0

        if deleted:
            # Soft delete related agent_tools
            cursor.execute(
                "UPDATE agent_tools SET deleted_at = CURRENT_TIMESTAMP WHERE agent_id = ? AND deleted_at IS NULL",
                (agent_uuid,),
            )
            # Soft delete related agent_tests
            cursor.execute(
                "UPDATE agent_tests SET deleted_at = CURRENT_TIMESTAMP WHERE agent_id = ? AND deleted_at IS NULL",
                (agent_uuid,),
            )
            logger.info(f"Soft deleted agent with UUID: {agent_uuid}")

        conn.commit()
        return deleted


def create_tool(
    name: str,
    description: str,
    config: Optional[Dict[str, Any]] = None,
) -> str:
    """Create a new tool and return its UUID."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        # Generate UUID for the tool
        tool_uuid = str(uuid.uuid4())
        # Serialize config to JSON string for storage
        config_json = json.dumps(config) if config is not None else None
        cursor.execute(
            """
            INSERT INTO tools (uuid, name, description, config)
            VALUES (?, ?, ?, ?)
            """,
            (tool_uuid, name, description, config_json),
        )
        conn.commit()
        logger.info(f"Created tool with UUID: {tool_uuid}")
        return tool_uuid


def _parse_tool_row(row: sqlite3.Row) -> Dict[str, Any]:
    """Parse a database row and deserialize JSON fields."""
    tool = dict(row)
    # Deserialize config from JSON string
    if tool.get("config"):
        tool["config"] = json.loads(tool["config"])

    return tool


def get_tool(tool_uuid: str) -> Optional[Dict[str, Any]]:
    """Get a tool by UUID."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM tools WHERE uuid = ? AND deleted_at IS NULL", (tool_uuid,)
        )
        row = cursor.fetchone()
        if row:
            return _parse_tool_row(row)
        return None


def get_all_tools() -> List[Dict[str, Any]]:
    """Get all tools."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM tools WHERE deleted_at IS NULL ORDER BY created_at DESC"
        )
        rows = cursor.fetchall()
        return [_parse_tool_row(row) for row in rows]


def update_tool(
    tool_uuid: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> bool:
    """Update a tool. Returns True if the tool was found and updated."""
    # Build dynamic update query
    updates = []
    params = []

    if name is not None:
        updates.append("name = ?")
        params.append(name)
    if description is not None:
        updates.append("description = ?")
        params.append(description)
    if config is not None:
        updates.append("config = ?")
        # Serialize config to JSON string for storage
        params.append(json.dumps(config))

    if not updates:
        return False

    updates.append("updated_at = CURRENT_TIMESTAMP")
    params.append(tool_uuid)

    query = (
        f"UPDATE tools SET {', '.join(updates)} WHERE uuid = ? AND deleted_at IS NULL"
    )

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        conn.commit()
        updated = cursor.rowcount > 0
        if updated:
            logger.info(f"Updated tool with UUID: {tool_uuid}")
        return updated


def delete_tool(tool_uuid: str) -> bool:
    """Soft delete a tool. Returns True if the tool was found and deleted.
    Also soft deletes related agent_tools entries.
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE tools SET deleted_at = CURRENT_TIMESTAMP WHERE uuid = ? AND deleted_at IS NULL",
            (tool_uuid,),
        )
        deleted = cursor.rowcount > 0

        if deleted:
            # Soft delete related agent_tools
            cursor.execute(
                "UPDATE agent_tools SET deleted_at = CURRENT_TIMESTAMP WHERE tool_id = ? AND deleted_at IS NULL",
                (tool_uuid,),
            )
            logger.info(f"Soft deleted tool with UUID: {tool_uuid}")

        conn.commit()
        return deleted


def add_tool_to_agent(agent_id: str, tool_id: str) -> int:
    """Add a tool to an agent. Returns the id of the created/restored link.
    If a soft-deleted link exists, it will be restored by unsetting deleted_at.
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        # Check if a soft-deleted link exists
        cursor.execute(
            "SELECT id FROM agent_tools WHERE agent_id = ? AND tool_id = ? AND deleted_at IS NOT NULL",
            (agent_id, tool_id),
        )
        existing = cursor.fetchone()
        if existing:
            # Restore the soft-deleted link
            cursor.execute(
                "UPDATE agent_tools SET deleted_at = NULL WHERE id = ?",
                (existing["id"],),
            )
            conn.commit()
            logger.info(f"Restored tool {tool_id} to agent {agent_id}")
            return existing["id"]
        else:
            # Insert new link
            cursor.execute(
                """
                INSERT INTO agent_tools (agent_id, tool_id)
                VALUES (?, ?)
                """,
                (agent_id, tool_id),
            )
            conn.commit()
            link_id = cursor.lastrowid
            logger.info(f"Added tool {tool_id} to agent {agent_id}")
            return link_id


def remove_tool_from_agent(agent_id: str, tool_id: str) -> bool:
    """Soft delete a tool from an agent. Returns True if the link was found and deleted."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE agent_tools SET deleted_at = CURRENT_TIMESTAMP WHERE agent_id = ? AND tool_id = ? AND deleted_at IS NULL",
            (agent_id, tool_id),
        )
        conn.commit()
        deleted = cursor.rowcount > 0

        if deleted:
            logger.info(f"Soft deleted tool {tool_id} from agent {agent_id}")

        return deleted


def get_tools_for_agent(agent_id: str) -> List[Dict[str, Any]]:
    """Get all tools associated with an agent."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT t.* FROM tools t
            INNER JOIN agent_tools at ON t.uuid = at.tool_id
            WHERE at.agent_id = ? AND at.deleted_at IS NULL AND t.deleted_at IS NULL
            ORDER BY at.created_at DESC
            """,
            (agent_id,),
        )
        rows = cursor.fetchall()
        return [_parse_tool_row(row) for row in rows]


def get_agents_for_tool(tool_id: str) -> List[Dict[str, Any]]:
    """Get all agents associated with a tool."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT a.* FROM agents a
            INNER JOIN agent_tools at ON a.uuid = at.agent_id
            WHERE at.tool_id = ? AND at.deleted_at IS NULL AND a.deleted_at IS NULL
            ORDER BY at.created_at DESC
            """,
            (tool_id,),
        )
        rows = cursor.fetchall()
        return [_parse_agent_row(row) for row in rows]


def get_agent_tool_link(agent_id: str, tool_id: str) -> Optional[Dict[str, Any]]:
    """Check if a specific agent-tool link exists."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM agent_tools WHERE agent_id = ? AND tool_id = ? AND deleted_at IS NULL",
            (agent_id, tool_id),
        )
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None


def get_all_agent_tools() -> List[Dict[str, Any]]:
    """Get all agent-tool links."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM agent_tools WHERE deleted_at IS NULL ORDER BY created_at DESC"
        )
        rows = cursor.fetchall()
        return [dict(row) for row in rows]


# ============ Tests Functions ============


def create_test(
    name: str,
    type: str,
    config: Optional[Dict[str, Any]] = None,
) -> str:
    """Create a new test and return its UUID."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        test_uuid = str(uuid.uuid4())
        config_json = json.dumps(config) if config is not None else None
        cursor.execute(
            """
            INSERT INTO tests (uuid, name, type, config)
            VALUES (?, ?, ?, ?)
            """,
            (test_uuid, name, type, config_json),
        )
        conn.commit()
        logger.info(f"Created test with UUID: {test_uuid}")
        return test_uuid


def _parse_test_row(row: sqlite3.Row) -> Dict[str, Any]:
    """Parse a database row and deserialize JSON fields."""
    test = dict(row)
    if test.get("config"):
        test["config"] = json.loads(test["config"])
    return test


def get_test(test_uuid: str) -> Optional[Dict[str, Any]]:
    """Get a test by UUID."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM tests WHERE uuid = ? AND deleted_at IS NULL", (test_uuid,)
        )
        row = cursor.fetchone()
        if row:
            return _parse_test_row(row)
        return None


def get_all_tests() -> List[Dict[str, Any]]:
    """Get all tests."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM tests WHERE deleted_at IS NULL ORDER BY created_at DESC"
        )
        rows = cursor.fetchall()
        return [_parse_test_row(row) for row in rows]


def update_test(
    test_uuid: str,
    name: Optional[str] = None,
    type: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> bool:
    """Update a test. Returns True if the test was found and updated."""
    updates = []
    params = []

    if name is not None:
        updates.append("name = ?")
        params.append(name)
    if type is not None:
        updates.append("type = ?")
        params.append(type)
    if config is not None:
        updates.append("config = ?")
        params.append(json.dumps(config))

    if not updates:
        return False

    updates.append("updated_at = CURRENT_TIMESTAMP")
    params.append(test_uuid)

    query = (
        f"UPDATE tests SET {', '.join(updates)} WHERE uuid = ? AND deleted_at IS NULL"
    )

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        conn.commit()
        updated = cursor.rowcount > 0
        if updated:
            logger.info(f"Updated test with UUID: {test_uuid}")
        return updated


def delete_test(test_uuid: str) -> bool:
    """Soft delete a test. Returns True if the test was found and deleted.
    Also soft deletes related agent_tests entries.
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE tests SET deleted_at = CURRENT_TIMESTAMP WHERE uuid = ? AND deleted_at IS NULL",
            (test_uuid,),
        )
        deleted = cursor.rowcount > 0

        if deleted:
            # Soft delete related agent_tests
            cursor.execute(
                "UPDATE agent_tests SET deleted_at = CURRENT_TIMESTAMP WHERE test_id = ? AND deleted_at IS NULL",
                (test_uuid,),
            )
            logger.info(f"Soft deleted test with UUID: {test_uuid}")

        conn.commit()
        return deleted


# ============ Agent Tests Functions ============


def add_test_to_agent(agent_id: str, test_id: str) -> int:
    """Add a test to an agent. Returns the id of the created/restored link.
    If a soft-deleted link exists, it will be restored by unsetting deleted_at.
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        # Check if a soft-deleted link exists
        cursor.execute(
            "SELECT id FROM agent_tests WHERE agent_id = ? AND test_id = ? AND deleted_at IS NOT NULL",
            (agent_id, test_id),
        )
        existing = cursor.fetchone()
        if existing:
            # Restore the soft-deleted link
            cursor.execute(
                "UPDATE agent_tests SET deleted_at = NULL WHERE id = ?",
                (existing["id"],),
            )
            conn.commit()
            logger.info(f"Restored test {test_id} to agent {agent_id}")
            return existing["id"]
        else:
            # Insert new link
            cursor.execute(
                """
                INSERT INTO agent_tests (agent_id, test_id)
                VALUES (?, ?)
                """,
                (agent_id, test_id),
            )
            conn.commit()
            link_id = cursor.lastrowid
            logger.info(f"Added test {test_id} to agent {agent_id}")
            return link_id


def remove_test_from_agent(agent_id: str, test_id: str) -> bool:
    """Soft delete a test from an agent. Returns True if the link was found and deleted."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE agent_tests SET deleted_at = CURRENT_TIMESTAMP WHERE agent_id = ? AND test_id = ? AND deleted_at IS NULL",
            (agent_id, test_id),
        )
        conn.commit()
        deleted = cursor.rowcount > 0

        if deleted:
            logger.info(f"Soft deleted test {test_id} from agent {agent_id}")

        return deleted


def get_tests_for_agent(agent_id: str) -> List[Dict[str, Any]]:
    """Get all tests associated with an agent."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT t.* FROM tests t
            INNER JOIN agent_tests at ON t.uuid = at.test_id
            WHERE at.agent_id = ? AND at.deleted_at IS NULL AND t.deleted_at IS NULL
            ORDER BY at.created_at DESC
            """,
            (agent_id,),
        )
        rows = cursor.fetchall()
        return [_parse_test_row(row) for row in rows]


def get_agents_for_test(test_id: str) -> List[Dict[str, Any]]:
    """Get all agents associated with a test."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT a.* FROM agents a
            INNER JOIN agent_tests at ON a.uuid = at.agent_id
            WHERE at.test_id = ? AND at.deleted_at IS NULL AND a.deleted_at IS NULL
            ORDER BY at.created_at DESC
            """,
            (test_id,),
        )
        rows = cursor.fetchall()
        return [_parse_agent_row(row) for row in rows]


def get_agent_test_link(agent_id: str, test_id: str) -> Optional[Dict[str, Any]]:
    """Check if a specific agent-test link exists."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM agent_tests WHERE agent_id = ? AND test_id = ? AND deleted_at IS NULL",
            (agent_id, test_id),
        )
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None


def get_all_agent_tests() -> List[Dict[str, Any]]:
    """Get all agent-test links."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM agent_tests WHERE deleted_at IS NULL ORDER BY created_at DESC"
        )
        rows = cursor.fetchall()
        return [dict(row) for row in rows]


# ============ Jobs Functions ============


def create_job(
    job_type: str,
    status: str = "in_progress",
    details: Optional[Dict[str, Any]] = None,
    results: Optional[Dict[str, Any]] = None,
) -> str:
    """Create a new job and return its UUID.

    Args:
        job_type: Type of job (stt-eval, tts-eval, llm-unit-test, llm-benchmark)
        status: Initial status (defaults to 'in_progress')
        details: JSON config needed to re-trigger the job if interrupted
        results: Initial results (usually None)
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        job_uuid = str(uuid.uuid4())
        details_json = json.dumps(details) if details is not None else None
        results_json = json.dumps(results) if results is not None else None
        cursor.execute(
            """
            INSERT INTO jobs (uuid, type, status, details, results)
            VALUES (?, ?, ?, ?, ?)
            """,
            (job_uuid, job_type, status, details_json, results_json),
        )
        conn.commit()
        logger.info(f"Created job with UUID: {job_uuid}, type: {job_type}")
        return job_uuid


def _parse_job_row(row: sqlite3.Row) -> Dict[str, Any]:
    """Parse a job database row and deserialize JSON fields."""
    job = dict(row)
    if job.get("details"):
        job["details"] = json.loads(job["details"])
    if job.get("results"):
        job["results"] = json.loads(job["results"])
    return job


def get_job(job_uuid: str) -> Optional[Dict[str, Any]]:
    """Get a job by UUID."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM jobs WHERE uuid = ?", (job_uuid,))
        row = cursor.fetchone()
        if row:
            return _parse_job_row(row)
        return None


def get_all_jobs(job_type: Optional[str] = None) -> List[Dict[str, Any]]:
    """Get all jobs, optionally filtered by type."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        if job_type:
            cursor.execute(
                "SELECT * FROM jobs WHERE type = ? ORDER BY created_at DESC",
                (job_type,),
            )
        else:
            cursor.execute("SELECT * FROM jobs ORDER BY created_at DESC")
        rows = cursor.fetchall()
        return [_parse_job_row(row) for row in rows]


def get_pending_jobs() -> List[Dict[str, Any]]:
    """Get all jobs with status 'in_progress' (for recovery on restart)."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM jobs WHERE status = 'in_progress' ORDER BY created_at ASC"
        )
        rows = cursor.fetchall()
        return [_parse_job_row(row) for row in rows]


def update_job(
    job_uuid: str,
    status: Optional[str] = None,
    results: Optional[Dict[str, Any]] = None,
) -> bool:
    """Update a job. Returns True if the job was found and updated."""
    updates = []
    params = []

    if status is not None:
        updates.append("status = ?")
        params.append(status)
    if results is not None:
        updates.append("results = ?")
        params.append(json.dumps(results))

    if not updates:
        return False

    updates.append("updated_at = CURRENT_TIMESTAMP")
    params.append(job_uuid)

    query = f"UPDATE jobs SET {', '.join(updates)} WHERE uuid = ?"

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        conn.commit()
        updated = cursor.rowcount > 0
        if updated:
            logger.info(f"Updated job with UUID: {job_uuid}")
        return updated


def delete_job(job_uuid: str) -> bool:
    """Delete a job. Returns True if the job was found and deleted."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM jobs WHERE uuid = ?", (job_uuid,))
        conn.commit()
        deleted = cursor.rowcount > 0
        if deleted:
            logger.info(f"Deleted job with UUID: {job_uuid}")
        return deleted
