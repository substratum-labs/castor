"""Paper A T-257 negative-control and journal-overhead measurements."""

from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from castor.budget.manager import BudgetManager
from castor.evals.paper_a.harness import (
    SPayHarnessResult,
    run_s_pay_kill_trial,
)
from castor.gate.registry import ToolMetadata, ToolRegistry
from castor.gate.validator import SyscallGate
from castor.models.checkpoint import AgentCheckpoint, SyscallRecord
from castor.scheduler.runner import AgentRunner

MODULE_NAME = "castor.evals.paper_a.overhead"
DEFAULT_LENGTHS = (0, 4, 16, 64, 256)


@dataclass(frozen=True)
class LScalingResult:
    """One measured journal-prefix resume sample."""

    journal_len: int
    journal_bytes: int
    resume_ms: float
    status: str
    error: str | None


def run_s_bypass_trial(
    work_dir: Path,
    *,
    fault: str = "kill_after_commit",
    pid: str = "paper-a-s-bypass",
) -> SPayHarnessResult:
    """Run the raw-I/O negative control through the existing kill harness."""
    return run_s_pay_kill_trial(
        work_dir,
        system="s_bypass",
        fault=fault,  # type: ignore[arg-type]
        pid=pid,
    )


def run_l_scaling(
    work_dir: Path,
    *,
    lengths: Sequence[int],
) -> list[LScalingResult]:
    """Measure replay of deterministic journal prefixes at each length."""
    requested_lengths = tuple(lengths)
    if any(length < 0 for length in requested_lengths):
        raise ValueError("journal lengths must be non-negative")
    if not requested_lengths:
        return []

    work_dir.mkdir(parents=True, exist_ok=True)
    return [_run_l_sample(work_dir, length) for length in requested_lengths]


def _run_l_sample(work_dir: Path, journal_len: int) -> LScalingResult:
    """Build one in-memory checkpoint and measure its replay completion."""
    del work_dir  # Kept in the public API so CLI runs have a stable scratch root.

    def echo(index: int) -> dict[str, int]:
        return {"index": index}

    registry = ToolRegistry()
    registry.register(ToolMetadata.from_function(echo))
    gate = SyscallGate(registry)
    budget_manager = BudgetManager()
    checkpoint = AgentCheckpoint(
        pid=f"paper-a-t257-l{journal_len}",
        status="RUNNING",
        agent_function_name="l_scaling_agent",
        capabilities=budget_manager.create_budgets({"_default": 0.0}),
        syscall_log=[
            SyscallRecord(
                request={
                    "tool_name": "echo",
                    "arguments": {"index": index},
                },
                response={"index": index},
            )
            for index in range(journal_len)
        ],
    )
    journal_bytes = len(checkpoint.model_dump_json().encode("utf-8"))

    async def agent(proxy) -> int:
        for index in range(journal_len):
            await proxy.syscall("echo", {"index": index})
        return journal_len

    started = time.perf_counter()
    try:
        result = asyncio.run(AgentRunner(gate, budget_manager).run(agent, checkpoint))
    except Exception as exc:  # noqa: BLE001 - preserve one row per requested L
        return LScalingResult(
            journal_len=journal_len,
            journal_bytes=journal_bytes,
            resume_ms=(time.perf_counter() - started) * 1000.0,
            status=checkpoint.status,
            error=f"{type(exc).__name__}: {exc}",
        )

    return LScalingResult(
        journal_len=journal_len,
        journal_bytes=journal_bytes,
        resume_ms=(time.perf_counter() - started) * 1000.0,
        status=result.status,
        error=None,
    )


def write_overhead_results(
    out_dir: Path,
    *,
    bypass: SPayHarnessResult,
    scaling: Sequence[LScalingResult],
    manifest: dict[str, object],
) -> None:
    """Write the T-257 JSON and Markdown artifacts."""
    out_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "bypass": {
            "system": bypass.system,
            "fault": bypass.fault,
            "committed_effects": bypass.committed_effects,
            "dup_commits": bypass.dup_commits,
            "missing_commits": bypass.missing_commits,
            "resume_success": bypass.resume_success,
            "resumed_checkpoint_status": bypass.resumed_checkpoint_status,
            "commits": list(bypass.commits),
        },
        "l_scaling": [
            {
                "journal_len": sample.journal_len,
                "journal_bytes": sample.journal_bytes,
                "resume_ms": sample.resume_ms,
                "status": sample.status,
                "error": sample.error,
            }
            for sample in scaling
        ],
        "manifest": manifest,
    }
    (out_dir / "overhead.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    lines = [
        "# T-257 S-Bypass and L-Scaling",
        "",
        "## S-Bypass negative control",
        "",
        (
            "Raw payment I/O is outside the Castor journal; duplicate effects "
            "are expected after crash recovery."
        ),
        "",
        (
            "| system | fault | committed_effects | dup_commits | "
            "resume_success | commits |"
        ),
        "|---|---|---:|---:|:---:|---|",
        "| {system} | {fault} | {committed} | {dups} | {resume} | {commits} |".format(
            system=bypass.system,
            fault=bypass.fault,
            committed=bypass.committed_effects,
            dups=bypass.dup_commits,
            resume="yes" if bypass.resume_success else "no",
            commits=", ".join(bypass.commits),
        ),
        "",
        "## Journal-length scaling",
        "",
        (
            "Prototype checkpoint/journal overhead measurements; no MMU or "
            "asymptotic claim."
        ),
        "",
        "| journal_len | journal_bytes | resume_ms | status | error |",
        "|---:|---:|---:|---|---|",
    ]
    for sample in scaling:
        error = (sample.error or "").replace("|", "/")
        lines.append(
            f"| {sample.journal_len} | {sample.journal_bytes} | "
            f"{sample.resume_ms:.3f} | {sample.status} | {error} |"
        )
    lines.extend(
        [
            "",
            "## Reproduction",
            "",
            f"`{manifest.get('command', '')}`",
            "",
        ]
    )
    (out_dir / "overhead.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    """Run the T-257 benchmark and write labeled artifacts."""
    import argparse

    parser = argparse.ArgumentParser(description="Paper A T-257 overhead benchmark")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("results/paper_a"),
        help="Directory for overhead.json / overhead.md",
    )
    parser.add_argument("--label", default="t-257")
    parser.add_argument("--lengths", nargs="+", type=int, default=list(DEFAULT_LENGTHS))
    args = parser.parse_args(argv)

    work_dir = args.out / "overhead_work"
    bypass = run_s_bypass_trial(work_dir / "s_bypass")
    scaling = run_l_scaling(work_dir / "l_scaling", lengths=args.lengths)
    command = " ".join(
        shlex.quote(part)
        for part in (
            "python",
            "-m",
            MODULE_NAME,
            "--out",
            str(args.out),
            "--label",
            args.label,
            "--lengths",
            *(str(length) for length in args.lengths),
        )
    )
    manifest = {
        "label": args.label,
        "lengths": list(args.lengths),
        "result_count": 1 + len(scaling),
        "command": command,
        "git_commit": subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "generated_at_utc": datetime.now(UTC).isoformat(),
    }
    write_overhead_results(args.out, bypass=bypass, scaling=scaling, manifest=manifest)
    print((args.out / "overhead.md").read_text(encoding="utf-8"), end="")

    if not bypass.resume_success or bypass.dup_commits < 1:
        raise SystemExit("S-Bypass negative control did not observe a duplicate")
    failures = [
        sample
        for sample in scaling
        if sample.error or sample.status != "COMPLETED"
    ]
    if failures:
        raise SystemExit(f"L-scaling contains {len(failures)} failed samples")


if __name__ == "__main__":
    main()
