"""SSE adapter layer: bridge an async pipeline (a generator of event dicts) to
a synchronous generator of SSE frame strings for Flask, injecting heartbeat
comments during idle gaps so nginx's 60s proxy_read_timeout never trips."""

import asyncio
import json
import queue
import threading

import config

_SENTINEL = object()


def format_frame(event: dict) -> str:
    """One event dict -> one SSE frame: `event: <type>\\n data: <json>\\n\\n`."""
    etype = event.get("type", "message")
    return f"event: {etype}\ndata: {json.dumps(event)}\n\n"


def stream_async(async_gen_factory):
    """Drive `async_gen_factory()` (a zero-arg callable returning an async
    generator of event dicts) in a dedicated thread + event loop, forwarding
    events to this sync generator through a thread-safe queue. Yields SSE
    strings; emits `: hb` heartbeats when the pipeline is quiet."""
    q: "queue.Queue" = queue.Queue(maxsize=1000)

    def worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def drive():
            try:
                async for event in async_gen_factory():
                    q.put(event)
            except Exception as exc:  # never let the worker die silently
                q.put({"type": "error", "scope": "fatal", "message": str(exc)})
                q.put({"type": "done", "status": "error", "stats": {}})
            finally:
                q.put(_SENTINEL)

        try:
            loop.run_until_complete(drive())
        finally:
            loop.close()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    while True:
        try:
            item = q.get(timeout=config.SSE_HEARTBEAT_SECS)
        except queue.Empty:
            yield ": hb\n\n"
            continue
        if item is _SENTINEL:
            break
        yield format_frame(item)
