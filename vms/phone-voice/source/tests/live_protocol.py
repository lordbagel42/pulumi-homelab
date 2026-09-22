"""Manual real-Codex protocol test. Uses an isolated DB and never rings the phone."""

import asyncio
import json
import tempfile
import tomllib
from pathlib import Path

from codex_client import CodexClient
from session_manager import Sessions
from session_store import Store


class NoPhone:
    async def phone_busy(self):
        return True

    async def ring(self, number):
        raise AssertionError("This test must never call the handset")


async def main():
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "test.db")
        codex = CodexClient()
        sessions = Sessions(store, codex, NoPhone())
        await codex.start()
        try:
            for mode in ("native", "dynamic"):
                session = sessions.create("Automated protocol test")
                number = session["id"]
                thread = await codex.new_thread(number=number)
                store.update_session(number, thread_id=thread)
                future = asyncio.get_running_loop().create_future()
                state = {"future": future, "messages": [], "turn_id": None, "started": asyncio.Event()}
                codex.turns[thread] = state
                text = ("This is an automated tool-transport test, not a real user preference. "
                        "Ask the test harness to choose Blue or Green using "
                        + ("request_user_input" if mode == "native" else "phone_ask_user")
                        + ". Do not ask in plain assistant text. After the test harness answers, "
                        "reply only with the selected color. Do not use other tools.")
                params = {"threadId": thread, "input": [{"type": "text", "text": text}], "effort": "low"}
                if mode == "native":
                    config = tomllib.loads((Path.home() / ".codex/config.toml").read_text())
                    params["collaborationMode"] = {"mode": "plan", "settings": {
                        "model": config["model"], "reasoning_effort": "low", "developer_instructions": None}}
                result = await codex.request("turn/start", params)
                state["turn_id"] = result["turn"]["id"]
                state["started"].set()
                async with asyncio.timeout(90):
                    while not store.pending_questions(number):
                        if future.done():
                            raise AssertionError(f"{mode}: model did not issue the expected question: {future.result()}")
                        await asyncio.sleep(0.1)
                    question = store.pending_questions(number)[0]
                    assert question["source"] == mode, question
                    item = question["questions"][0]
                    # Explicitly synthetic harness response in an isolated database.
                    store.record_answer(question["id"], item["id"], "Green")
                    sessions.changed(number)
                    reply = await future
                    assert "green" in reply.lower(), reply
                    print(f"PASS {mode}: real Codex question -> test answer -> {reply}", flush=True)
                codex.turns.pop(thread, None)
        finally:
            await sessions.close()
            await codex.close()
            store.db.close()


if __name__ == "__main__":
    asyncio.run(main())
