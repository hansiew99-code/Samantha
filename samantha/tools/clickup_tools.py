"""clickup_* tools (BRIEF §8). The owner's own tasks — mutations auto-allowed."""

from __future__ import annotations

import sqlite3

from ..integrations.clickup import ClickUpClient, ClickUpSync
from .registry import Tool, ToolRegistry


def register(
    registry: ToolRegistry,
    conn: sqlite3.Connection,
    client: ClickUpClient,
    sync: ClickUpSync,
) -> None:
    async def clickup_list_tasks(refresh: bool = False) -> str:
        if refresh:
            await sync.poll()
        rows = conn.execute(
            "SELECT external_id, title, due_at, status FROM tasks "
            "WHERE source = 'clickup' AND status = 'open' "
            "ORDER BY due_at IS NULL, due_at LIMIT 25"
        ).fetchall()
        if not rows:
            return "No open ClickUp tasks."
        return "\n".join(
            f"[{r['external_id']}] {r['title']}"
            + (f" — due {r['due_at']}" if r["due_at"] else "")
            for r in rows
        )

    async def clickup_complete_task(task_id: str) -> str:
        await client.set_status(task_id, "complete")
        conn.execute(
            "UPDATE tasks SET status = 'done', updated_at = datetime('now') "
            "WHERE source = 'clickup' AND external_id = ?",
            (task_id,),
        )
        conn.commit()
        return f"Task {task_id} marked complete."

    async def clickup_update_task(task_id: str, status: str) -> str:
        await client.set_status(task_id, status)
        return f"Task {task_id} moved to '{status}'."

    registry.register(Tool(
        name="clickup_list_tasks",
        description=(
            "List the owner's open ClickUp tasks (locally synced every ~10 "
            "min; pass refresh=true for a live pull). Call before answering "
            "anything about their tasks or workload."
        ),
        input_schema={
            "type": "object",
            "properties": {"refresh": {"type": "boolean"}},
        },
        func=clickup_list_tasks,
    ))

    registry.register(Tool(
        name="clickup_complete_task",
        description="Mark a ClickUp task complete when the owner says it's done. task_id from clickup_list_tasks.",
        input_schema={
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
        func=clickup_complete_task,
    ))

    registry.register(Tool(
        name="clickup_update_task",
        description="Move a ClickUp task to a named status (e.g. 'in progress'). Statuses must exist in the owner's workspace.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "status": {"type": "string"},
            },
            "required": ["task_id", "status"],
        },
        func=clickup_update_task,
    ))
