"""castor.lib — standard library for agent developers."""

from castor.lib.patterns import (
    conversation,
    map_reduce,
    parallel,
    plan_execute,
    react,
    supervisor,
)
from castor.lib.primitives import budget, chat, tool, try_tool
from castor.lib.run_task import run_task
from castor.lib.spawn import join, spawn

__all__ = [
    "budget",
    "chat",
    "conversation",
    "join",
    "map_reduce",
    "parallel",
    "plan_execute",
    "react",
    "run_task",
    "spawn",
    "supervisor",
    "tool",
    "try_tool",
]
