"""Narrow PBX adapter: the only outbound destination is the owner's handset."""

import asyncio
import re
import uuid


def display_name(value):
    value = value if isinstance(value, str) else ""
    value = " ".join("".join(c if c.isprintable() and c not in '\\"<>' else " " for c in value).split())
    return value.encode("utf-8")[:39].decode("utf-8", errors="ignore").strip() or "Slack huddle"


class PBX:
    async def command(self, *args, input=None):
        process = await asyncio.create_subprocess_exec(
            "docker", "exec", "-i", "sccp-pbx", *args,
            stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(input), 10)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise RuntimeError(stderr.decode(errors="replace")[:1000] or "PBX command failed")
        return stdout.decode(errors="replace")

    async def phone_busy(self):
        channels = await self.command("asterisk", "-rx", "core show channels concise")
        return any(line.startswith("SCCP/6738-") for line in channels.splitlines())

    async def connected_line(self, call_id, name, number):
        """Update only the handset carrying this exact AudioSocket call."""
        call_id = str(uuid.UUID(call_id))
        if not isinstance(number, str) or not re.fullmatch(r"(?:\d{1,16}|[UW][A-Z0-9]{8,20})", number):
            raise ValueError("Invalid connected-party number")
        channels = await self.command("asterisk", "-rx", "core show channels concise")
        matches = []
        for line in channels.splitlines():
            fields = line.split("!")
            if (len(fields) > 6 and re.fullmatch(r"SCCP/6738-[A-Fa-f0-9]+", fields[0])
                    and fields[5] == "AudioSocket" and fields[6].split(",", 1)[0] == call_id):
                matches.append(fields[0])
        if len(matches) != 1:
            return False  # Hung up or replaced by another call; never guess.
        for variable, value in (("CONNECTEDLINE(name-pres,i)", "allowed"),
                                ("CONNECTEDLINE(num-pres,i)", "allowed"),
                                ("CONNECTEDLINE(name,i)", display_name(name)),
                                ("CONNECTEDLINE(num)", number)):
            # This is Asterisk CLI quoting, passed as one argv item without a
            # shell. The final update emits both name and number to the phone.
            quoted = '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
            await self.command("asterisk", "-rx", f"dialplan set chanvar {matches[0]} {variable} {quoted}")
        return True

    async def ring(self, number):
        if not isinstance(number, int) or not 0 <= number <= 999:
            raise ValueError("Invalid session")
        name = f"codex-{uuid.uuid4()}.call"
        body = (f"Channel: SCCP/6738\nCallerID: Codex {number:03d} <611{number:03d}>\n"
                "MaxRetries: 0\nRetryTime: 60\nWaitTime: 45\nContext: codex-voice\n"
                f"Extension: s\nPriority: 1\nSetvar: CODEX_SESSION={number:03d}\nArchive: yes\n")
        # A fully written file is moved into the queue atomically. No caller-provided
        # destination, shell fragment, or arbitrary caller-ID text is accepted.
        command = ("umask 077; cat > /var/spool/asterisk/" + name
                   + " && chown asterisk:asterisk /var/spool/asterisk/" + name
                   + " && mv /var/spool/asterisk/" + name + " /var/spool/asterisk/outgoing/" + name)
        await self.command("sh", "-c", command, input=body.encode())
        return name
