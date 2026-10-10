"""Fan-out of new punches to connected dashboard WebSockets (thread-safe)."""
import asyncio
import json

_clients: set = set()
_loop: asyncio.AbstractEventLoop | None = None


def bind_loop(loop):
    global _loop
    _loop = loop


async def register(ws):
    _clients.add(ws)


def unregister(ws):
    _clients.discard(ws)


async def _send_all(msg: str):
    for ws in list(_clients):
        try:
            await ws.send_text(msg)
        except Exception:
            _clients.discard(ws)


def publish(punch: dict):
    """Call from any thread."""
    if _loop is None:
        return
    msg = json.dumps(punch)
    asyncio.run_coroutine_threadsafe(_send_all(msg), _loop)
