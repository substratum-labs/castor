#!/usr/bin/env python3
"""Castor Security Levels — Visual Demo

Runs the same agent through three security levels and generates
an interactive HTML page comparing them side by side.
Auto-opens in browser.

Usage:
    uv run python examples/security_levels_web.py
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

# ── Simulated filesystem ──

_FS_INITIAL = {
    "/var/log/app.log.old": "old app logs...",
    "/var/log/nginx.log.7": "old nginx logs...",
    "/var/log/data.db.bak": "IMPORTANT: production database backup!",
    "/var/log/error.log.3": "old error logs...",
    "/var/log/app.log": "current app logs (active)",
    "/var/log/nginx.log": "current nginx logs (active)",
    "/var/log/cleanup_policy.txt": (
        "Policy: delete .old, .bak, and numbered log files older than 7 days.\n"
        "Exception: never delete files containing 'IMPORTANT' in content."
    ),
}
_filesystem: dict[str, str] = {}


def _reset():
    global _filesystem
    _filesystem = dict(_FS_INITIAL)


# ── Tools ──


async def list_files(directory: str) -> str:
    """List files in a directory."""
    return "\n".join(sorted(f for f in _filesystem if f.startswith(directory)))


async def read_file(path: str) -> str:
    """Read a file."""
    return _filesystem.get(path, f"Error: {path} not found")


async def delete_file(path: str) -> str:
    """Delete a file (irreversible)."""
    if path not in _filesystem:
        return f"Error: {path} not found"
    del _filesystem[path]
    return f"Deleted {path}"


async def write_report(path: str, content: str) -> str:
    """Write a report."""
    _filesystem[path] = content
    return f"Wrote {path}"


# ── Agent ──


async def cleanup_agent(proxy):
    """Buggy agent: ignores IMPORTANT exception."""
    files = await proxy.syscall("list_files", {"directory": "/var/log"})
    await proxy.syscall("read_file", {"path": "/var/log/cleanup_policy.txt"})
    deleted = []
    for f in files.strip().split("\n"):
        if f.endswith((".old", ".bak")) or any(f.endswith(f".{i}") for i in range(10)):
            r = await proxy.syscall("delete_file", {"path": f})
            deleted.append(str(r))
    report = "Cleanup:\n" + "\n".join(deleted)
    await proxy.syscall("write_report", {"path": "/var/log/report.txt", "content": report})
    return f"Cleaned {len(deleted)} files"


# ── Run all levels ──


async def run_all():
    from castor import Castor

    results = {}

    # ── Level 1: HITL ──
    _reset()
    kernel = Castor(
        tools=[list_files, read_file, delete_file, write_report],
        destructive=["delete_file", "write_report"],
    )
    hitl_log = []

    async def on_hitl(cp):
        tool = cp.pending_hitl.get("tool_name", "")
        args = cp.pending_hitl.get("arguments", {})
        path = args.get("path", "")
        if tool == "delete_file" and "data.db.bak" in path:
            hitl_log.append({"tool": tool, "args": args, "decision": "reject", "reason": "Contains IMPORTANT data"})
            return ("reject", "Contains IMPORTANT data — do not delete")
        hitl_log.append({"tool": tool, "args": args, "decision": "approve", "reason": ""})
        return ("approve", None)

    t0 = time.perf_counter()
    cp1 = await kernel.run_until_complete(cleanup_agent, budgets={"api": 50.0}, on_hitl=on_hitl)
    t1 = time.perf_counter() - t0

    results["level1"] = {
        "steps": [{"tool": r.request.get("tool_name"), "args": r.request.get("arguments", {}),
                    "response": str(r.response)[:120], "safe": not r.needs_review,
                    "was_hitl": r.was_hitl} for r in cp1.syscall_log],
        "hitl_log": hitl_log,
        "time": round(t1, 3),
        "backup_safe": any("data.db.bak" in k for k in _filesystem),
        "total": len(cp1.syscall_log),
        "interrupted": len(hitl_log),
    }

    # ── Level 2: Speculative ──
    _reset()
    kernel2 = Castor(
        tools=[list_files, read_file, delete_file, write_report],
        destructive=["delete_file", "write_report"],
    )
    t0 = time.perf_counter()
    cp2 = await kernel2.run(cleanup_agent, budgets={"api": 50.0}, speculative=True)
    t2 = time.perf_counter() - t0
    summary = kernel2.scan(cp2)

    results["level2"] = {
        "steps": [{"tool": r.request.get("tool_name"), "args": r.request.get("arguments", {}),
                    "response": str(r.response)[:120], "safe": not r.needs_review,
                    "flagged": r.needs_review, "reason": r.review_reason or ""} for r in cp2.syscall_log],
        "time": round(t2, 3),
        "backup_safe": any("data.db.bak" in k for k in _filesystem),
        "total": summary.total_steps,
        "auto_verified": summary.auto_verified,
        "flagged_count": summary.flagged_count,
    }

    # ── Level 3: Time-Travel ──
    bad_step = None
    for i, r in enumerate(cp2.syscall_log):
        if r.request.get("tool_name") == "delete_file" and "data.db.bak" in str(r.request.get("arguments", {})):
            bad_step = i
            break

    forked = cp2.fork(at_step=bad_step) if bad_step is not None else cp2.fork(at_step=0)
    _reset()
    for r in forked.syscall_log:
        if r.request.get("tool_name") == "delete_file":
            p = r.request.get("arguments", {}).get("path", "")
            _filesystem.pop(p, None)

    async def safe_delete(path: str) -> str:
        if "IMPORTANT" in _filesystem.get(path, ""):
            return f"BLOCKED: {path} (IMPORTANT)"
        return await delete_file(path)

    kernel3 = Castor(
        tools=[list_files, read_file, ("delete_file", safe_delete), write_report],
        destructive=["delete_file", "write_report"],
    )
    t0 = time.perf_counter()
    cp3 = await kernel3.run(cleanup_agent, checkpoint=forked, budgets={"api": 50.0}, speculative=True)
    t3 = time.perf_counter() - t0

    results["level3"] = {
        "steps": [{"tool": r.request.get("tool_name"), "args": r.request.get("arguments", {}),
                    "response": str(r.response)[:120], "safe": not r.needs_review,
                    "cached": i < (bad_step or 0)} for i, r in enumerate(cp3.syscall_log)],
        "time": round(t3, 3),
        "backup_safe": any("data.db.bak" in k for k in _filesystem),
        "total": len(cp3.syscall_log),
        "cached_steps": bad_step or 0,
        "fork_step": bad_step or 0,
    }

    return results


def generate_html(data: dict) -> str:
    d = json.dumps(data)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Castor — Security Levels</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#060614;color:#d4d4d8;font-family:-apple-system,system-ui,sans-serif;line-height:1.6}}
.header{{text-align:center;padding:50px 20px 30px;background:linear-gradient(180deg,#0f0f2e,#060614)}}
.header h1{{font-size:2.2rem;background:linear-gradient(135deg,#a78bfa,#6366f1);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.header p{{color:#71717a;margin-top:6px;font-size:0.95rem}}
.scenario{{max-width:900px;margin:0 auto;padding:20px;background:#0c0c24;border:1px solid #1a1a3a;border-radius:12px;margin-bottom:30px}}
.scenario h3{{color:#eab308;margin-bottom:8px}}
.scenario p{{color:#94a3b8;font-size:0.9rem}}
.levels{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:20px;max-width:1400px;margin:0 auto;padding:0 20px 40px}}
@media(max-width:1000px){{.levels{{grid-template-columns:1fr}}}}
.level{{background:#0c0c24;border:1px solid #1a1a3a;border-radius:14px;overflow:hidden}}
.level-head{{padding:18px 20px;border-bottom:1px solid #1a1a3a;display:flex;justify-content:space-between;align-items:center}}
.level-head h2{{font-size:1rem}}
.badge{{font-size:0.65rem;padding:3px 10px;border-radius:99px;font-weight:700}}
.badge-green{{background:#052e16;color:#22c55e;border:1px solid #16a34a}}
.badge-yellow{{background:#422006;color:#eab308;border:1px solid #ca8a04}}
.badge-blue{{background:#0c1e3a;color:#60a5fa;border:1px solid #3b82f6}}
.level-stats{{display:flex;gap:12px;padding:14px 20px;border-bottom:1px solid #1a1a3a;flex-wrap:wrap}}
.stat{{text-align:center;flex:1;min-width:60px}}
.stat-val{{font-size:1.3rem;font-weight:700}}
.stat-label{{font-size:0.6rem;color:#71717a;text-transform:uppercase;letter-spacing:0.5px}}
.steps{{padding:12px 16px;max-height:400px;overflow-y:auto}}
.step{{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:8px;margin-bottom:4px;font-size:0.8rem;transition:background 0.15s;cursor:default}}
.step:hover{{background:#12122a}}
.step-dot{{width:10px;height:10px;border-radius:50%;flex-shrink:0}}
.dot-safe{{background:#22c55e}}
.dot-flagged{{background:#ef4444}}
.dot-reject{{background:#ef4444;border:2px solid #fca5a5}}
.dot-approve{{background:#22c55e;border:2px solid #86efac}}
.dot-cached{{background:#3b82f6;opacity:0.5}}
.dot-blocked{{background:#eab308}}
.step-tool{{font-weight:600;color:#c4b5fd;min-width:80px}}
.step-detail{{color:#71717a;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.step-badge{{font-size:0.6rem;padding:1px 6px;border-radius:4px;font-weight:600;flex-shrink:0}}
.sb-review{{background:#3b1f1f;color:#fca5a5}}
.sb-safe{{background:#052e16;color:#86efac}}
.sb-hitl{{background:#422006;color:#fde68a}}
.sb-cached{{background:#0c1e3a;color:#93c5fd}}
.sb-blocked{{background:#422006;color:#fde68a}}
.result{{padding:16px 20px;border-top:1px solid #1a1a3a;text-align:center}}
.result-safe{{color:#22c55e;font-weight:700}}
.result-danger{{color:#ef4444;font-weight:700}}
.footer{{text-align:center;padding:40px 20px;color:#3f3f46;font-size:0.8rem}}
.footer a{{color:#6366f1;text-decoration:none}}
.footer .quote{{font-size:1.1rem;color:#a78bfa;font-style:italic;margin-bottom:10px}}
</style></head><body>
<div class="header">
<h1>Castor — Secure Execution Layer</h1>
<p>Same buggy agent. Same dangerous task. Three security levels.</p>
</div>
<div class="scenario">
<h3>⚠️ Scenario</h3>
<p><strong>Task:</strong> Agent cleans up old log files in /var/log<br>
<strong>Bug:</strong> Agent ignores the "IMPORTANT" exception in cleanup policy<br>
<strong>Risk:</strong> Production database backup (<code>data.db.bak</code>) may be deleted</p>
</div>
<div class="levels" id="levels"></div>
<div class="footer">
<div class="quote">"Your agent can think freely. It just can't act unsafely."</div>
<div><a href="https://github.com/substratum-labs/castor">github.com/substratum-labs/castor</a></div>
</div>
<script>
const D={d};
function renderLevel(id,title,badge,badgeClass,stats,steps,resultHtml){{
const el=document.createElement('div');el.className='level';
let statsHtml=stats.map(s=>`<div class="stat"><div class="stat-val">${{s.val}}</div><div class="stat-label">${{s.label}}</div></div>`).join('');
let stepsHtml=steps.map(s=>{{
let dotClass='dot-safe';let badgeText='safe';let badgeClass2='sb-safe';
if(s.type==='flagged'){{dotClass='dot-flagged';badgeText='needs review';badgeClass2='sb-review';}}
if(s.type==='reject'){{dotClass='dot-reject';badgeText='REJECTED';badgeClass2='sb-hitl';}}
if(s.type==='approve'){{dotClass='dot-approve';badgeText='approved';badgeClass2='sb-hitl';}}
if(s.type==='cached'){{dotClass='dot-cached';badgeText='cached';badgeClass2='sb-cached';}}
if(s.type==='blocked'){{dotClass='dot-blocked';badgeText='BLOCKED';badgeClass2='sb-blocked';}}
return `<div class="step"><div class="step-dot ${{dotClass}}"></div><span class="step-tool">${{s.tool}}</span><span class="step-detail">${{s.detail}}</span><span class="step-badge ${{badgeClass2}}">${{badgeText}}</span></div>`;
}}).join('');
el.innerHTML=`<div class="level-head"><h2>${{title}}</h2><span class="badge ${{badgeClass}}">${{badge}}</span></div><div class="level-stats">${{statsHtml}}</div><div class="steps">${{stepsHtml}}</div><div class="result">${{resultHtml}}</div>`;
document.getElementById('levels').appendChild(el);
}}
// Level 1
const l1=D.level1;
renderLevel('l1','Level 1: HITL','Every op approved','badge-green',
[{{val:l1.total,label:'Steps'}},{{val:l1.interrupted,label:'Interruptions'}},{{val:l1.time+'s',label:'Time'}}],
l1.steps.map((s,i)=>{{
let type='safe';let detail=JSON.stringify(s.args).substring(0,60);
if(s.was_hitl){{
const h=l1.hitl_log.find(x=>x.tool===s.tool&&JSON.stringify(x.args)===JSON.stringify(s.args));
type=h&&h.decision==='reject'?'reject':'approve';
detail=h&&h.decision==='reject'?h.reason:detail;
}}
return{{tool:s.tool,detail,type}};
}}),
l1.backup_safe?'<span class="result-safe">🛡️ Production backup: SAFE ✅</span>':'<span class="result-danger">❌ Backup DELETED</span>'
);
// Level 2
const l2=D.level2;
renderLevel('l2','Level 2: Speculative','Zero interruptions','badge-yellow',
[{{val:l2.total,label:'Steps'}},{{val:l2.auto_verified,label:'Auto-verified'}},{{val:l2.flagged_count,label:'Flagged'}},{{val:l2.time+'s',label:'Time'}}],
l2.steps.map(s=>{{
let type=s.flagged?'flagged':'safe';
let detail=JSON.stringify(s.args).substring(0,60);
return{{tool:s.tool,detail,type}};
}}),
l2.backup_safe?'<span class="result-safe">🛡️ Production backup: SAFE ✅</span>':'<span class="result-danger">❌ Backup DELETED — detected in review</span>'
);
// Level 3
const l3=D.level3;
renderLevel('l3','Level 3: Time-Travel','Rewind & fix','badge-blue',
[{{val:l3.total,label:'Steps'}},{{val:l3.cached_steps,label:'Cached (free)'}},{{val:l3.fork_step,label:'Fork at'}},{{val:l3.time+'s',label:'Time'}}],
l3.steps.map((s,i)=>{{
let type=s.cached?'cached':(s.safe?'safe':'flagged');
if(s.response&&s.response.includes('BLOCKED'))type='blocked';
let detail=JSON.stringify(s.args).substring(0,60);
return{{tool:s.tool,detail,type}};
}}),
l3.backup_safe?'<span class="result-safe">🛡️ Production backup: SAFE ✅ (recovered)</span>':'<span class="result-danger">❌ Backup DELETED</span>'
);
</script></body></html>"""


async def main():
    print("⏳ Running three security levels...")
    data = await run_all()

    out = Path("/tmp/castor_security_levels.html")
    out.write_text(generate_html(data))
    print(f"✅ Saved to {out}")
    print("🌐 Opening browser...")

    if sys.platform == "darwin":
        subprocess.run(["open", str(out)])
    elif sys.platform == "linux":
        subprocess.run(["xdg-open", str(out)])


if __name__ == "__main__":
    asyncio.run(main())
