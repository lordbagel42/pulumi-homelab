import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from session_store import Store, call_uuid, session_number
from session_manager import Sessions
from control import Control
from phone_bridge import Call, host_choice


class FakeCodex:
    def __init__(self):
        self.calls = {}
        self.interruptions = []
        self.steered = []
        self.serial = 0
        self.started = asyncio.Event()

    async def new_thread(self, cwd=None, number=None):
        self.serial += 1
        return f"thread-{self.serial}"

    async def resume(self, *args):
        pass

    async def ask(self, thread, text):
        self.calls[thread] = asyncio.get_running_loop().create_future()
        self.started.set()
        return await self.calls[thread]

    async def steer(self, thread, text):
        if thread in self.calls and not self.calls[thread].done():
            self.steered.append((thread, text))
            return True
        return False

    async def interrupt(self, thread):
        self.interruptions.append(thread)
        if thread in self.calls and not self.calls[thread].done():
            self.calls[thread].set_exception(RuntimeError("interrupted"))
            return True
        return False


class FakePBX:
    def __init__(self):
        self.busy, self.calls = False, []

    async def phone_busy(self):
        return self.busy

    async def ring(self, number):
        self.calls.append(number)
        return f"test-{number}.call"


class FakeAmp(FakeCodex):
    executor = "runner:homelab-amp"
    configured = True

    def workspace(self, cwd=None):
        return cwd or "/home/amp/workspaces"


class SessionsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "sessions.db"
        self.store = Store(self.path)
        self.codex, self.pbx = FakeCodex(), FakePBX()
        self.sessions = Sessions(self.store, self.codex, self.pbx)

    async def asyncTearDown(self):
        await self.sessions.close()
        self.store.db.close()
        self.tmp.cleanup()

    async def test_hangup_does_not_cancel_work_and_reconnect_gets_result(self):
        number = self.sessions.create()["id"]
        self.sessions.attach(number)
        await self.sessions.submit(number, "Build the requested thing")
        await self.codex.started.wait()
        self.sessions.detach(number)
        thread = self.store.get_session(number)["thread_id"]
        self.assertFalse(self.codex.calls[thread].cancelled())
        self.assertEqual([], self.codex.interruptions)
        self.codex.calls[thread].set_result("The build is complete.")
        await self.sessions.workers[number]
        self.sessions.attach(number)
        self.assertEqual("The build is complete.", self.store.get_session(number)["last_reply"])
        self.assertEqual(thread, self.store.get_session(number)["thread_id"])

    async def test_running_update_steers_same_task(self):
        number = self.sessions.create()["id"]
        await self.sessions.submit(number, "Build a page")
        await self.codex.started.wait()
        await self.sessions.submit(number, "Use a dark background")
        self.assertEqual(1, len(self.codex.calls))
        self.assertEqual("Use a dark background", self.codex.steered[0][1])

    async def test_amp_default_preserves_legacy_engine_and_binds_native_thread(self):
        old = self.sessions.create("Saved Codex conversation")
        amp = FakeAmp()
        self.sessions = Sessions(self.store, self.codex, self.pbx, amp=amp)
        new = self.sessions.create("Phone Amp conversation")
        self.assertEqual(("amp", "/home/amp/workspaces", amp.executor),
                         (new["engine"], new["cwd"], new["executor"]))
        self.assertIs(self.sessions.backend(old), self.codex)
        await self.sessions.submit(new["id"], "Inspect the runner project")
        await amp.started.wait()
        before = self.store.get_session(new["id"])["thread_id"]
        native = "T-11111111-2222-3333-4444-555555555555"
        self.sessions.on_event("thread/started", {"engine": "amp", "executor": amp.executor,
            "number": new["id"], "previousThreadId": before, "threadId": native})
        amp.calls[before].set_result("Inspected the project")
        await self.sessions.workers[new["id"]]
        saved = self.store.get_session(new["id"])
        self.assertEqual(native, saved["thread_id"])
        self.assertEqual("https://ampcode.com/threads/" + native, saved["thread_url"])
        self.assertEqual(old["extension"], self.store.get_session(old["id"])["extension"])
        self.assertEqual({}, self.codex.calls)

    async def test_amp_runner_change_cannot_silently_move_saved_work(self):
        amp = FakeAmp()
        self.sessions = Sessions(self.store, self.codex, self.pbx, amp=amp)
        saved = self.sessions.create()
        amp.executor = "runner:another-runner"
        with self.assertRaisesRegex(ValueError, "different Amp runner"):
            await self.sessions.submit(saved["id"], "Continue")
        self.assertIsNone(self.store.next_job(saved["id"]))

    async def test_unconfirmed_amp_cancellation_does_not_claim_stopped(self):
        amp = FakeAmp()
        self.sessions = Sessions(self.store, self.codex, self.pbx, amp=amp)
        saved = self.sessions.create()
        await self.sessions.submit(saved["id"], "Long task")
        await amp.started.wait()
        async def unconfirmed(thread):
            raise RuntimeError("Cancellation is unconfirmed")
        amp.interrupt = unconfirmed
        with self.assertRaisesRegex(RuntimeError, "unconfirmed"):
            await self.sessions.interrupt(saved["id"])
        self.assertEqual("running", self.store.get_session(saved["id"])["state"])
        self.assertFalse(self.sessions.workers[saved["id"]].done())

    async def test_explicit_stop_interrupts_but_preserves_session(self):
        number = self.sessions.create()["id"]
        await self.sessions.submit(number, "Long-running task")
        await self.codex.started.wait()
        thread = self.store.get_session(number)["thread_id"]
        await self.sessions.interrupt(number)
        await self.sessions.workers[number]
        self.assertEqual([thread], self.codex.interruptions)
        self.assertEqual("interrupted", self.store.get_session(number)["state"])
        self.assertEqual(thread, self.store.get_session(number)["thread_id"])

    async def test_native_question_survives_hangup_and_returns_correct_schema(self):
        session = self.store.create_session("test", str(self.tmp.name), thread_id="native-thread")
        task = asyncio.create_task(self.sessions.handle_request("item/tool/requestUserInput", {
            "threadId": "native-thread", "itemId": "item-1", "turnId": "turn-1", "isBlocking": True,
            "questions": [{"id": "color", "header": "Color", "question": "Which color?",
                           "options": [{"label": "Blue", "description": "Blue"}]}],
        }))
        await asyncio.sleep(0)
        self.sessions.attach(session["id"])
        self.sessions.detach(session["id"])
        self.assertFalse(task.done())
        question = self.store.pending_questions(session["id"])[0]
        # Simulated caller answer in this isolated test database.
        self.store.record_answer(question["id"], "color", "Blue")
        self.sessions.changed(session["id"])
        self.assertEqual({"answers": {"color": {"answers": ["Blue"]}}}, await task)

    async def test_question_wait_timeout_is_not_answer_or_cancel(self):
        number = self.sessions.create()["id"]
        question = self.sessions.ask_question(number, [{"id": "q", "question": "Which design?"}])
        result = await self.sessions.wait_answer(question["id"], timeout=0.01)
        self.assertEqual("pending", result["state"])
        self.assertEqual({}, result["answers"])
        self.assertEqual("pending", self.store.get_question(question["id"])["state"])

    async def test_callbacks_queue_while_busy_and_ring_only_once(self):
        number = self.sessions.create()["id"]
        other = self.sessions.create()["id"]
        question = self.sessions.ask_question(number, [{"id": "q", "question": "Which design?"}])
        self.sessions.attach(other)
        await self.sessions.ring_once()
        self.assertEqual([], self.pbx.calls)
        self.sessions.detach(other)
        self.pbx.busy = True
        await self.sessions.ring_once()
        self.assertEqual([], self.pbx.calls)
        self.pbx.busy = False
        await self.sessions.ring_once()
        await self.sessions.ring_once()
        self.assertEqual([number], self.pbx.calls)
        self.assertEqual("pending", self.store.get_question(question["id"])["state"])

    async def test_same_call_gets_question_without_ring(self):
        number = self.sessions.create()["id"]
        self.sessions.attach(number)
        self.sessions.ask_question(number, [{"id": "q", "question": "Which design?"}])
        await self.sessions.ring_once()
        self.assertEqual([], self.pbx.calls)

    async def test_external_status_and_callback_inbox(self):
        control = Control(self.sessions)
        session = await control.dispatch("register", {"session_key": "desktop-123", "title": "Build page"})
        same = await control.dispatch("register", {"session_key": "desktop-123"})
        self.assertEqual(session["id"], same["id"])
        await control.dispatch("update", {"session_id": session["id"], "summary": "Tests passed", "state": "done"})
        self.assertEqual("Tests passed", self.store.get_session(session["id"])["last_reply"])
        await self.sessions.submit(session["id"], "Please also add a search box")
        messages = await control.dispatch("inbox", {"session_id": session["id"]})
        self.assertEqual("Please also add a search box", messages[0]["text"])
        self.assertEqual([], self.store.inbox(session["id"]))

    async def test_spoken_answer_returns_to_codex_without_a_confirmation_menu(self):
        number = self.sessions.create()["id"]
        question = self.sessions.ask_question(number, [{"id": "q", "question": "Which color?",
                                                       "options": [{"label": "Blue"}, {"label": "Green"}]}])
        class FakeBridge:
            sessions = self.sessions
        call = Call(FakeBridge(), None, None)
        call.number = number
        call.status = lambda *args, **kwargs: None
        call.closed = True  # A disconnect without words is not an answer.
        self.assertEqual("pending", self.store.get_question(question["id"])["state"])
        call.closed = False
        await call.handle_text("option two")
        self.assertEqual({"q": {"answers": ["Green"]}}, self.store.get_question(question["id"])["answers"])
        self.assertTrue(call.output.empty())  # Let Codex respond, not a canned echo.

    async def test_follow_up_correction_steers_the_same_conversation(self):
        number = self.sessions.create()["id"]
        await self.sessions.submit(number, "Check the microphone")
        await self.codex.started.wait()
        thread = self.store.get_session(number)["thread_id"]
        question = self.sessions.ask_question(number, [{"id": "q", "question": "Did it work?"}])
        class FakeBridge:
            sessions = self.sessions
        call = Call(FakeBridge(), None, None)
        call.number = number
        call.status = lambda *args, **kwargs: None
        await call.handle_text("Test, test, I am interrupting you")
        await call.handle_text("It is too sensitive to breathing. Please fix that.")
        answers = self.store.get_question(question["id"])["answers"]
        self.assertEqual(["Test, test, I am interrupting you"], answers["q"]["answers"])
        self.assertEqual([(thread, "It is too sensitive to breathing. Please fix that.")], self.codex.steered)
        self.assertTrue(call.output.empty())

    async def test_natural_confirmation_reaches_codex_verbatim(self):
        number = self.sessions.create()["id"]
        question = self.sessions.ask_question(number, [{"id": "q", "question": "You want a blue background. Is that right?"}])
        class FakeBridge:
            sessions = self.sessions
        call = Call(FakeBridge(), None, None)
        call.number = number
        call.status = lambda *args, **kwargs: None
        await call.handle_text("That's right. Yes.")
        self.assertEqual(["That's right. Yes."], self.store.get_question(question["id"])["answers"]["q"]["answers"])
        self.assertTrue(call.output.empty())

    async def test_stop_discards_speech_that_was_still_being_transcribed(self):
        number = self.sessions.create()["id"]
        started, release = asyncio.Event(), asyncio.Event()
        class FakeBridge:
            sessions = self.sessions
            async def transcribe(self, pcm):
                started.set()
                await release.wait()
                return "Start a new task"
        call = Call(FakeBridge(), None, None)
        call.number = number
        call.status = lambda *args, **kwargs: None
        transcribing = asyncio.create_task(call.transcribe(b"test", 0))
        await started.wait()
        await self.sessions.interrupt(number)
        release.set()
        await transcribing
        self.assertIsNone(self.store.next_job(number))
        self.assertEqual({}, self.codex.calls)
        self.assertEqual("interrupted", self.store.get_session(number)["state"])

    def add_workstation(self):
        workstation = FakeCodex()
        workstation.serial = 100
        self.sessions.backends["workstation"] = workstation
        return workstation

    async def test_hosts_run_independently_and_interrupt_routes_to_original_host(self):
        workstation = self.add_workstation()
        cluster = self.sessions.create(host="proxmox")
        desktop = self.sessions.create(host="workstation", cwd="/home/raygen/Projects")
        await self.sessions.submit(cluster["id"], "Cluster task")
        await self.sessions.submit(desktop["id"], "Local task")
        await asyncio.wait_for(asyncio.gather(self.codex.started.wait(), workstation.started.wait()), 2)
        self.sessions.detach(desktop["id"])
        local_thread = self.store.get_session(desktop["id"])["thread_id"]
        await self.sessions.submit(desktop["id"], "Use my local repository")
        self.assertEqual([(local_thread, "Use my local repository")], workstation.steered)
        await self.sessions.interrupt(desktop["id"])
        await self.sessions.workers[desktop["id"]]
        self.assertEqual([local_thread], workstation.interruptions)
        self.assertEqual([], self.codex.interruptions)
        cluster_thread = self.store.get_session(cluster["id"])["thread_id"]
        self.codex.calls[cluster_thread].set_result("Cluster completed independently")
        await self.sessions.workers[cluster["id"]]
        self.assertEqual("done", self.store.get_session(cluster["id"])["state"])
        with self.assertRaisesRegex(ValueError, "original host"):
            self.sessions.select_host(desktop["id"], "proxmox")

    async def test_task_before_host_choice_is_saved_across_hangup(self):
        workstation = self.add_workstation()
        number = self.sessions.create(host="choose")["id"]
        await self.sessions.submit(number, "Inspect my local repository")
        self.sessions.detach(number)
        self.assertEqual({}, self.codex.calls)
        self.assertEqual({}, workstation.calls)
        self.assertEqual("Inspect my local repository", self.store.next_job(number)["text"])
        class FakeBridge:
            sessions = self.sessions
        call = Call(FakeBridge(), None, None)
        call.number = number
        call.status = lambda *args, **kwargs: None
        await call.key("2")
        await asyncio.wait_for(workstation.started.wait(), 2)
        session = self.store.get_session(number)
        self.assertEqual("workstation", session["host"])
        workstation.calls[session["thread_id"]].set_result("Inspected locally")
        await self.sessions.workers[number]
        self.assertEqual("Inspected locally", self.store.get_session(number)["last_reply"])

    async def test_spoken_host_and_task_are_accepted_together(self):
        workstation = self.add_workstation()
        number = self.sessions.create(host="choose")["id"]
        class FakeBridge:
            sessions = self.sessions
        call = Call(FakeBridge(), None, None)
        call.number = number
        call.status = lambda *args, **kwargs: None
        await call.handle_text("On my workstation, check the project status")
        await asyncio.wait_for(workstation.started.wait(), 2)
        self.assertEqual("workstation", self.store.get_session(number)["host"])
        self.assertEqual("check the project status", self.store.db.execute("SELECT text FROM jobs").fetchone()[0])
        self.assertEqual(("proxmox", ""), host_choice("Proxmox, please."))

    async def test_offline_host_never_falls_back_and_explicit_retry_retains_task(self):
        workstation = self.add_workstation()
        original = workstation.new_thread
        async def unavailable(*args):
            raise RuntimeError("Workstation unavailable")
        workstation.new_thread = unavailable
        number = self.sessions.create(host="workstation")["id"]
        with self.assertLogs("codex-phone.sessions", level="ERROR"):
            await self.sessions.submit(number, "Inspect the local repository")
            await self.sessions.workers[number]
        self.assertEqual("error", self.store.get_session(number)["state"])
        self.assertEqual({}, self.codex.calls)
        workstation.new_thread = original
        await self.sessions.submit(number, "Continue")
        await asyncio.wait_for(workstation.started.wait(), 2)
        self.assertEqual("Inspect the local repository", self.store.last_job(number)["text"])
        session = self.store.get_session(number)
        self.assertEqual("workstation", session["host"])
        workstation.calls[session["thread_id"]].set_result("Completed after reconnecting")
        await self.sessions.workers[number]


class PersistenceTests(unittest.TestCase):
    def test_engine_upgrade_preserves_old_threads_and_remote_disconnect_is_truthful(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.db"
            store = Store(path)
            legacy = store.create_session("Legacy", directory, thread_id="old-thread")
            store.db.execute("ALTER TABLE sessions DROP COLUMN engine")
            store.db.execute("ALTER TABLE sessions DROP COLUMN executor")
            store.db.commit()
            store.db.close()
            store = Store(path)
            self.assertEqual("codex", store.get_session(legacy["id"])["engine"])
            current = store.create_session("Amp", "/runner/work", engine="amp", executor="runner:homelab-amp",
                thread_id="T-11111111-2222-3333-4444-555555555555")
            store.update_session(current["id"], state="running")
            job = store.add_job(current["id"], "A task that may still be running remotely")
            store.update_job(job, "running")
            store.recover()
            recovered = store.get_session(current["id"])
            self.assertEqual("error", recovered["state"])
            self.assertIn("may still be working", recovered["error"])
            self.assertEqual(current["thread_id"], recovered["thread_id"])
            self.assertIsNone(store.next_job(current["id"]))
            store.db.close()

    def test_upgrade_keeps_old_numbers_and_persists_workstation_routing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.db"
            store = Store(path)
            old = store.create_session("Original session", directory, thread_id="original-thread")
            store.db.execute("ALTER TABLE sessions DROP COLUMN host")
            store.db.commit()
            store.db.close()
            store = Store(path)
            self.assertEqual("proxmox", store.get_session(old["id"])["host"])
            self.assertEqual("original-thread", store.get_session(old["id"])["thread_id"])
            new = store.create_session("Workstation session", "/home/raygen/Projects", host="workstation")
            store.db.close()
            store = Store(path)
            self.assertEqual("workstation", store.get_session(new["extension"])["host"])
            self.assertEqual("611002", new["extension"])
            store.db.close()

    def test_stable_numbers_and_restart_preserves_history_without_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.db"
            store = Store(path)
            session = store.create_session("Build page", directory, thread_id="real-thread-id")
            store.update_session(session["id"], state="running", last_reply="Previous result")
            job = store.add_job(session["id"], "Potentially consequential command")
            store.update_job(job, "running")
            store.db.close()
            store = Store(path)
            store.recover()
            restored = store.get_session("611-001")
            self.assertEqual("real-thread-id", restored["thread_id"])
            self.assertEqual("Previous result", restored["last_reply"])
            self.assertEqual("interrupted", restored["state"])
            self.assertIsNone(store.next_job(1))
            self.assertEqual("611002", store.create_session("Next task", directory)["extension"])
            store.db.close()

    def test_uuid_routing_and_invalid_numbers(self):
        for number in (0, 1, 12, 999):
            self.assertEqual(f"{number:03d}", call_uuid(number).hex[:3])
        for value in ("611-001", "611001", "001", 1):
            self.assertEqual(1, session_number(value))
        for value in ("000", "611000", "1000", "garbage", "../001"):
            with self.assertRaises(ValueError):
                session_number(value)


if __name__ == "__main__":
    unittest.main()
