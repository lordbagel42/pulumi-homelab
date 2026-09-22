"""Amp runner adapter using the supported CLI streaming protocol.

References: https://ampcode.com/docs/cli/execute-mode,
https://ampcode.com/docs/cli/streaming-json, and
https://ampcode.com/docs/cli/spawning-orbs.

The CLI is only an observer of remote execution. Losing or terminating that
process MUST NOT be reported as stopping the runner. Optional ``control`` is
an async callback backed by the documented Amp PluginThread API; cancel must
acknowledge an idle/error state before this adapter reports interruption.
"""

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import uuid

LOG = logging.getLogger("switchboard.amp")
THREAD_ID = re.compile(r"T-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
PENDING_PREFIX = "amp-pending:"

PHONE_INSTRUCTIONS = """You are Amp speaking with the owner through their Cisco desk phone and Switchboard.
The caller's messages are speech transcripts and can contain recognition mistakes.
Reply in natural, concise speech, normally one to three sentences. Avoid Markdown,
code, and reading URLs unless requested; your native Amp thread remains the audit trail.
Briefly say what you understood before substantive work, use your actual tools to do
the work, and verify outcomes. Never claim actions or permissions you do not have.
Clarify ambiguous consequential instructions with one short question. When checking
an interpretation, paraphrase what you understood and invite correction. Accept
natural confirmations and do not require a second confirmation of ordinary answers.
If you need the owner while working, use the configured phone clarification tools
when available, always attaching to the existing Switchboard session given below.
Wait for an actual answer; a missed call or hangup is not consent. Otherwise ask your
question in your spoken reply so the owner can answer on their next turn.
The owner may hang up while you work. Keep working; only an explicit stop instruction
cancels the task. Do not call other people or send external messages without explicit
instructions. Keep Amp's configured permissions; a phone question is not permission
escalation. Do not change the live voice bridge or PBX unless explicitly asked.
"""


def thread_url(thread_id):
    """Only native thread IDs may become links in the Switchboard UI."""
    if not isinstance(thread_id, str) or not THREAD_ID.fullmatch(thread_id):
        return ""
    return "https://ampcode.com/threads/" + thread_id


class AmpClient:
    """Small session backend whose conversations live on an Amp runner.

    ``new_thread`` reserves a local handle without creating a remote thread or
    using inference. The first ``ask`` receives the real ID and synchronously
    emits ``thread/started`` with ``previousThreadId`` and ``number``. Persist
    this binding before processing subsequent events. After a bridge restart,
    an unresolved reservation cannot be retried automatically: creation might
    already have happened remotely.

    ``on_event(method, params)`` receives normalized progress events plus every
    original public stream message as ``amp/event`` for audit consumers.
    """

    engine = "amp"

    def __init__(self, host="proxmox", *, runner_id=None, runner_dir=None,
                 binary=None, mode=None, visibility=None, control=None):
        self.host = host
        self.runner_id = runner_id or os.environ.get("AMP_RUNNER_ID", "homelab-amp")
        if not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", self.runner_id):
            raise ValueError("AMP_RUNNER_ID must be an Amp runner hostname")
        self.executor = "runner:" + self.runner_id
        self.default_cwd = runner_dir or os.environ.get("AMP_RUNNER_DIR", "/home/amp/workspaces")
        self.default_cwd = self.workspace(self.default_cwd)
        self.binary = binary or os.environ.get("AMP_BIN") or shutil.which("amp") or "amp"
        self.mode = mode or os.environ.get("AMP_MODE", "medium")
        self.visibility = visibility or os.environ.get("AMP_THREAD_VISIBILITY", "private")
        if self.visibility not in {"private", "unlisted", "workspace", "group"}:
            raise ValueError("Invalid Amp thread visibility")
        self.control = control
        self.on_request = self.on_event = None
        self.contexts = {}
        self.aliases = {}
        self.turns = {}
        self.closed = False

    @property
    def configured(self):
        """Local prerequisites only; this does not assert the runner is online."""
        if not shutil.which(self.binary):
            return False
        if os.environ.get("AMP_API_KEY", "").strip():
            return True
        data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
        try:
            return (data_home / "amp/secrets.json").stat().st_size > 2
        except OSError:
            return False

    def workspace(self, cwd=None):
        value = str(cwd or self.default_cwd)
        path = PurePosixPath(value)
        if not path.is_absolute() or ".." in path.parts or "\x00" in value:
            raise ValueError("Use an absolute directory served by the Amp runner")
        # The directory belongs to the runner, not to this phone service host.
        return str(path)

    async def start(self):
        if self.closed:
            raise RuntimeError("The Amp phone adapter is closed")
        if not shutil.which(self.binary):
            raise RuntimeError("The Amp CLI is not installed on the phone bridge; configure AMP_BIN")
        if not self.configured:
            raise RuntimeError("Sign in to Amp as the phone bridge service account, or configure its AMP_API_KEY. "
                               "The runner also needs its own signed-in Amp account.")

    async def new_thread(self, cwd=None, number=None):
        await self.start()
        handle = PENDING_PREFIX + str(uuid.uuid4())
        self.contexts[handle] = {"cwd": self.workspace(cwd), "number": number, "native": None, "submitted": False}
        return handle

    async def resume(self, thread_id, cwd=None, number=None):
        await self.start()
        resolved = self.aliases.get(thread_id, thread_id)
        if resolved.startswith(PENDING_PREFIX):
            if resolved in self.contexts:
                return
            raise RuntimeError("The bridge lost the first Amp connection before saving its thread ID. "
                               "The task may already exist in Amp; check Amp before starting it again.")
        if not THREAD_ID.fullmatch(resolved):
            raise ValueError("This is not a native Amp thread; start a new Amp session")
        self.contexts[resolved] = {"cwd": self.workspace(cwd), "number": number, "native": resolved}

    def _emit(self, method, state, **params):
        if self.on_event:
            self.on_event(method, {
                "threadId": state["context"]["native"] or state["handle"],
                "number": state["context"]["number"], "engine": "amp",
                "executor": self.executor, **params,
            })

    def _bind(self, state, native):
        context = state["context"]
        if not isinstance(native, str) or not THREAD_ID.fullmatch(native):
            raise RuntimeError("Amp returned an invalid native thread ID")
        if context["native"]:
            if native != context["native"]:
                raise RuntimeError("Amp returned events for an unexpected thread")
            return
        context["native"] = native
        self.aliases[state["handle"]] = native
        self.contexts[native] = context
        self.turns[native] = state
        self._emit("thread/started", state, previousThreadId=state["handle"], threadUrl=thread_url(native))
        state["ready"].set()

    def _command(self, context):
        if context["native"]:
            # --orb-execute continues the thread on its own remote executor,
            # including runners; never accidentally attach as a local executor.
            args = [self.binary, "threads", "continue", context["native"],
                    "--execute", "--orb-execute", "--stream-json"]
        else:
            args = [self.binary, "--execute", "--stream-json", "--executor", self.executor,
                    "--runner-dir", context["cwd"], "--mode", self.mode,
                    "--visibility", self.visibility, "--no-archive-after-execute"]
            if context["number"]:
                args += ["--title", f"Switchboard 611{context['number']:03d}"]
        return args + ["--no-color", "--no-notifications"]

    def _prompt(self, context, text):
        number = context["number"]
        session = (f"Existing Switchboard session_id: {number}; phone extension: 611{number:03d}.\n"
                   if number else "")
        preface = "" if context["native"] else PHONE_INSTRUCTIONS + "\n"
        return preface + session + f"Runner: {self.runner_id}. Workspace: {context['cwd']}.\n\nCaller said:\n" + text

    async def ask(self, thread_id, text):
        await self.start()
        thread_id = self.aliases.get(thread_id, thread_id)
        if thread_id in self.turns:
            raise RuntimeError("An Amp turn is already running in this session")
        if thread_id not in self.contexts:
            raise ValueError("Resume the Amp thread before sending it a message")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Provide a nonempty message for Amp")
        context = self.contexts[thread_id]
        if not context["native"] and context.get("submitted"):
            raise RuntimeError("The first Amp task was submitted without a confirmed thread ID. "
                               "Check Amp before starting it again; the phone bridge will not replay it.")
        state = {"handle": thread_id, "context": context, "process": None,
                 "ready": asyncio.Event(), "done": asyncio.Event(), "interrupted": False,
                 "result": None}
        self.turns[thread_id] = state
        stderr_task = None
        try:
            prompt = self._prompt(context, text)
            state["process"] = process = await asyncio.create_subprocess_exec(
                *self._command(context), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                # Large tool results are valid JSON lines. No local project path
                # is needed when the actual workspace is on the remote runner.
                cwd=Path(__file__).resolve().parent, limit=8 * 1024 * 1024,
                start_new_session=True,
            )
            stderr_task = asyncio.create_task(self._drain_stderr(process))
            context["submitted"] = True
            process.stdin.write(prompt.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()
            async for line in process.stdout:
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeDecodeError) as error:
                    raise RuntimeError("Amp returned an invalid event stream; inspect the native thread before continuing") from error
                if not isinstance(message, dict):
                    raise RuntimeError("Amp returned an invalid event object")
                self._consume(state, message)
            code = await process.wait()
            if state["interrupted"]:
                raise RuntimeError("Amp task interrupted at your request")
            result = state["result"]
            if result is None:
                raise RuntimeError(self._disconnected(context, code))
            if result.get("is_error") or result.get("subtype") != "success":
                # Do not turn arbitrary CLI output (which may contain credentials)
                # into speech or logs. The native thread retains detailed errors.
                raise RuntimeError("Amp could not finish this turn. Inspect its native thread for details: "
                                   + thread_url(context["native"]))
            if code:
                raise RuntimeError(self._disconnected(context, code))
            reply = result.get("result")
            if not context["native"]:
                raise RuntimeError("Amp completed without identifying its thread. Check Amp before continuing.")
            if not isinstance(reply, str):
                raise RuntimeError("Amp completed without a readable final response")
            return reply.strip()
        except FileNotFoundError as error:
            raise RuntimeError("The Amp CLI is unavailable; configure AMP_BIN on the phone bridge") from error
        finally:
            state["done"].set()
            state["ready"].set()
            await self._detach(state["process"])
            if stderr_task:
                stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stderr_task
            for key in (thread_id, context["native"]):
                if self.turns.get(key) is state:
                    self.turns.pop(key, None)

    def _consume(self, state, message):
        native = message.get("session_id")
        # Subagent messages belong in the native audit stream, but are not the
        # caller's reply and must not rebind the parent phone conversation.
        child = bool(message.get("parent_tool_use_id"))
        if native and not child:
            self._bind(state, native)
        self._emit("amp/event", state, event=message)
        if child:
            return
        if message.get("type") == "result":
            state["result"] = message
            return
        if message.get("type") != "assistant":
            return
        details = message.get("message", {})
        blocks = details.get("content", [])
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                self._emit("item/completed", state, item={
                    "type": "agentMessage", "text": block["text"],
                    "phase": "final_answer" if details.get("stop_reason") == "end_turn" else "commentary",
                })
            elif block.get("type") == "tool_use":
                tool = str(block.get("name", ""))
                kind = ("commandExecution" if tool.lower() in {"bash", "shell", "terminal"}
                        else "fileChange" if tool in {"edit_file", "create_file", "apply_patch"}
                        else "webSearch" if tool in {"web_search", "read_web_page"}
                        else "toolCall")
                self._emit("item/started", state, item={"type": kind, "id": block.get("id"), "tool": tool})

    @staticmethod
    def _disconnected(context, code):
        link = thread_url(context["native"])
        return (f"The Amp connection ended without a confirmed result (exit {code}). "
                "The runner task may still be running. Check the existing Amp thread before continuing"
                + (": " + link if link else "; no native thread ID was received. Check Amp before retrying."))

    @staticmethod
    async def _drain_stderr(process):
        # Drain without retaining unbounded stderr or publishing login URLs/tokens.
        while await process.stderr.read(65536):
            pass

    async def steer(self, thread_id, text):
        thread_id = self.aliases.get(thread_id, thread_id)
        state = self.turns.get(thread_id)
        if not state or state["done"].is_set() or not self.control:
            return False
        if not THREAD_ID.fullmatch(thread_id):
            return False
        result = await self.control("steer", thread_id, text)
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError("Amp did not confirm delivery of the steering message; it was not queued again")
        return True

    async def interrupt(self, thread_id):
        thread_id = self.aliases.get(thread_id, thread_id)
        state = self.turns.get(thread_id)
        if not self.control:
            raise RuntimeError("The Amp runner has no phone cancellation control configured. "
                               "Stopping the task is unconfirmed; open its native Amp thread to stop it"
                               + (": " + thread_url(thread_id) if thread_url(thread_id) else "."))
        if not THREAD_ID.fullmatch(thread_id):
            if state:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(state["ready"].wait(), 5)
                thread_id = state["context"]["native"] or ""
            if not THREAD_ID.fullmatch(thread_id):
                raise RuntimeError("Amp has not returned its thread ID; cancellation is unconfirmed. Check Amp.")
        result = await self.control("cancel", thread_id, None)
        if (not isinstance(result, dict) or result.get("ok") is not True
                or result.get("state") not in {"idle", "error"}):
            raise RuntimeError("Amp has not confirmed that the task stopped. Check its native thread: " + thread_url(thread_id))
        if state:
            state["interrupted"] = True
            await self._detach(state["process"])
        return True

    @staticmethod
    async def _detach(process):
        """Release a CLI observer, not the independent remote runner task."""
        if process and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()

    async def close(self):
        self.closed = True
        states = list({id(state): state for state in self.turns.values()}.values())
        await asyncio.gather(*(self._detach(state["process"]) for state in states))
