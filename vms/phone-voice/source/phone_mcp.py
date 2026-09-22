#!/usr/bin/env python3
"""stdio MCP adapter usable by any local Codex session."""

import os
import uuid

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from control import request

mcp = FastMCP(
    "codex_phone",
    instructions="The owner has a Cisco desk phone and authorizes clarification calls about ongoing work. "
    "Use ask_user when you need their input. It rings only their own handset and returns their actual words. "
    "Respond conversationally: paraphrase their intent, accept corrections, and ask for confirmation only "
    "when meaning or authorization is unclear. Do not turn every answer into a yes/no confirmation loop. "
    "A missed call is not consent. Keep the returned session_id and question_id; use get_answer for later answers. "
    "After a phone answer, publish your brief conversational response with update_session so they hear it. "
    "Publish progress with update_session so "
    "the owner can call back to check on work. The original Codex process remains responsible for its task.")
SESSION_KEY = os.environ.get("CODEX_THREAD_ID") or "mcp-" + str(uuid.uuid4())
SESSION_ID = None
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)


async def ensure_session(title, session_id=None):
    global SESSION_ID
    if session_id:
        return (await request("session", session_id=session_id))["id"]
    if SESSION_ID is None:
        result = await request("register", session_key=SESSION_KEY, title=title, cwd=os.getcwd())
        SESSION_ID = result["id"]
    return SESSION_ID


@mcp.tool(annotations=WRITE)
async def ask_user(question: str, options: list[str] | None = None,
                   title: str = "Codex task", session_id: str | None = None,
                   wait_seconds: int = 90) -> dict:
    """Call the owner's desk phone to ask one clarification question and await their answer.

    Use normal prose and up to six short options. The owner can speak freely.
    The call is queued while their phone is busy and rings once. If the result
    is pending, do independent work and use get_answer; do not assume consent
    or place another call for the same question. Keep the returned session_id
    for update_session and check_messages. No arbitrary telephone numbers.
    """
    if not question.strip() or len(question) > 3000 or len(options or []) > 6:
        raise ValueError("Use a question of at most 3000 characters and at most six options")
    number = await ensure_session(title, session_id)
    record = await request("ask", session_id=number, request_key="mcp:" + str(uuid.uuid4()),
                           questions=[{"id": "answer", "question": question, "header": "Question",
                                       "options": [{"label": value, "description": ""} for value in options or []]}])
    result = await request("question", question_id=record["id"], wait_seconds=max(0, min(wait_seconds, 120)))
    return {"question_id": result["id"], "session_id": number, "extension": result["extension"],
            "state": result["state"], "answers": result["answers"],
            "call_state": result["ring_state"],
            "instructions": "Pending means no answer yet, not approval. Use get_answer with this question_id."}


@mcp.tool(annotations=READ)
async def get_answer(question_id: str, wait_seconds: int = 50) -> dict:
    """Collect the caller's saved phone answer, optionally waiting. Does not ring again."""
    result = await request("question", question_id=question_id, wait_seconds=max(0, min(wait_seconds, 120)))
    return {"question_id": result["id"], "session_id": result["session_id"],
            "extension": result["extension"], "state": result["state"], "answers": result["answers"]}


@mcp.tool(annotations=WRITE)
async def update_session(summary: str, state: str = "running", title: str = "Codex task",
                         session_id: str | None = None) -> dict:
    """Publish a short, truthful progress/result summary the owner can hear by dialing this session.

    States: idle, running, waiting, done, error, interrupted. Keep the returned
    session_id across calls. This registers the current external session if needed.
    """
    number = await ensure_session(title, session_id)
    return await request("update", session_id=number, state=state, summary=summary)


@mcp.tool(annotations=WRITE)
async def check_messages(session_id: str | None = None) -> dict:
    """Collect and acknowledge the owner's callback messages, including explicit stop requests.

    External desktop sessions should check this between long work steps. The
    bridge cannot forcibly interrupt a desktop session owned by another host.
    """
    number = await ensure_session("Codex task", session_id)
    return {"session_id": number, "messages": await request("inbox", session_id=number, acknowledge=True)}


@mcp.tool(annotations=READ)
async def list_sessions() -> list[dict]:
    """List phone callback extensions and their latest reported work state. Does not ring."""
    return await request("sessions")


if __name__ == "__main__":
    mcp.run(transport="stdio")
