import os
path = '../tiphys/src/tiphys/panel/template.py'
with open(path, 'r') as f:
    content = f.read()

# 1. Add CSS
css_add = '''/* === SPLIT VIEW === */
.timeline-container { display: flex; flex-direction: column; }
.timeline-split { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; position: relative; margin-top: 10px; }
.timeline-split::after { content: '⑃'; position: absolute; left: 50%; top: -25px; transform: translateX(-50%); 
    font-size: 1.2rem; color: var(--purple); background: var(--bg); padding: 0 10px; z-index: 10; }
.timeline-col { display: flex; flex-direction: column; }
.timeline-col-header { font-size: 0.7rem; color: var(--text-dim); text-transform: uppercase; 
    margin-bottom: 10px; border-bottom: 1px solid var(--border); padding-bottom: 4px; }
.timeline-col .timeline { padding-left: 20px; }
'''
if '/* === SPLIT VIEW === */' not in content:
    content = content.replace('/* === TIMELINE === */', css_add + '/* === TIMELINE === */')

# 2. Wrap timeline in timeline-root
if '<div id="timeline-root">' not in content:
    content = content.replace('<div class="timeline" id="timeline">', '<div id="timeline-root"><div class="timeline" id="timeline">')
    # Close it before result
    content = content.replace('            <div class="result"', '            </div>\n            <div class="result"')

# 3. Replace JS functions
import re

# Replace showTaskDetail
new_show = '''async function showTaskDetail(t) {
    const root = document.getElementById('timeline-root');
    root.innerHTML = '<div class="timeline" id="timeline"></div>';
    const currentTl = document.getElementById('timeline');
    
    document.getElementById('result').classList.remove('active');
    document.getElementById('hitl').classList.remove('active');
    document.getElementById('stats').style.display = 'grid';
    document.getElementById('tl-title').textContent = t.name;

    updateBudget(t.budget_used, t.budget_total);

    if (t.parent_pid) {
        try {
            const resp = await fetch((window._panelApiPrefix||"/api")+"/task/" + t.parent_pid);
            const parent = await resp.json();
            
            root.innerHTML = `
                <div class="timeline-split">
                    <div class="timeline-col">
                        <div class="timeline-col-header">Original: ${esc(parent.name)}</div>
                        <div class="timeline" id="tl-parent"></div>
                    </div>
                    <div class="timeline-col">
                        <div class="timeline-col-header">Fork: ${esc(t.name)}</div>
                        <div class="timeline" id="tl-fork"></div>
                    </div>
                </div>
            `;
            
            const parentTl = document.getElementById('tl-parent');
            const forkTl = document.getElementById('tl-fork');
            
            const parentSteps = parent.events.slice(0, t.fork_step);
            parentSteps.forEach(d => parentTl.appendChild(createStepEl(d)));
            t.events.forEach(d => forkTl.appendChild(createStepEl(d)));
            
            updateStatsFrom([parentTl, forkTl]);
        } catch (e) {
            console.error("Failed to fetch parent task", e);
            t.events.forEach(d => currentTl.appendChild(createStepEl(d)));
        }
    } else {
        if (t.events.length === 0) {
            currentTl.innerHTML = '<div class="empty" id="empty"><div class="empty-icon">⏳</div><div>Waiting...</div></div>';
        } else {
            t.events.forEach(d => currentTl.appendChild(createStepEl(d)));
            updateStatsFrom([currentTl]);
        }
    }

    if (t.result) {
        showResult({result: t.result, total_steps: t.steps, elapsed: t.elapsed, budget_used: t.budget_used});
    }
}'''

content = re.sub(r'function showTaskDetail\(t\) \{.*?^\}', new_show, content, flags=re.MULTILINE|re.DOTALL)

# Replace makeStep with createStepEl and updateStatsFrom
new_helpers = '''function createStepEl(d) {
    const div = document.createElement('div');
    let cls = 'step';
    if (['write_file','create_tool'].includes(d.tool)) cls += ' destructive';
    div.className = cls; div.id = 'step-'+d.pid+'-'+d.index;
    div.onclick = () => div.classList.toggle('open');
    const pre = esc(String(d.response||'').substring(0,120));
    div.innerHTML = `<div class="step-row">
        <div class="step-left"><span class="step-icon">${icon(d.tool)}</span>
        <span class="step-num">${d.index}</span>
        <span class="step-tool mono">${esc(d.tool)}</span></div>
        <div class="step-right">
        <span class="step-cost mono">${(d.budget_used||0).toFixed(1)}cr</span>
        <span class="step-time mono">${(d.elapsed||0).toFixed(1)}s</span>
        <span class="step-ok">✓</span></div>
        <div class="step-actions">
            <button class="act-btn" onclick="event.stopPropagation();rollbackToStep('${d.pid}', ${d.index})">↩ rollback</button>
            <button class="act-btn" onclick="event.stopPropagation();forkFromStep('${d.pid}', ${d.index})">⑃ fork</button>
        </div>
    </div><div class="step-preview">→ ${pre}</div>
    <div class="step-detail mono">${esc(String(d.response||''))}</div>`;
    return div;
}

function updateStatsFrom(tls) {
    let count=0, th=0, wr=0;
    tls.forEach(t => {
        if(!t) return;
        const steps = t.querySelectorAll('.step');
        count += steps.length;
        steps.forEach(s => {
            const tool = s.querySelector('.step-tool')?.textContent||'';
            if (tool==='think') th++; if (tool==='write_file') wr++;
        });
    });
    document.getElementById('s-steps').textContent = count;
    document.getElementById('s-thinks').textContent = th;
    document.getElementById('s-writes').textContent = wr;
}'''

if 'function createStepEl' not in content:
    content = re.sub(r'function makeStep\(d\) \{.*?^\}', new_helpers, content, flags=re.MULTILINE|re.DOTALL)

# Update other functions
content = content.replace('updateStats();', 'updateStatsFrom([document.getElementById("tl-parent"), document.getElementById("tl-fork"), document.getElementById("timeline")]);')

if 'rollbackToStep' not in content:
    content = content.replace('function rollbackTo(step) {', "function rollbackTo(step) { rollbackToStep(activePid, step); }\nfunction rollbackToStep(pid, step) {")
    content = content.replace('function forkFrom(step) {', "function forkFrom(step) { forkFromStep(activePid, step); }\nfunction forkFromStep(pid, step) {")
    content = content.replace("fetch((window._panelApiPrefix||'/api')+'/task/' + activePid + '/rollback/' + step", "fetch((window._panelApiPrefix||'/api')+'/task/' + pid + '/rollback/' + step")
    content = content.replace("fetch((window._panelApiPrefix||'/api')+'/task/' + activePid + '/fork/' + step", "fetch((window._panelApiPrefix||'/api')+'/task/' + pid + '/fork/' + step")

# Fix step placement in WS handlers
content = content.replace("tl.appendChild(div);", "const targetTl = document.getElementById('tl-fork') || document.getElementById('timeline'); if(targetTl) targetTl.appendChild(div);")
content = content.replace("tl.innerHTML = '';", "const root = document.getElementById('timeline-root'); if(root) root.innerHTML = '';")

# Fix addCompleted
if 'function addCompleted(d) {\n    const targetTl' not in content:
    content = content.replace("function addCompleted(d) {", "function addCompleted(d) {\n    const targetTl = document.getElementById('tl-fork') || document.getElementById('timeline');")
    content = content.replace("makeStep(d);", "if(targetTl) targetTl.appendChild(createStepEl(d));")

# Fix completeStep
new_complete = '''function completeStep(d) {
    const el = document.getElementById('step-'+d.pid+'-'+d.index);
    if (el) el.remove();
    const targetTl = document.getElementById('tl-fork') || document.getElementById('timeline');
    if (targetTl) targetTl.appendChild(createStepEl(d));
}'''
content = re.sub(r'function completeStep\(d\) \{.*?^\}', new_complete, content, flags=re.MULTILINE|re.DOTALL)

# Fix addRunning
new_running = '''function addRunning(d) {
    hideEmpty();
    const div = document.createElement('div');
    div.className = 'step running'; div.id = 'step-'+d.pid+'-'+d.index;
    div.innerHTML = `<div class="step-row">
        <div class="step-left"><span class="step-icon">${icon(d.tool)}</span>
        <span class="step-num">${d.index}</span>
        <span class="step-tool mono">${esc(d.tool)}</span></div>
        <div class="step-right"><span class="step-spin">◌</span></div>
    </div><div class="step-preview">Running...</div>`;
    const targetTl = document.getElementById('tl-fork') || document.getElementById('timeline');
    if (targetTl) targetTl.appendChild(div);
    div.scrollIntoView({behavior:'smooth',block:'nearest'});
}'''
content = re.sub(r'function addRunning\(d\) \{.*?^\}', new_running, content, flags=re.MULTILINE|re.DOTALL)

with open(path, 'w') as f:
    f.write(content)
