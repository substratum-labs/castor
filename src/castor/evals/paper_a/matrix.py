"""Paper A experiment matrix runner (S-Pay effect-safety slice).

Produces the core comparison table:

* C-full under kill_after_commit / kill_after_success → 0 dups
* Ablations / naive baseline → predictable dups or loss patterns

Usage::

    python -m castor.evals.paper_a.matrix --out /tmp/paper_a_results
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import time
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from castor.evals.paper_a.harness import SPayHarnessResult, run_s_pay_kill_trial

DEFAULT_SYSTEMS = (
    "c_full",
    "c_no_op_id",
    "c_no_dedup",
    "b_naive",
    "b_langgraph",
)
DEFAULT_FAULTS = ("kill_after_commit", "kill_after_success")
MODULE_NAME = "castor.evals.paper_a.matrix"


@dataclass(frozen=True)
class TrialResult:
    """One matrix cell trial with wall-clock metadata."""

    system: str
    fault: str
    trial: int
    committed_effects: int
    dup_commits: int
    missing_commits: int
    resume_success: bool
    resumed_checkpoint_status: str | None
    commits: list[str]
    wall_ms: float
    error: str | None = None


def run_trial(
    work_dir: Path,
    *,
    system: str,
    fault: str,
    trial: int = 0,
) -> TrialResult:
    """Run a single (system, fault) trial under an isolated work directory."""
    trial_dir = work_dir / f"{system}__{fault}__t{trial}"
    if trial_dir.exists():
        # Fresh DB paths per trial.
        for child in trial_dir.iterdir():
            if child.is_file():
                child.unlink()
    trial_dir.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    try:
        result: SPayHarnessResult = run_s_pay_kill_trial(
            trial_dir,
            system=system,  # type: ignore[arg-type]
            fault=fault,  # type: ignore[arg-type]
            pid=f"paper-a-{system}-{fault}-t{trial}",
        )
        wall_ms = (time.perf_counter() - started) * 1000.0
        return TrialResult(
            system=result.system,
            fault=result.fault,
            trial=trial,
            committed_effects=result.committed_effects,
            dup_commits=result.dup_commits,
            missing_commits=result.missing_commits,
            resume_success=result.resume_success,
            resumed_checkpoint_status=result.resumed_checkpoint_status,
            commits=list(result.commits),
            wall_ms=wall_ms,
        )
    except Exception as exc:  # noqa: BLE001 — matrix must record failures
        wall_ms = (time.perf_counter() - started) * 1000.0
        return TrialResult(
            system=system,
            fault=fault,
            trial=trial,
            committed_effects=-1,
            dup_commits=-1,
            missing_commits=-1,
            resume_success=False,
            resumed_checkpoint_status=None,
            commits=[],
            wall_ms=wall_ms,
            error=f"{type(exc).__name__}: {exc}",
        )


def run_matrix(
    work_dir: Path,
    *,
    systems: Sequence[str] = DEFAULT_SYSTEMS,
    faults: Sequence[str] = DEFAULT_FAULTS,
    trials: int = 1,
) -> list[TrialResult]:
    """Run the full S-Pay effect-safety matrix."""
    results: list[TrialResult] = []
    for system in systems:
        for fault in faults:
            for trial in range(trials):
                results.append(
                    run_trial(
                        work_dir,
                        system=system,
                        fault=fault,
                        trial=trial,
                    )
                )
    return results


def results_to_markdown(results: Iterable[TrialResult]) -> str:
    """Render a GitHub-flavored markdown table for paper drafts."""
    lines = [
        "| system | fault | trial | committed | dups | missing | resume_ok | "
        "status | wall_ms | error |",
        "|---|---|---:|---:|---:|---:|:---:|---|---:|---|",
    ]
    for r in results:
        lines.append(
            "| {system} | {fault} | {trial} | {committed_effects} | {dup_commits} | "
            "{missing_commits} | {resume} | {status} | {wall:.1f} | {error} |".format(
                system=r.system,
                fault=r.fault,
                trial=r.trial,
                committed_effects=r.committed_effects,
                dup_commits=r.dup_commits,
                missing_commits=r.missing_commits,
                resume="yes" if r.resume_success else "no",
                status=r.resumed_checkpoint_status or "—",
                wall=r.wall_ms,
                error=(r.error or "").replace("|", "/"),
            )
        )
    return "\n".join(lines) + "\n"


def write_results(
    out_dir: Path,
    results: list[TrialResult],
    *,
    manifest: dict[str, object],
) -> None:
    """Write JSON + markdown artifacts under ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = [asdict(r) for r in results]
    (out_dir / "results.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / "results.md").write_text(results_to_markdown(results), encoding="utf-8")
    (out_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Paper A S-Pay evaluation matrix")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("results/paper_a"),
        help="Directory for results.json / results.md",
    )
    parser.add_argument(
        "--work",
        type=Path,
        default=None,
        help="Scratch directory for per-trial DBs (default: <out>/work)",
    )
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--label", default="adhoc")
    parser.add_argument(
        "--systems",
        nargs="+",
        default=list(DEFAULT_SYSTEMS),
        choices=list(DEFAULT_SYSTEMS),
    )
    parser.add_argument(
        "--faults",
        nargs="+",
        default=list(DEFAULT_FAULTS),
        choices=list(DEFAULT_FAULTS),
    )
    args = parser.parse_args(argv)

    work_dir = args.work or (args.out / "work")
    results = run_matrix(
        work_dir,
        systems=args.systems,
        faults=args.faults,
        trials=args.trials,
    )
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
            "--systems",
            *args.systems,
            "--faults",
            *args.faults,
            "--trials",
            str(args.trials),
        )
    )
    write_results(
        args.out,
        results,
        manifest={
            "label": args.label,
            "trials": args.trials,
            "systems": args.systems,
            "faults": args.faults,
            "result_count": len(results),
            "command": command,
            "git_commit": subprocess.run(
                ["git", "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),  # noqa: UP017
        },
    )
    print(results_to_markdown(results), end="")
    failures = [r for r in results if r.error]
    # Scientific failures (dups on c_full) also exit non-zero for CI.
    science_fail = [
        r
        for r in results
        if r.error is None
        and r.system == "c_full"
        and (r.dup_commits != 0 or not r.resume_success)
    ]
    if failures or science_fail:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
