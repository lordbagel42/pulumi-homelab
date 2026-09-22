"""Durable agent work is independent of individual telephone calls."""

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path

from codex_client import ROOT
from session_store import session_number
from callback_policy import callback_permitted

LOG = logging.getLogger("codex-phone.sessions")


class Sessions:
    def __init__(self, store, codex, pbx, workstation=None, *, amp=None, amp_workstation=None, default_engine=None):
        self.store, self.codex, self.pbx = store, codex, pbx
        legacy = {"proxmox": codex} if codex else {}
        if workstation:
            legacy["workstation"] = workstation
        amp_backends = {"proxmox": amp} if amp else {}
        if amp_workstation:
            amp_backends["workstation"] = amp_workstation
        self.engine_backends = {"codex": legacy, "amp": amp_backends}
        self.default_engine = default_engine or ("amp" if amp else "codex")
        if self.default_engine not in self.engine_backends:
            raise ValueError("Choose Amp or Codex as the default engine")
        self.backends = self.engine_backends[self.default_engine]
        self.workers = {}
        self.events = {}
        self.spoken_progress = {}
        self.input_generation = {}
        self.attached = {}
        self.ringer = None
        self.closed = False
        for backend in [b for hosts in self.engine_backends.values() for b in hosts.values()]:
            backend.on_request = self.handle_request
            backend.on_event = self.on_event

    def backend(self, session):
        engine = session.get("engine", "codex")
        backend = self.engine_backends.get(engine, {}).get(session["host"])
        if not backend:
            raise ValueError(f"The saved {engine} task host is not configured")
        if engine == "amp" and session.get("executor") != backend.executor:
            raise ValueError("This session belongs to a different Amp runner; restore that runner configuration or create a new session")
        return backend

    def capabilities(self):
        return {"default_engine": self.default_engine, "backends": [
            {"engine": engine, "host": host, "executor": getattr(backend, "executor", ""),
             "configured": getattr(backend, "configured", True)}
            for engine, hosts in self.engine_backends.items() for host, backend in hosts.items()]}

    def event(self, number):
        return self.events.setdefault(number, asyncio.Event())

    def changed(self, number):
        self.events.setdefault(number, asyncio.Event()).set()
        self.events[number] = asyncio.Event()

    async def start(self):
        self.store.recover()
        for session in self.store.list_sessions():
            if (session["kind"] == "managed" and session["host"] != "choose"
                    and not (session["engine"] == "amp" and session["state"] == "error")
                    and self.store.next_job(session["id"])):
                self.ensure_worker(session["id"])
        self.ringer = asyncio.create_task(self.ring_loop())

    def workspace(self, host, cwd=None, engine=None):
        backends = self.engine_backends.get(engine or self.default_engine, {})
        if host == "choose":
            return ""
        if host not in backends:
            raise ValueError("That task host is not configured")
        if hasattr(backends[host], "workspace"):
            return backends[host].workspace(cwd)
        if host == "workstation":
            workspace = str(cwd or os.environ.get("CODEX_PHONE_WORKSTATION_WORKSPACE", "/home/raygen/Projects/cisco-phone-shenanigans"))
            if not Path(workspace).is_absolute():
                raise ValueError("Use an absolute workstation project path")
            return workspace
        workspace = Path(cwd or ROOT).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError("The session workspace must be an existing directory")
        return str(workspace)

    def create(self, title="Phone task", cwd=None, host="proxmox", engine=None):
        engine = engine or self.default_engine
        if engine not in self.engine_backends or not self.engine_backends[engine]:
            raise ValueError("That agent engine is not configured")
        workspace = self.workspace(host, cwd, engine)
        backend = self.engine_backends[engine].get(host)
        return self.store.create_session(title, workspace, host=host, engine=engine,
                                         executor=getattr(backend, "executor", ""))

    def select_host(self, number, host):
        saved = self.store.get_session(number)
        workspace = self.workspace(host, engine=saved["engine"])
        backend = self.engine_backends[saved["engine"]][host]
        session = self.store.select_host(number, host, workspace, getattr(backend, "executor", ""))
        if self.store.next_job(session["id"]):
            self.ensure_worker(session["id"])
        self.changed(session["id"])
        return session

    def attach(self, number):
        self.attached[number] = self.attached.get(number, 0) + 1

    def detach(self, number):
        self.attached[number] = max(0, self.attached.get(number, 1) - 1)
        # Deliberately no task cancellation or turn/interrupt here.

    def ensure_worker(self, number):
        if number not in self.workers or self.workers[number].done():
            self.workers[number] = asyncio.create_task(self.run_jobs(number))

    async def submit(self, number, text):
        number = session_number(number)
        session = self.store.get_session(number)
        text = text.strip()
        if not text or len(text) > 20000:
            raise ValueError("Provide a nonempty message of at most 20,000 characters")
        if session["kind"] == "external":
            self.store.add_inbox(number, text)
            self.changed(number)
            return "Saved your message for the original session to collect."
        if (not session["thread_id"] and session["state"] == "error"
                and text.lower().strip(" .!?") in {"continue", "resume", "try again", "retry", "continue the task"}):
            previous = self.store.last_job(number)
            if previous and previous["state"] == "error":
                # Connection failure happened before a Codex conversation could
                # receive the task. Retry only after this explicit user request.
                text = previous["text"]
        if session["title"] == "Phone task":
            self.store.update_session(number, title=text[:100])
        if session["host"] == "choose":
            self.store.add_job(number, text)
            return "I saved your task. Choose where to run it: one for Proxmox, or two for this workstation."
        codex = self.backend(session)
        if session["thread_id"] and await codex.steer(session["thread_id"], text):
            self.changed(number)
            return "I passed that update to the running task."
        self.store.add_job(number, text)
        self.ensure_worker(number)
        self.changed(number)
        return "Your task is running. You can hang up and call this session again."

    async def run_jobs(self, number):
        while not self.closed and (job := self.store.next_job(number)):
            self.store.update_job(job["id"], "running")
            self.store.update_session(number, state="running", error="", progress="Starting your task.")
            self.changed(number)
            try:
                session = self.store.get_session(number)
                codex = self.backend(session)
                if not session["thread_id"]:
                    thread_id = await codex.new_thread(session["cwd"], number)
                    self.store.update_session(number, thread_id=thread_id)
                else:
                    thread_id = session["thread_id"]
                    await codex.resume(thread_id, session["cwd"], number)
                reply = await codex.ask(thread_id, job["text"])
                self.store.update_job(job["id"], "done", reply)
                self.store.update_session(number, state="done", last_reply=reply, progress="Task finished.")
                LOG.info("Session %03d finished: %s", number, reply)
            except asyncio.CancelledError:
                self.store.update_job(job["id"], "interrupted")
                current = self.store.get_session(number)
                if current["engine"] == "amp":
                    self.store.update_session(number, state="error", progress="The phone connection to Amp was closed.",
                        error="The Amp runner may still be working. Inspect the native thread before continuing.")
                else:
                    self.store.update_session(number, state="interrupted", progress="Task stopped.")
                raise
            except Exception as error:
                stopped = "interrupted" in str(error).lower()
                self.store.update_job(job["id"], "interrupted" if stopped else "error", str(error))
                self.store.update_session(number, state="interrupted" if stopped else "error",
                                          error="" if stopped else str(error)[:1000],
                                          progress="Task stopped." if stopped else "The task encountered an error.")
                if not stopped:
                    LOG.exception("Session %03d failed", number)
                # In particular, a lost Amp stream does not imply the remote
                # runner stopped. Do not send another queued task automatically.
                break
            finally:
                self.changed(number)

    async def interrupt(self, number):
        number = session_number(number)
        session = self.store.get_session(number)
        # An older utterance still being transcribed must not restart work
        # after an explicit stop, including one sent through the control API.
        self.input_generation[number] = self.input_generation.get(number, 0) + 1
        if session["kind"] == "external":
            self.store.add_inbox(number, "The owner explicitly asks you to stop the current task.")
            self.changed(number)
            return "I sent a stop request to the original session. I cannot stop its process from this bridge."
        self.store.cancel_pending(number)
        self.changed(number)
        interrupted = False
        if session["thread_id"]:
            interrupted = await self.backend(session).interrupt(session["thread_id"])
        worker = self.workers.get(number)
        if not interrupted and worker and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        self.store.update_session(number, state="interrupted", progress="Task stopped at your request.")
        self.changed(number)
        return "I stopped the task. Its conversation is saved."

    def on_event(self, method, params):
        if method == "thread/started" and params.get("engine") == "amp":
            session = self.store.bind_amp_thread(params["number"], params["previousThreadId"],
                                                 params["threadId"], params["executor"])
            self.changed(session["id"])
            return
        session = self.store.find_thread(params.get("threadId"))
        if not session:
            return
        progress = None
        item = params.get("item", {})
        if method == "item/completed" and item.get("type") == "agentMessage":
            if item.get("phase") != "final_answer":
                progress = item.get("text")
                if progress:
                    self.spoken_progress[session["id"]] = progress
        elif method == "item/started":
            progress = {"commandExecution": "Running a workspace command.",
                        "fileChange": "Editing project files.",
                        "webSearch": "Looking up documentation."}.get(item.get("type"))
        if progress:
            self.store.update_session(session["id"], progress=progress[:3000])
            self.changed(session["id"])

    async def handle_request(self, method, params):
        session = self.store.find_thread(params.get("threadId"))
        if not session:
            raise ValueError("No phone extension is registered for this thread")
        if method == "item/tool/requestUserInput":
            questions = params["questions"]
            source = "native"
        elif method == "item/tool/call" and params.get("tool") == "phone_ask_user":
            args = params["arguments"]
            if isinstance(args, str):
                args = json.loads(args)
            questions = [{"id": "answer", "header": "Question", "question": args["question"],
                          "options": [{"label": value, "description": ""} for value in args.get("options", [])]}]
            source = "dynamic"
        else:
            raise ValueError("This request needs the desktop interface; phone clarification is not permission escalation")
        request_key = session["thread_id"] + ":" + str(params.get("itemId") or params.get("callId"))
        question = self.ask_question(session["id"], questions, source, request_key)
        question = await self.wait_answer(question["id"], timeout=None)
        if source == "native":
            return {"answers": question["answers"]}
        return {"success": question["state"] == "answered", "contentItems": [
            {"type": "inputText", "text": json.dumps({
                "state": question["state"], "answers": question["answers"],
                "extension": question["extension"]})}
        ]}

    def ask_question(self, number, questions, source="mcp", request_key=None):
        number = session_number(number)
        self.store.get_session(number)
        question = self.store.add_question(number, questions, source, request_key)
        self.store.update_session(number, state="waiting", progress="Waiting for your answer by phone.")
        self.changed(number)
        return question

    async def wait_answer(self, question_id, timeout=50):
        async def wait():
            while True:
                question = self.store.get_question(question_id)
                event = self.event(question["session_id"])
                if question["state"] != "pending":
                    return question
                await event.wait()
        if timeout is None:
            return await wait()
        try:
            return await asyncio.wait_for(wait(), max(0, min(float(timeout), 120)))
        except asyncio.TimeoutError:
            return self.store.get_question(question_id)

    async def answer(self, question_id, item_id, text):
        question = self.store.record_answer(question_id, item_id, text)
        number = question["session_id"]
        self.changed(number)
        if question["state"] == "answered":
            worker = self.workers.get(number)
            self.store.update_session(number, state="running" if worker and not worker.done() else "idle")
            if question["source"] != "mcp" and (worker is None or worker.done()):
                # A restart lost the transport request, but the user's new answer
                # can resume the saved conversation without replaying old tool calls.
                text = "The owner answered your earlier questions: " + json.dumps(question["answers"])
                await self.submit(number, text + ". Continue the previously requested task using these answers.")
        return question

    async def ring_loop(self):
        while True:
            try:
                await self.ring_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Callback scheduler error")
            await asyncio.sleep(2)

    async def ring_once(self):
        pending = [q for q in self.store.pending_questions()
                   if not q["ring_attempted"] and not self.attached.get(q["session_id"])]
        if pending and not any(self.attached.values()) and not await self.pbx.phone_busy() and await callback_permitted():
            question = pending[0]
            # Mark before the side effect so a crash cannot repeatedly ring.
            self.store.mark_ring(question["id"], "starting")
            try:
                name = await self.pbx.ring(question["session_id"])
                self.store.mark_ring(question["id"], "ringing_once", name)
                LOG.info("Calling owner for session %03d question %s", question["session_id"], question["id"])
            except Exception:
                self.store.mark_ring(question["id"], "call_failed")
                LOG.exception("Could not ring the phone")

    async def close(self):
        self.closed = True
        if self.ringer:
            self.ringer.cancel()
        for task in self.workers.values():
            task.cancel()
        await asyncio.gather(*self.workers.values(), *([self.ringer] if self.ringer else []),
                             return_exceptions=True)
