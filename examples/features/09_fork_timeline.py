"""Demo 09 — Fork & Time-Travel: Try the alternate future.

Your agent made a wrong call at step N. With Castor, you don't redo from
scratch. You fork at step N-1, change the inputs, and explore the alternate
timeline. Both timelines coexist as first-class checkpoints — replay-safe,
inspectable, comparable side-by-side.

Scenario: a deploy agent picks production when it should have picked
staging. We rewind, switch the deployment policy, and watch the same agent
take the safer path. No re-running the prefix. No flaky reproduction.
Just a deterministic alternate history.

Run (video pacing, ~35s):
    uv run python examples/features/09_fork_timeline.py

Run (fast, no pauses — for iteration):
    CASTOR_DEMO_FAST=1 uv run python examples/features/09_fork_timeline.py

Recording tips:
- Terminal width: at least 100 cols (the side-by-side panel is ~95 wide).
- Font: 18-22pt for screen capture. iTerm2/Ghostty/Alacritty all fine.
- Capture: `asciinema rec demo.cast` then publish, or OBS / QuickTime
  screen recording for a proper .mp4. Asciinema is faster + selectable text.
- Twitter: cuts at 2:20; this demo is 35s, fits comfortably.
"""

import asyncio
import os

from castor import Castor
from castor.lib import tool

# ── Pacing (video-ready by default; export CASTOR_DEMO_FAST=1 to skip pauses) ──
#
# When recording: leave defaults. Each section + key beat gets a hold so the
# viewer can read. When iterating on the script: `CASTOR_DEMO_FAST=1 uv run …`.

FAST = os.getenv("CASTOR_DEMO_FAST") == "1"
BEAT_TINY = 0.0 if FAST else 0.35   # between consecutive trace lines
BEAT_SHORT = 0.0 if FAST else 0.9   # after a section header
BEAT_MED = 0.0 if FAST else 1.6     # after a complete section
BEAT_LONG = 0.0 if FAST else 2.5    # major narrative pivots
BEAT_HOLD = 0.0 if FAST else 4.0    # hold a finished frame so it can land


async def _pause(secs: float) -> None:
    if secs > 0:
        await asyncio.sleep(secs)


# ── ANSI helpers ──

BOLD = "\033[1m"
DIM = "\033[90m"
CYAN = "\033[1;36m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
RESET = "\033[0m"


def _h(text: str) -> None:
    print(f"\n{CYAN}═══ {text} ═══{RESET}")


def _step(text: str) -> None:
    print(f"  {DIM}>{RESET} {text}")


def _ok(text: str) -> None:
    print(f"  {GREEN}✓{RESET} {text}")


def _bad(text: str) -> None:
    print(f"  {RED}✗{RESET} {text}")


def _info(text: str) -> None:
    print(f"  {DIM}{text}{RESET}")


# ── Tools ──
#
# Plain async functions. Castor wraps them as syscalls automatically.

async def recon() -> str:
    """Inspect current production health."""
    return "high traffic on production DB; latency p99 = 480ms"


async def deploy(target: str) -> str:
    """Deploy to a target environment."""
    if target == "production":
        return "INCIDENT: error rate jumped to 30%, pager fired"
    return f"OK: rollout to {target} complete"


async def rollback() -> str:
    """Roll back the last deployment."""
    return "reverted to previous version"


async def notify(message: str) -> str:
    """Notify the on-call team."""
    return f"team notified: {message[:60]}"


# ── Agent ──
#
# Single agent function. Its only branch point is `DEPLOYMENT_POLICY`,
# a module-level setting that an operator could flip during an incident
# response.

DEPLOYMENT_POLICY = "aggressive"  # or "cautious"


async def deploy_agent() -> str:
    health = await tool("recon")  # noqa: F841 — read for context, used downstream in real life

    target = "production" if DEPLOYMENT_POLICY == "aggressive" else "staging"

    result = await tool("deploy", target=target)

    if "INCIDENT" in result:
        await tool("rollback")
        await tool("notify", message=f"INCIDENT: {target} deploy failed, rolled back")
        return f"FAILED: {target}"

    await tool("notify", message=f"deployed to {target} cleanly")
    return f"SUCCESS: {target}"


# ── Demo ──


COL_WIDTH = 44  # plain-text width per side-by-side column


def _format_syscall(record, sig_max: int = 70, resp_max: int = 70) -> tuple[str, str]:
    """Return (signature, response) plain-text strings, truncated to fit."""
    name = record.request["tool_name"]
    args = record.request.get("arguments", {})
    args_str = ", ".join(f"{k}={_truncate(repr(v), 40)}" for k, v in args.items())
    sig = f"{name}({args_str})" if args_str else f"{name}()"
    resp = repr(record.response)
    return _truncate(sig, sig_max), _truncate(resp, resp_max)


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _pad(plain: str, width: int) -> str:
    """Pad plain-text string to width (caller is responsible for no ANSI inside)."""
    return plain + " " * max(0, width - len(plain))


def _print_side_by_side(label_a: str, cp_a, label_b: str, cp_b, divergence_at: int) -> None:
    """Print two timelines aligned at the divergence point."""
    sep = "│"
    rule = "─" * COL_WIDTH

    print()
    print(f"  {BOLD}{_pad(label_a, COL_WIDTH)}{RESET} {sep} {BOLD}{label_b}{RESET}")
    print(f"  {DIM}{rule}{RESET} {sep} {DIM}{rule}{RESET}")

    max_steps = max(len(cp_a.syscall_log), len(cp_b.syscall_log))
    for i in range(max_steps):
        if i == divergence_at:
            banner = "── divergence point ──"
            cell = banner.center(COL_WIDTH)
            print(f"  {YELLOW}{cell}{RESET} {sep} {YELLOW}{cell}{RESET}")

        left_sig, left_resp = ("", "")
        right_sig, right_resp = ("", "")

        narrow = COL_WIDTH - 6
        if i < len(cp_a.syscall_log):
            left_sig, left_resp = _format_syscall(cp_a.syscall_log[i], sig_max=narrow, resp_max=narrow)
            color_left = RED if "INCIDENT" in left_resp or "FAIL" in left_resp.upper() else BLUE
        else:
            color_left = DIM
        if i < len(cp_b.syscall_log):
            right_sig, right_resp = _format_syscall(cp_b.syscall_log[i], sig_max=narrow, resp_max=narrow)
            color_right = GREEN if "OK" in right_resp else BLUE
        else:
            color_right = DIM

        # Row 1: index + signature
        if left_sig:
            left_plain = f"[{i}] {left_sig}"
            left_ansi = f"{DIM}[{i}]{RESET} {color_left}{left_sig}{RESET}"
        else:
            left_plain, left_ansi = "—", f"{DIM}—{RESET}"
        if right_sig:
            tag = " [REPLAY]" if i < divergence_at else ""
            right_plain = f"[{i}] {right_sig}{tag}"
            right_ansi = f"{DIM}[{i}]{RESET} {color_right}{right_sig}{RESET}{DIM}{tag}{RESET}"
        else:
            right_plain, right_ansi = "—", f"{DIM}—{RESET}"

        # Compose: pad based on plain length, then substitute styled version
        left_pad = " " * max(0, COL_WIDTH - len(left_plain))
        right_pad = ""  # right column doesn't need trailing pad
        print(f"  {left_ansi}{left_pad} {sep} {right_ansi}{right_pad}")

        # Row 2: response
        if left_resp or right_resp:
            l_plain = f"    → {left_resp}" if left_resp else ""
            l_ansi = f"    {DIM}→ {left_resp}{RESET}" if left_resp else ""
            r_ansi = f"    {DIM}→ {right_resp}{RESET}" if right_resp else ""
            l_pad = " " * max(0, COL_WIDTH - len(l_plain))
            print(f"  {l_ansi}{l_pad} {sep} {r_ansi}")


async def main() -> None:
    global DEPLOYMENT_POLICY

    # Title card — held so the viewer reads the framing before action begins.
    print(f"\n{BOLD}Castor — Fork & Time-Travel demo{RESET}")
    print(f"{DIM}Same agent function, two divergent futures, both deterministic.{RESET}")
    await _pause(BEAT_LONG)

    kernel = Castor(
        tools=[recon, deploy, rollback, notify],
        destructive=["deploy", "rollback"],
    )

    # ── Timeline A: aggressive policy ──
    _h("Timeline A — operator policy: AGGRESSIVE")
    await _pause(BEAT_SHORT)
    DEPLOYMENT_POLICY = "aggressive"
    _info("agent will pick `production` on this run")
    await _pause(BEAT_SHORT)

    # speculative=True flags destructive ops with needs_review but does not
    # pause for HITL — the whole point here is to LET the bad decision happen
    # so we can compare the trace against the alternate timeline.
    cp_a = await kernel.run(deploy_agent, pid="incident-001", speculative=True)

    # Print step-by-step with a beat between lines so the viewer can read.
    for i, rec in enumerate(cp_a.syscall_log):
        sig, resp = _format_syscall(rec)
        if "INCIDENT" in resp:
            _bad(f"step {i}: {sig} → {RED}{resp}{RESET}")
            await _pause(BEAT_MED)  # let the failure register
        else:
            _step(f"step {i}: {sig} → {resp}")
            await _pause(BEAT_TINY)

    await _pause(BEAT_SHORT)
    print(f"\n  Status: {YELLOW}{cp_a.status}{RESET}")
    print(f"  Result: {RED}{cp_a.result}{RESET}")
    _bad("4-syscall trail. Production is on fire.")
    await _pause(BEAT_LONG)

    # ── Time-travel: fork before the bad decision ──
    _h("Time-travel: fork at step 1 (after recon, before deploy)")
    await _pause(BEAT_SHORT)
    cp_forked = cp_a.fork(at_step=1)
    _ok(f"forked checkpoint pid: {MAGENTA}{cp_forked.pid}{RESET}")
    await _pause(BEAT_TINY)
    _info(f"prefix preserved: {len(cp_forked.syscall_log)} syscall(s) carried over")
    await _pause(BEAT_TINY)
    _info(f"status reset to: {cp_forked.status}")
    await _pause(BEAT_MED)

    # ── Timeline B: cautious policy on the fork ──
    _h("Timeline B — operator policy: CAUTIOUS (rerun from fork)")
    await _pause(BEAT_SHORT)
    DEPLOYMENT_POLICY = "cautious"
    _info("policy changed; agent will pick `staging` on the live portion")
    await _pause(BEAT_SHORT)

    cp_b = await kernel.run(deploy_agent, checkpoint=cp_forked, speculative=True)

    for i, rec in enumerate(cp_b.syscall_log):
        sig, resp = _format_syscall(rec)
        prefix = f"{DIM}[REPLAY]{RESET} " if i < 1 else f"{GREEN}[LIVE]{RESET}   "
        if "OK" in resp:
            _step(f"{prefix}step {i}: {sig} → {GREEN}{resp}{RESET}")
        else:
            _step(f"{prefix}step {i}: {sig} → {resp}")
        await _pause(BEAT_TINY)

    await _pause(BEAT_SHORT)
    print(f"\n  Status: {GREEN}{cp_b.status}{RESET}")
    print(f"  Result: {GREEN}{cp_b.result}{RESET}")
    await _pause(BEAT_LONG)

    # ── Side-by-side comparison (the payoff frame) ──
    _h("Side-by-side: two futures from the same past")
    await _pause(BEAT_SHORT)
    _print_side_by_side(
        f"A — {cp_a.pid}",
        cp_a,
        f"B — {cp_b.pid}",
        cp_b,
        divergence_at=1,
    )
    await _pause(BEAT_MED)

    print()
    print(f"  {BOLD}A:{RESET} {RED}{cp_a.result}{RESET}    {BOLD}B:{RESET} {GREEN}{cp_b.result}{RESET}")
    print()
    await _pause(BEAT_LONG)

    # Closing narration — typed line by line so each beat lands.
    closing = [
        "Both checkpoints exist as first-class state. Either can be replayed,",
        "forked again, or compared. The original timeline isn't rewritten —",
        "it's an alternate that still happened. This is what makes Castor's",
        "post-hoc analysis meaningful: every 'wrong' decision keeps a",
        "counterfactual you can actually run.",
    ]
    for line in closing:
        _info(line)
        await _pause(BEAT_SHORT)
    print()
    await _pause(BEAT_HOLD)  # hold final frame so video can end on a still


if __name__ == "__main__":
    asyncio.run(main())
