"""One pinned playwright-mcp child process, attached to the shared Chrome.

The stdio transport is an async context manager that must be entered and left
in the same task, so each child gets a holder task that keeps it open until
``close``; callers in any task use the session it hands back.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, Tool

logger = logging.getLogger(__name__)

START_TIMEOUT_SECONDS = 60
CLOSE_TIMEOUT_SECONDS = 10
# A call that outlives this raises instead of holding its session's lock (and
# so idle reaping and browser_close) forever. The pinned server's own waits
# cap at 30s (browser_wait_for), its navigation timeouts at 60s.
CALL_TIMEOUT = timedelta(seconds=120)


@dataclass(frozen=True)
class ChildSpec:
    binary: str
    cdp_port: int
    output_dir: str
    workspace: str
    path: str

    def params(self) -> StdioServerParameters:
        return StdioServerParameters(
            command=self.binary,
            args=[
                "--cdp-endpoint",
                f"http://127.0.0.1:{self.cdp_port}",
                "--output-dir",
                self.output_dir,
            ],
            # playwright-mcp confines file access to its cwd: never the repo.
            cwd=self.workspace,
            env={"PATH": self.path},
        )


class PlaywrightChild:
    def __init__(
        self, session: ClientSession, stop: asyncio.Event, task: asyncio.Task[None]
    ) -> None:
        self._session = session
        self._stop = stop
        self._task = task

    @classmethod
    async def start(cls, spec: ChildSpec) -> PlaywrightChild:
        ready: asyncio.Future[ClientSession] = (
            asyncio.get_running_loop().create_future()
        )
        stop = asyncio.Event()

        async def hold() -> None:
            try:
                async with (
                    stdio_client(spec.params()) as (read, write),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
                    ready.set_result(session)
                    await stop.wait()
            except Exception as exc:
                if not ready.done():
                    ready.set_exception(exc)
                else:
                    logger.warning("playwright child exited: %s", exc)

        task = asyncio.create_task(hold())
        try:
            session = await asyncio.wait_for(
                asyncio.shield(ready), START_TIMEOUT_SECONDS
            )
        except BaseException:
            stop.set()
            task.cancel()
            raise
        return cls(session, stop, task)

    async def call(self, name: str, args: dict[str, Any]) -> CallToolResult:
        return await self._session.call_tool(
            name, args, read_timeout_seconds=CALL_TIMEOUT
        )

    async def list_tools(self) -> list[Tool]:
        return (await self._session.list_tools()).tools

    async def close(self) -> None:
        self._stop.set()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(self._task, CLOSE_TIMEOUT_SECONDS)
