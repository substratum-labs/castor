import sys

path = '../tiphys/src/tiphys/panel/server.py'
content = r'''"""Control Panel server — multi-task dashboard with real-time WebSocket events.

Usage:
    panel = ControlPanel(port=8766)
    await panel.start()
    cp1 = await panel.run(kernel, agent_fn, budgets={"api": 50}, task_name="audit")
    cp2 = await panel.run(kernel, agent_fn2, budgets={"api": 30}, task_name="build")
    # Browser shows both tasks in sidebar, click to view timeline
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web
from castor.models.checkpoint import AgentCheckpoint

from tiphys.interactions.adapters.web import WebInteractionAdapter
from tiphys.interactions.manager import InteractionManager
from tiphys.panel.template import DASHBOARD_HTML


@dataclass
class TaskRecord:
    """Record of a single task execution."""

    pid: str
    name: str
    status: str  # idle, running, suspended, completed, error, killed
    events: list[dict[str, Any]] = field(default_factory=list)
    budget_total: float = 0.0
    budget_used: float = 0.0
    real_cost: float = 0.0
    start_time: float = 0.0
    end_time: float = 0.0
    result: str | None = None
    checkpoint: AgentCheckpoint | None = None
    # Stored for rollback/fork re-runs
    agent_fn: Any = None
    kernel: Any = None
    speculative: bool = True
    parent_pid: str | None = None
    fork_step: int | None = None

    @property
    def elapsed(self) -> float:
        end = self.end_time or time.time()
        return end - self.start_time if self.start_time else 0.0

    @property
    def step_count(self) -> int:
        return len(self.events)

    def to_summary(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "name": self.name,
            "status": self.status,
            "steps": self.step_count,
            "budget_used": round(self.budget_used, 1),
            "budget_total": round(self.budget_total, 1),
            "elapsed": round(self.elapsed, 1),
            "real_cost": round(self.real_cost, 4),
            "parent_pid": self.parent_pid,
            "fork_step": self.fork_step,
        }

    def to_detail(self) -> dict[str, Any]:
        return {
            **self.to_summary(),
            "events": self.events,
            "result": self.result,
        }


class ControlPanel:
    """Real-time control panel for agent execution."""

    def __init__(self, port: int = 8766, auto_open: bool = True) -> None:
        self._port = port
        self._auto_open = auto_open
        self._runner: web.AppRunner | None = None
        self._ws_clients: list[web.WebSocketResponse] = []
        self._tasks: dict[str, TaskRecord] = {}
        self._active_task: str | None = None
        self._hitl_future: asyncio.Future | None = None
        self._task_counter = 0
        self._running_tasks: dict[str, asyncio.Task] = {}  # pid → asyncio.Task for kill
        self._interaction_mgr = InteractionManager()
        self._web_adapter = WebInteractionAdapter()
        self._last_interaction_msg: dict | None = None  # For late-joining clients

    @property
    def interactions(self) -> InteractionManager:
        """Access the interaction manager for requesting user input."""
        return self._interaction_mgr

    async def start(self) -> None:
        """Start the control panel web server."""
        app = web.Application()
        app.router.add_get("/", self._handle_page)
        app.router.add_get("/ws", self._handle_ws)
        app.router.add_get("/api/tasks", self._handle_tasks)
        app.router.add_get("/api/task/{pid}", self._handle_task_detail)
        app.router.add_post("/api/hitl/{action}", self._handle_hitl)
        app.router.add_post("/api/task/{pid}/kill", self._handle_kill)
        app.router.add_post("/api/task/{pid}/rollback/{step}", self._handle_rollback)
        app.router.add_post("/api/task/{pid}/fork/{step}", self._handle_fork)
        app.router.add_post("/api/interaction/{request_id}", self._handle_interaction_response)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "localhost", self._port)
        await site.start()

        # Wire interaction adapter to broadcast + cache for late joiners
        original_adapter_handle = self._web_adapter.handle

        async def _caching_handle(interaction):
            msg = self._web_adapter.to_ws_message(interaction)
            if msg:
                self._last_interaction_msg = msg
            await original_adapter_handle(interaction)

        self._web_adapter.handle = _caching_handle
        self._web_adapter.set_broadcast(self._broadcast)
        self._interaction_mgr.add_adapter(self._web_adapter)

        url = f"http://localhost:{self._port}"
        print(f"  🖥️  Control Panel: {url}")

        if self._auto_open:
            if sys.platform == "darwin":
                subprocess.Popen(["open", url])
            elif sys.platform == "linux":
                subprocess.Popen(["xdg-open", url])

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    async def run(
        self,
        kernel: Any,
        agent_fn: Any,
        *,
        budgets: dict[str, float] | None = None,
        speculative: bool = True,
        parent_pid: str | None = None,
        fork_step: int | None = None,
        task_name: str | None = None,
        **kwargs: Any,
    ) -> AgentCheckpoint:
        """Run an agent with the control panel tracking execution."""
        self._task_counter += 1
        name = task_name or f"task-{self._task_counter}"

        task_rec = TaskRecord(
            pid=f"{name}-{self._task_counter}",
            name=name,
            status="running",
            budget_total=sum(budgets.values()) if budgets else 0,
            start_time=time.time(),
            parent_pid=parent_pid,
            fork_step=fork_step,
        )
        task_rec.agent_fn = agent_fn
        task_rec.kernel = kernel
        task_rec.speculative = speculative
        self._tasks[task_rec.pid] = task_rec
        self._active_task = task_rec.pid

        await self._broadcast(
            {
                "type": "task_started",
                "task": task_rec.to_summary(),
            }
        )

        # Wrap agent to intercept syscalls
        original_fn = agent_fn

        async def tracked_agent(proxy):
            async def tracked_syscall(tool_name, arguments=None, /, **kw):
                step_idx = len(proxy._journal)
                await self._broadcast(
                    {
                        "type": "step_start",
                        "pid": task_rec.pid,
                        "index": step_idx,
                        "tool": tool_name,
                        "args": str(arguments or kw)[:200],
                        "timestamp": time.time(),
                    }
                )

                t0 = time.time()
                result = await proxy.__class__.syscall(proxy, tool_name, arguments, **kw)
                elapsed = time.time() - t0

                task_rec.budget_used = sum(
                    c.current_usage for c in proxy.checkpoint.capabilities.values()
                )

                event = {
                    "type": "step_complete",
                    "pid": task_rec.pid,
                    "index": step_idx,
                    "tool": tool_name,
                    "response": str(result)[:500],
                    "elapsed": round(elapsed, 2),
                    "budget_used": round(task_rec.budget_used, 1),
                    "budget_total": round(task_rec.budget_total, 1),
                    "timestamp": time.time(),
                }
                task_rec.events.append(event)
                await self._broadcast(event)

                # Budget warnings
                if task_rec.budget_total > 0:
                    pct = task_rec.budget_used / task_rec.budget_total * 100
                    if pct >= 90:
                        await self._broadcast(
                            {
                                "type": "budget_alert",
                                "pid": task_rec.pid,
                                "level": "critical",
                                "message": f"Budget critical: {pct:.0f}% used",
                                "used": round(task_rec.budget_used, 1),
                                "total": round(task_rec.budget_total, 1),
                            }
                        )
                    elif pct >= 75:
                        await self._broadcast(
                            {
                                "type": "budget_alert",
                                "pid": task_rec.pid,
                                "level": "warning",
                                "message": f"Budget warning: {pct:.0f}% used",
                                "used": round(task_rec.budget_used, 1),
                                "total": round(task_rec.budget_total, 1),
                            }
                        )

                return result

            proxy.syscall = tracked_syscall
            return await original_fn(proxy)

        try:
            # Wrap in asyncio.Task so kill can cancel it
            run_coro = kernel.run(
                tracked_agent,
                budgets=budgets,
                speculative=speculative,
                **kwargs,
            )
            run_task = asyncio.create_task(run_coro)
            self._running_tasks[task_rec.pid] = run_task
            cp = await run_task
        except asyncio.CancelledError:
            task_rec.status = "killed"
            task_rec.end_time = time.time()
            task_rec.result = "Killed by operator"
            await self._broadcast(
                {
                    "type": "task_completed",
                    "task": task_rec.to_summary(),
                    "result": task_rec.result,
                }
            )
            # Return a minimal checkpoint
            cp = AgentCheckpoint(
                pid=task_rec.pid,
                status="PREEMPTED",
                agent_function_name="killed",
                capabilities={},
            )
            return cp
        except Exception as e:
            task_rec.status = "error"
            task_rec.end_time = time.time()
            task_rec.result = str(e)
            await self._broadcast({"type": "task_updated", "task": task_rec.to_summary()})
            raise
        finally:
            self._running_tasks.pop(task_rec.pid, None)

        task_rec.checkpoint = cp
        task_rec.end_time = time.time()

        if cp.status == "SUSPENDED_FOR_HITL":
            task_rec.status = "suspended"
            await self._broadcast(
                {
                    "type": "hitl",
                    "pid": task_rec.pid,
                    "tool": cp.pending_hitl.get("tool_name") if cp.pending_hitl else "?",
                    "args": str(cp.pending_hitl.get("arguments", ""))[:300]
                    if cp.pending_hitl
                    else "",
                }
            )
            await self._broadcast({"type": "task_updated", "task": task_rec.to_summary()})

            self._hitl_future = asyncio.get_event_loop().create_future()
            action = await self._hitl_future
            self._hitl_future = None

            if action == "approve":
                await kernel.approve(cp)
                task_rec.status = "running"
                await self._broadcast({"type": "task_updated", "task": task_rec.to_summary()})
                cp = await kernel.run(original_fn, checkpoint=cp, speculative=speculative)
                task_rec.checkpoint = cp
                task_rec.end_time = time.time()

        task_rec.status = cp.status.lower() if cp.status else "completed"
        if task_rec.status == "completed":
            task_rec.result = str(cp.result)[:500] if cp.result else None

        await self._broadcast(
            {
                "type": "task_completed",
                "task": task_rec.to_summary(),
                "result": task_rec.result,
            }
        )

        return cp

    # ── HTTP handlers ──

    async def _handle_page(self, request: web.Request) -> web.Response:
        return web.Response(text=DASHBOARD_HTML, content_type="text/html")

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws_clients.append(ws)

        await ws.send_json(
            {
                "type": "init",
                "tasks": [t.to_summary() for t in self._tasks.values()],
                "active_task": self._active_task,
            }
        )

        # Send active task detail if exists
        if self._active_task and self._active_task in self._tasks:
            task = self._tasks[self._active_task]
            await ws.send_json(
                {
                    "type": "task_detail",
                    "task": task.to_detail(),
                }
            )

        # Send pending interaction (for late-joining clients)
        if self._last_interaction_msg and self._interaction_mgr.pending_count > 0:
            await ws.send_json(self._last_interaction_msg)

        try:
            async for msg in ws:
                if msg.type == 1:  # TEXT
                    data = json.loads(msg.data)
                    if data.get("action") == "select_task":
                        pid = data.get("pid")
                        if pid in self._tasks:
                            await ws.send_json(
                                {
                                    "type": "task_detail",
                                    "task": self._tasks[pid].to_detail(),
                                }
                            )
                    elif data.get("action") == "interaction_response":
                        from tiphys.interactions.types import InteractionResponse

                        resp = InteractionResponse(
                            request_id=data.get("request_id", ""),
                            selected=data.get("selected"),
                            confirmed=data.get("confirmed"),
                            action=data.get("response_action"),
                            feedback=data.get("feedback"),
                        )
                        self._interaction_mgr.respond(resp.request_id, resp)
                        self._last_interaction_msg = None  # Clear cached
        finally:
            self._ws_clients.remove(ws)

        return ws

    async def _handle_tasks(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "tasks": [t.to_summary() for t in self._tasks.values()],
                "active_task": self._active_task,
            }
        )

    async def _handle_task_detail(self, request: web.Request) -> web.Response:
        pid = request.match_info["pid"]
        task = self._tasks.get(pid)
        if not task:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(task.to_detail())

    async def _handle_hitl(self, request: web.Request) -> web.Response:
        action = request.match_info["action"]
        if self._hitl_future and not self._hitl_future.done():
            self._hitl_future.set_result(action)
        return web.Response(text="OK")

    async def _handle_kill(self, request: web.Request) -> web.Response:
        pid = request.match_info["pid"]
        task = self._tasks.get(pid)
        if task and task.status == "running":
            # Actually cancel the running asyncio task
            running = self._running_tasks.get(pid)
            if running and not running.done():
                running.cancel()
            task.status = "killed"
            task.end_time = time.time()
            await self._broadcast(
                {
                    "type": "task_killed",
                    "task": task.to_summary(),
                }
            )
        return web.Response(text="OK")

    async def _handle_rollback(self, request: web.Request) -> web.Response:
        """Rollback to a step and re-run from there."""
        pid = request.match_info["pid"]
        step = int(request.match_info["step"])
        task = self._tasks.get(pid)
        if not task or not task.checkpoint:
            return web.json_response({"error": "task not found or no checkpoint"}, status=404)

        # Fork checkpoint
        forked = task.checkpoint.model_copy(deep=True)
        forked.syscall_log = forked.syscall_log[:step]
        forked.status = "RUNNING"
        forked.result = None
        forked.pid = f"{pid}::rollback-{step}"
        for cap in forked.capabilities.values():
            cap.current_usage = 0.0

        await self._broadcast(
            {
                "type": "task_rollback",
                "original_pid": pid,
                "rollback_step": step,
                "new_pid": forked.pid,
            }
        )

        # Re-run as a new task in background
        asyncio.create_task(
            self._run_forked(
                task,
                forked,
                f"{task.name}::rollback@{step}",
                parent_pid=pid,
                fork_step=step,
            )
        )

        return web.json_response({"new_pid": forked.pid, "rollback_step": step})

    async def _handle_fork(self, request: web.Request) -> web.Response:
        """Fork a new timeline from a step."""
        pid = request.match_info["pid"]
        step = int(request.match_info["step"])
        task = self._tasks.get(pid)
        if not task or not task.checkpoint:
            return web.json_response({"error": "task not found or no checkpoint"}, status=404)

        # Fork checkpoint
        forked = task.checkpoint.model_copy(deep=True)
        forked.syscall_log = forked.syscall_log[:step]
        forked.status = "RUNNING"
        forked.result = None
        forked.pid = f"{pid}::fork-{step}"
        for cap in forked.capabilities.values():
            cap.current_usage = 0.0

        await self._broadcast(
            {
                "type": "task_forked",
                "original_pid": pid,
                "fork_step": step,
                "new_pid": forked.pid,
            }
        )

        asyncio.create_task(
            self._run_forked(
                task,
                forked,
                f"{task.name}::fork@{step}",
                parent_pid=pid,
                fork_step=step,
            )
        )

        return web.json_response({"new_pid": forked.pid, "fork_step": step})

    async def _handle_interaction_response(self, request: web.Request) -> web.Response:
        """Handle user response to an interaction request."""
        request_id = request.match_info["request_id"]
        try:
            data = await request.json()
        except Exception:
            data = {}

        from tiphys.interactions.types import InteractionResponse

        resp = InteractionResponse(
            request_id=request_id,
            selected=data.get("selected"),
            confirmed=data.get("confirmed"),
            action=data.get("action"),
            feedback=data.get("feedback"),
        )
        found = self._interaction_mgr.respond(request_id, resp)
        return web.json_response({"resolved": found})

    async def _run_forked(
        self,
        original_task: TaskRecord,
        forked_cp: AgentCheckpoint,
        name: str,
        parent_pid: str | None = None,
        fork_step: int | None = None,
    ) -> None:
        """Run a forked/rolled-back task."""
        if not original_task.agent_fn or not original_task.kernel:
            return
        try:
            await self.run(
                original_task.kernel,
                original_task.agent_fn,
                budgets={r: c.max_budget for r, c in forked_cp.capabilities.items()},
                speculative=original_task.speculative,
                task_name=name,
                parent_pid=parent_pid,
                fork_step=fork_step,
                checkpoint=forked_cp,
            )
        except Exception:
            await self._broadcast(
                {
                    "type": "task_updated",
                    "task": {
                        "pid": forked_cp.pid,
                        "name": name,
                        "status": "error",
                        "steps": 0,
                        "budget_used": 0,
                        "budget_total": 0,
                        "elapsed": 0,
                    },
                }
            )

    async def _broadcast(self, event: dict[str, Any]) -> None:
        dead = []
        for ws in self._ws_clients:
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._ws_clients.remove(ws)
'''

with open(path, 'w') as f:
    f.write(content)
