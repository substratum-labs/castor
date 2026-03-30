import sys

path = '../tiphys/src/tiphys/panel/server.py'
with open(path, 'r') as f:
    lines = f.readlines()

# Let's rebuild the run method carefully
new_lines = []
skip = False
for i, line in enumerate(lines):
    if 'async def run(' in line:
        new_lines.append('    async def run(\n')
        new_lines.append('        self,\n')
        new_lines.append('        kernel: Any,\n')
        new_lines.append('        agent_fn: Any,\n')
        new_lines.append('        *,\n')
        new_lines.append('        budgets: dict[str, float] | None = None,\n')
        new_lines.append('        speculative: bool = True,\n')
        new_lines.append('        parent_pid: str | None = None,\n')
        new_lines.append('        fork_step: int | None = None,\n')
        new_lines.append('        task_name: str | None = None,\n')
        new_lines.append('        **kwargs: Any,\n')
        new_lines.append('    ) -> AgentCheckpoint:\n')
        skip = True
    elif skip and '"""Run an agent' in line:
        new_lines.append(line)
        skip = False
    elif not skip:
        # Fix the instantiation too while at it
        if 'task_rec = TaskRecord(' in line:
             new_lines.append(line)
             new_lines.append('            pid=f"{name}-{self._task_counter}",\n')
             new_lines.append('            name=name,\n')
             new_lines.append('            status="running",\n')
             new_lines.append('            budget_total=sum(budgets.values()) if budgets else 0,\n')
             new_lines.append('            start_time=time.time(),\n')
             new_lines.append('            parent_pid=parent_pid,\n')
             new_lines.append('            fork_step=fork_step,\n')
             new_lines.append('        )\n')
             skip_instantiation = True
        elif 'skip_instantiation' in locals() and skip_instantiation:
             if ')' in line:
                 skip_instantiation = False
        else:
             new_lines.append(line)

with open(path, 'w') as f:
    f.writelines(new_lines)
