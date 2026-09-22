"""Real speech -> Codex question/answer/correction; never rings the handset."""

import asyncio

from audio import Speech
from control import request
from live_bridge import Connection, until


async def main():
    speech = await asyncio.to_thread(Speech)
    session = await request("create", title="Conversational read-back verification")
    number = session["id"]
    call = await Connection().open(number)
    completed = False
    try:
        await call.key("*")
        await request("submit", session_id=number, text=(
            "This is a conversation test about a hypothetical page; do not edit files or use other tools. "
            "Start by using phone_ask_user to ask exactly: What background and text colors do you want? "
            "Then respond conversationally to the caller's answer."))
        await until(lambda: "what background" in call.status().get("spoken_text", "").lower())
        pcm = await asyncio.to_thread(speech.synthesize,
                                     "A blue background with white text. Would that be easy to read?")
        await call.speak(pcm)

        async def reply_with(word):
            result = await request("session", session_id=number)
            if result["state"] == "error":
                raise AssertionError(result["error"])
            return result if result["state"] == "done" and word in result["last_reply"].lower() else None

        result = await until(lambda: reply_with("blue"), timeout=45)
        assert "white" in result["last_reply"].lower(), result["last_reply"]
        thread = result["thread_id"]
        print("PASS: a spoken answer with a follow-up question receives a real conversational Codex reply:",
              result["last_reply"], flush=True)
        await until(lambda: call.status().get("event") == "speaking")
        pcm = await asyncio.to_thread(speech.synthesize, "Actually, make the background dark green instead.")
        await call.speak(pcm)
        result = await until(lambda: reply_with("green"), timeout=45)
        assert result["thread_id"] == thread
        print("PASS: a spoken correction updates the same conversation:", result["last_reply"], flush=True)
        completed = True
    finally:
        if not completed:
            await request("interrupt", session_id=number)
        await call.close()


if __name__ == "__main__":
    asyncio.run(main())
