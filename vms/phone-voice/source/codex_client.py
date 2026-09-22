"""Async client for the installed Codex app-server's stdio protocol."""

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path

LOG = logging.getLogger("codex-phone.codex")
ROOT = Path(os.environ.get("CODEX_PHONE_WORKSPACE", Path(__file__).resolve().parents[1])).resolve()
INSTRUCTIONS = """
You are Codex conversing with the owner of this computer over their Cisco phone.
Your replies are spoken aloud by a local speech synthesizer. Use natural, concise
speech: usually one to three sentences. Avoid Markdown, lists, URLs, and reading
out code unless requested. Ask one short question when clarification is needed.
The user's input is a speech transcript and may contain recognition mistakes.
Clarify ambiguous instructions before making consequential changes.
When checking an interpretation, repeat what you understood in the question
and invite a correction. The owner prefers this to a bare yes/no prompt.
Be a conversational partner, not a transcription echo. Briefly paraphrase the
meaning of a task or an answer in your response. Accept natural corrections
and combined confirmations such as "That's right. Yes." or "Done. Correct."
Only ask for confirmation when the meaning or authorization is unclear;
do not require a second confirmation of every ordinary answer. Replies to
phone questions are the caller's actual words, which may themselves be a
follow-up question or feedback. Respond to what they mean and clarify as needed.
For a substantive task, briefly say what you understood before starting work.
You are an actual Codex session with this project's workspace tools, not a
simulation. Use tools when the user asks you to do work, and verify outcomes.
Do not claim to have done something you have not done. Requests requiring
permissions unavailable in this workspace must be explained to the caller.
Do not modify the running voice bridge or PBX during this call unless explicitly
asked. The owner has authorized calls to their desk phone for clarification about
tasks you are building. Prefer the normal request_user_input tool when available;
the host routes it to the phone. Otherwise use phone_ask_user. Use these tools
for questions that block your work instead of ending your turn with a question.
The caller may hang up while you work. Keep working until the assigned task is
done or you need an answer. Hanging up is not a request to stop.
Only an explicit stop/cancel instruction interrupts your task. The phone host
manages and announces your callback number. Never invent phone numbers.
Do not call other people or send external messages without explicit instructions.

Context: We recovered the owner's Cisco CP-7945G, MAC 0C2724317F2E, with SCCP
9.4(2)SR3 firmware. It is registered to the local Asterisk PBX as extension 6738.
This voice bridge is extension 611. The phone is at 192.168.0.197.
The owner asked to talk to Codex from the phone.
This conversation persists across calls and can be resumed by its extension.
"""
INSTRUCTIONS += "\nPBX/TFTP host: " + os.environ.get("CODEX_PHONE_PBX_HOST", "192.168.0.105") + ".\n"

PHONE_TOOL = {
    "type": "function", "name": "phone_ask_user",
    "description": "Ask the owner a clarification question about the current task. If they hung up, the host calls their desk phone. Returns their spoken answer or follow-up question; respond conversationally and clarify meaning when needed. Use when normal request_user_input is unavailable. Hangup without an answer never cancels the question or implies consent.",
    "inputSchema": {
        "type": "object", "properties": {
            "question": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}},
        }, "required": ["question"], "additionalProperties": False,
    },
}


def app_server_command():
    return [os.environ.get("CODEX_BIN", "/usr/bin/codex"), "app-server", "--listen", "stdio://",
            "-c", 'model_reasoning_effort="low"', "-c", 'sandbox_mode="danger-full-access"',
            "-c", 'approval_policy="never"', "-c", 'web_search="live"', "-c", "agents.enabled=true"]


class CodexClient:
    def __init__(self, socket_path=None, host="proxmox"):
        self.socket_path, self.host = socket_path, host
        self.reader = self.writer = None
        self.start_lock = asyncio.Lock()
        self.process = None
        self.serial = 0
        self.pending = {}
        self.turns = {}
        self.reader_task = self.stderr_task = None
        self.on_request = self.on_event = None
        self.server_requests = set()
        self.loaded = set()

    async def start(self):
        async with self.start_lock:
            if self.reader_task and not self.reader_task.done():
                return
            await self.close()
            try:
                await self._start()
            except Exception as error:
                await self.close()
                if self.socket_path:
                    raise RuntimeError("The workstation is unavailable. Turn it on and reconnect its phone tunnel, then tell this session to continue.") from error
                raise

    async def _start(self):
        if self.socket_path:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self.socket_path), limit=8 * 1024 * 1024), 5)
        else:
            self.process = await asyncio.create_subprocess_exec(
                *app_server_command(),
                cwd=ROOT, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=8 * 1024 * 1024,
            )
            self.reader, self.writer = self.process.stdout, self.process.stdin
            self.stderr_task = asyncio.create_task(self._stderr())
        self.reader_task = asyncio.create_task(self._read())
        await self.request("initialize", {
            "clientInfo": {"name": "codex_phone", "title": "Codex Phone", "version": "2.0.0"},
            "capabilities": {"experimentalApi": True},
        }, timeout=15)
        await self.send({"method": "initialized", "params": {}})
        LOG.info("Codex app-server ready on %s", self.host)

    async def send(self, message):
        self.writer.write((json.dumps(message) + "\n").encode())
        await self.writer.drain()

    async def request(self, method, params, timeout=90):
        self.serial += 1
        request_id = self.serial
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(request_id, None)

    async def new_thread(self, cwd=None, number=None):
        await self.start()
        instructions = INSTRUCTIONS + f"\nThis session runs on the {self.host} host. Its workspace is {cwd or ROOT}.\n"
        if number:
            instructions += f"\nYour persistent phone extension is 611{number:03d}.\n"
        result = await self.request("thread/start", {
            "cwd": str(cwd or ROOT), "sandbox": "danger-full-access", "approvalPolicy": "never",
            "developerInstructions": instructions, "serviceName": "codex-phone",
            "dynamicTools": [PHONE_TOOL],
        })
        thread_id = result["thread"]["id"]
        self.loaded.add(thread_id)
        LOG.info("New conversation: %s; model: %s", thread_id, result.get("model"))
        return thread_id

    async def resume(self, thread_id, cwd=None, number=None):
        await self.start()
        if thread_id in self.loaded:
            return
        instructions = INSTRUCTIONS + f"\nThis session runs on the {self.host} host. Its workspace is {cwd or ROOT}.\n"
        instructions += (f"\nYour persistent phone extension is 611{number:03d}.\n" if number else "")
        await self.request("thread/resume", {
            "threadId": thread_id, "cwd": str(cwd or ROOT),
            "sandbox": "danger-full-access", "approvalPolicy": "never",
            "developerInstructions": instructions, "dynamicTools": [PHONE_TOOL],
        })
        self.loaded.add(thread_id)

    async def ask(self, thread_id, text):
        if thread_id in self.turns:
            raise RuntimeError("A Codex turn is already running on this call")
        future = asyncio.get_running_loop().create_future()
        state = {"future": future, "messages": [], "turn_id": None, "started": asyncio.Event()}
        self.turns[thread_id] = state
        try:
            result = await self.request("turn/start", {
                "threadId": thread_id,
                "input": [{"type": "text", "text": text}], "effort": "low",
            })
            state["turn_id"] = result["turn"]["id"]
            state["started"].set()
            # No phone-call timeout: the session manager owns this task.
            return await asyncio.shield(future)
        finally:
            state["started"].set()
            self.turns.pop(thread_id, None)

    async def interrupt(self, thread_id):
        state = self.turns.get(thread_id)
        if not state:
            return False
        await state["started"].wait()
        if not state["turn_id"] or state["future"].done():
            return False
        await self.request("turn/interrupt", {"threadId": thread_id, "turnId": state["turn_id"]})
        return True

    async def steer(self, thread_id, text):
        state = self.turns.get(thread_id)
        if not state:
            return False
        await state["started"].wait()
        if not state["turn_id"] or state["future"].done():
            return False
        try:
            await self.request("turn/steer", {
                "threadId": thread_id, "expectedTurnId": state["turn_id"],
                "input": [{"type": "text", "text": text}],
            })
            return True
        except RuntimeError:
            if state["future"].done():
                return False
            raise

    async def _answer_request(self, message):
        try:
            if not self.on_request:
                raise ValueError("No phone request handler")
            result = await self.on_request(message["method"], message.get("params", {}))
            await self.send({"id": message["id"], "result": result})
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOG.warning("Request %s failed: %s", message["method"], error)
            await self.send({"id": message["id"], "error": {"code": -32603, "message": str(error)}})

    async def _read(self):
        try:
            while True:
                line = await self.reader.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "id" in message and "method" not in message:
                    future = self.pending.get(message["id"])
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(RuntimeError(str(message["error"])))
                        else:
                            future.set_result(message.get("result", {}))
                    continue
                if "id" in message:
                    # Keep consuming other threads' events while the phone waits.
                    task = asyncio.create_task(self._answer_request(message))
                    self.server_requests.add(task)
                    task.add_done_callback(self.server_requests.discard)
                    continue
                params = message.get("params", {})
                if self.on_event:
                    self.on_event(message.get("method"), params)
                state = self.turns.get(params.get("threadId"))
                if not state:
                    continue
                if message.get("method") == "item/completed":
                    item = params.get("item", {})
                    if item.get("type") == "agentMessage" and item.get("text"):
                        state["messages"].append(item)
                elif message.get("method") == "turn/completed":
                    future = state["future"]
                    if future.done():
                        continue
                    turn = params["turn"]
                    if turn.get("status") != "completed":
                        future.set_exception(RuntimeError(str(turn.get("error") or turn.get("status"))))
                    else:
                        messages = state["messages"]
                        finals = [m["text"] for m in messages if m.get("phase") == "final_answer"]
                        text = "\n".join(finals) if finals else (messages[-1]["text"] if messages else "")
                        future.set_result(text.strip())
        except Exception:
            LOG.exception("Codex event reader failed")
        finally:
            self.loaded.clear()
            for future in list(self.pending.values()) + [s["future"] for s in self.turns.values()]:
                if not future.done():
                    message = ("The workstation connection was lost. Its task may still be running. Reconnect before continuing."
                               if self.socket_path else "Codex app-server disconnected")
                    future.set_exception(RuntimeError(message))

    async def _stderr(self):
        while line := await self.process.stderr.readline():
            LOG.debug("app-server: %s", line.decode(errors="replace").rstrip())

    async def close(self):
        for task in list(self.server_requests):
            task.cancel()
        await asyncio.gather(*self.server_requests, return_exceptions=True)
        if self.writer is not None:
            self.writer.close()
            with contextlib.suppress(ConnectionError, BrokenPipeError):
                await self.writer.wait_closed()
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 8)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        for task in (self.reader_task, self.stderr_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self.loaded.clear()
        self.reader = self.writer = None
