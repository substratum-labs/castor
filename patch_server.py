import sys

path = '../tiphys/src/tiphys/panel/server.py'
with open(path, 'r') as f:
    content = f.read()

# 1. Add fields to TaskRecord
old_fields = '    speculative: bool = True'
new_fields = '    speculative: bool = True\n    parent_pid: str | None = None\n    fork_step: int | None = None'
content = content.replace(old_fields, new_fields)

# 2. Update to_summary
old_summary = '            "real_cost": round(self.real_cost, 4),'
new_summary = '            "real_cost": round(self.real_cost, 4),\n            "parent_pid": self.parent_pid,\n            "fork_step": self.fork_step,'
content = content.replace(old_summary, new_summary)

# 3. Update _run_forked signature
old_run_forked_def = '    async def _run_forked(\n        self,\n        original_task: TaskRecord,\n        forked_cp: AgentCheckpoint,\n        name: str,\n    ) -> None:'
new_run_forked_def = '    async def _run_forked(\n        self,\n        original_task: TaskRecord,\n        forked_cp: AgentCheckpoint,\n        name: str,\n        parent_pid: str | None = None,\n        fork_step: int | None = None,\n    ) -> None:'
content = content.replace(old_run_forked_def, new_run_forked_def)

# 4. Update _run_forked implementation
old_run_call = '        try:\n            await self.run('
new_run_call = '        try:\n            # Record parent relationship\n            new_pid = f"{name}-{self._task_counter+1}" # Prediction of pid from run()\n            # But run() sets _task_counter += 1 and uses f"{name}-{self._task_counter}"\n            # Let\'s pass parent info to run if possible, or set it after\n            # Actually, run() returns the checkpoint but we need to set parent_pid on the record.\n            \n            # We need to modify run() to accept parent_pid or do it here.\n            # Simplified: run() will create a record, we find it by pid and patch it.\n            cp = await self.run('
content = content.replace(old_run_call, new_run_call)

# Wait, modifying run() is cleaner. Let's do that.
old_run_def = '    async def run(\n        self,\n        kernel: Any,\n        agent_fn: Any,\n        *,\n        budgets: dict[str, float] | None = None,\n        speculative: bool = True,\n        task_name: str | None = None,\n        **kwargs: Any,\n    ) -> AgentCheckpoint:'

new_run_def = '    async def run(\n        self,\n        kernel: Any,\n        agent_fn: Any,\n        *,\n        budgets: dict[str, float] | None = None,\n        speculative: bool = True,\n        task_name: str | None = None,\n        parent_pid: str | None = None,\n        fork_step: int | None = None,\n        **kwargs: Any,\n    ) -> AgentCheckpoint:'
content = content.replace(old_run_def, new_run_def)

old_task_rec_init = '        task_rec = TaskRecord(\n            pid=f"{name}-{self._task_counter}",\n            name=name,\n            status="running",\n            budget_total=sum(budgets.values()) if budgets else 0,\n            start_time=time.time(),\n        )'
new_task_rec_init = '        task_rec = TaskRecord(\n            pid=f"{name}-{self._task_counter}",\n            name=name,\n            status="running",\n            budget_total=sum(budgets.values()) if budgets else 0,\n            start_time=time.time(),\n            parent_pid=parent_pid,\n            fork_step=fork_step,\n        )'
content = content.replace(old_task_rec_init, new_task_rec_init)

# Now update _run_forked to pass these
old_run_forked_call = '            await self.run(\n                original_task.kernel,\n                original_task.agent_fn,\n                budgets={r: c.max_budget for r, c in forked_cp.capabilities.items()},\n                speculative=original_task.speculative,\n                task_name=name,\n                checkpoint=forked_cp,\n            )'
new_run_forked_call = '            await self.run(\n                original_task.kernel,\n                original_task.agent_fn,\n                budgets={r: c.max_budget for r, c in forked_cp.capabilities.items()},\n                speculative=original_task.speculative,\n                task_name=name,\n                parent_pid=parent_pid,\n                fork_step=fork_step,\n                checkpoint=forked_cp,\n            )'
content = content.replace(old_run_forked_call, new_run_forked_call)

# Finally update handlers to pass parent_pid and fork_step to _run_forked
old_rollback_run = '        asyncio.create_task(\n            self._run_forked(\n                task,\n                forked,\n                f"{task.name}::rollback@{step}",\n            )\n        )'
new_rollback_run = '        asyncio.create_task(\n            self._run_forked(\n                task,\n                forked,\n                f"{task.name}::rollback@{step}",\n                parent_pid=pid,\n                fork_step=step,\n            )\n        )'
content = content.replace(old_rollback_run, new_rollback_run)

old_fork_run = '        asyncio.create_task(\n            self._run_forked(\n                task,\n                forked,\n                f"{task.name}::fork@{step}",\n            )\n        )'
new_fork_run = '        asyncio.create_task(\n            self._run_forked(\n                task,\n                forked,\n                f"{task.name}::fork@{step}",\n                parent_pid=pid,\n                fork_step=step,\n            )\n        )'
content = content.replace(old_fork_run, new_fork_run)

with open(path, 'w') as f:
    f.write(content)
