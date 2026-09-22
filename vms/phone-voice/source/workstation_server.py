#!/usr/bin/env python3
"""Owner-only stdio transport for the workstation's phone Codex app-server."""

import asyncio
import contextlib
import logging
import os
from pathlib import Path
import signal
import socket
import struct

from codex_client import ROOT, app_server_command

LOG = logging.getLogger("codex-phone.workstation")
SOCKET = Path(os.environ.get("CODEX_PHONE_WORKER_SOCKET",
                            Path(__file__).resolve().parent / "state/workstation-app-server.sock"))


async def relay(reader, writer):
    while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()


async def serve(reader, writer):
    process, pumps = None, []
    try:
        peer = writer.get_extra_info("socket")
        _, uid, _ = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != os.getuid():
            raise PermissionError("Only the workstation owner may connect")
        process = await asyncio.create_subprocess_exec(
            *app_server_command(), cwd=ROOT, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)
        pumps = [asyncio.create_task(relay(reader, process.stdin)),
                 asyncio.create_task(relay(process.stdout, writer))]
        LOG.info("Cluster connected to workstation Codex")
        done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (ConnectionError, BrokenPipeError):
        pass
    except asyncio.CancelledError:
        raise
    except Exception:
        LOG.exception("Workstation connection failed")
    finally:
        for task in pumps:
            task.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)
        if process:
            # A transport failure must not leave unobservable shell commands
            # running. Phone hangup never closes this cluster connection.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 8)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            LOG.info("Workstation connection closed; conversation history retained")
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()


async def main():
    SOCKET.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    SOCKET.unlink(missing_ok=True)
    connections = set()

    def accept(reader, writer):
        task = asyncio.create_task(serve(reader, writer))
        connections.add(task)
        task.add_done_callback(connections.discard)

    server = await asyncio.start_unix_server(accept, str(SOCKET))
    SOCKET.chmod(0o600)
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    try:
        async with server:
            LOG.info("Workstation task host ready")
            await stop.wait()
    finally:
        for task in list(connections):
            task.cancel()
        await asyncio.gather(*connections, return_exceptions=True)
        SOCKET.unlink(missing_ok=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main())
