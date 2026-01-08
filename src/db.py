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
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
                UNIQUE(agent_id, tool_id),
                FOREIGN KEY (agent_id) REFERENCES agents(uuid),
                FOREIGN KEY (tool_id) REFERENCES tools(uuid)
            )
        """
        )
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
        cursor.execute("SELECT * FROM agents WHERE uuid = ?", (agent_uuid,))
        row = cursor.fetchone()
        if row:
            return _parse_agent_row(row)
        return None


def get_all_agents() -> List[Dict[str, Any]]:
    """Get all agents."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM agents ORDER BY created_at DESC")
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

    query = f"UPDATE agents SET {', '.join(updates)} WHERE uuid = ?"

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        conn.commit()
        updated = cursor.rowcount > 0
        if updated:
            logger.info(f"Updated agent with UUID: {agent_uuid}")
        return updated


def delete_agent(agent_uuid: str) -> bool:
    """Delete an agent. Returns True if the agent was found and deleted."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM agents WHERE uuid = ?", (agent_uuid,))
        conn.commit()
        deleted = cursor.rowcount > 0

        if deleted:
            logger.info(f"Deleted agent with UUID: {agent_uuid}")

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
        cursor.execute("SELECT * FROM tools WHERE uuid = ?", (tool_uuid,))
        row = cursor.fetchone()
        if row:
            return _parse_tool_row(row)
        return None


def get_all_tools() -> List[Dict[str, Any]]:
    """Get all tools."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tools ORDER BY created_at DESC")
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

    query = f"UPDATE tools SET {', '.join(updates)} WHERE uuid = ?"

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        conn.commit()
        updated = cursor.rowcount > 0
        if updated:
            logger.info(f"Updated tool with UUID: {tool_uuid}")
        return updated


def delete_tool(tool_uuid: str) -> bool:
    """Delete a tool. Returns True if the tool was found and deleted."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM tools WHERE uuid = ?", (tool_uuid,))
        conn.commit()
        deleted = cursor.rowcount > 0

        if deleted:
            logger.info(f"Deleted tool with UUID: {tool_uuid}")

        return deleted


def add_tool_to_agent(agent_id: str, tool_id: str) -> int:
    """Add a tool to an agent. Returns the id of the created link."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
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
    """Remove a tool from an agent. Returns True if the link was found and deleted."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM agent_tools WHERE agent_id = ? AND tool_id = ?",
            (agent_id, tool_id),
        )
        conn.commit()
        deleted = cursor.rowcount > 0

        if deleted:
            logger.info(f"Removed tool {tool_id} from agent {agent_id}")

        return deleted


def get_tools_for_agent(agent_id: str) -> List[Dict[str, Any]]:
    """Get all tools associated with an agent."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT t.* FROM tools t
            INNER JOIN agent_tools at ON t.uuid = at.tool_id
            WHERE at.agent_id = ?
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
            WHERE at.tool_id = ?
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
            "SELECT * FROM agent_tools WHERE agent_id = ? AND tool_id = ?",
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
        cursor.execute("SELECT * FROM agent_tools ORDER BY created_at DESC")
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
