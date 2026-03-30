import sys

path = '../tiphys/src/tiphys/panel/template.py'
with open(path, 'r') as f:
    content = f.read()

# 1. Add CSS for Split View
old_css = '/* === TIMELINE === */'
split_view_css = """/* === SPLIT VIEW === */
.timeline-container { display: flex; flex-direction: column; }
.timeline-split { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; position: relative; }
.timeline-split::after { content: '⑃'; position: absolute; left: 50%; top: -25px; transform: translateX(-50%); 
    font-size: 1.2rem; color: var(--purple); background: var(--bg); padding: 0 10px; z-index: 10; }
.timeline-col { display: flex; flex-direction: column; }
.timeline-col-header { font-size: 0.7rem; color: var(--text-dim); text-transform: uppercase; 
    margin-bottom: 10px; border-bottom: 1px solid var(--border); padding-bottom: 4px; }

/* Adjust original timeline styles to work in columns */
.timeline-col .timeline { padding-left: 20px; }

"""
content = content.replace(old_css, split_view_css + old_css)

# 2. Add placeholder for multi-timeline
old_timeline_div = '<div class="timeline" id="timeline">'
new_timeline_div = '<div id="timeline-root"><div class="timeline" id="timeline">'
content = content.replace(old_timeline_div, new_timeline_div)
content = content.replace('</div>\n\n            <div class="result"', '</div></div>\n\n            <div class="result"')

# 3. Update showTaskDetail JS function
old_showTaskDetail = """function showTaskDetail(t) {
    // Clear timeline
    tl.innerHTML = '';
    document.getElementById('result').classList.remove('active');
    document.getElementById('hitl').classList.remove('active');
    document.getElementById('stats').style.display = 'grid';

    // Update budget
    updateBudget(t.budget_used, t.budget_total);

    // Render events
    if (t.events.length === 0) {
        tl.innerHTML = '<div class="empty" id="empty"><div class="empty-icon">⏳</div><div>Waiting...</div></div>';
    } else {
        t.events.forEach(addCompleted);
        updateStats();
    }

    document.getElementById('tl-title').textContent = t.name;

    if (t.result) {
        showResult({result: t.result, total_steps: t.steps, elapsed: t.elapsed, budget_used: t.budget_used});
    }
}"""

new_showTaskDetail = """async function showTaskDetail(t) {
    const root = document.getElementById('timeline-root');
    root.innerHTML = '<div class="timeline" id="timeline"></div>';
    const currentTl = document.getElementById('timeline');
    
    document.getElementById('result').classList.remove('active');
    document.getElementById('hitl').classList.remove('active');
    document.getElementById('stats').style.display = 'grid';
    document.getElementById('tl-title').textContent = t.name;

    updateBudget(t.budget_used, t.budget_total);

    if (t.parent_pid) {
        // Fetch parent to get full history for split view
        try {
            const resp = await fetch((window._panelApiPrefix||'/api')+'/task/' + t.parent_pid);
            const parent = await resp.json();
            
            // Re-render as split view
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
            
            // Parent: only steps up to fork_step
            const parentSteps = parent.events.slice(0, t.fork_step);
            parentSteps.forEach(d => parentTl.appendChild(createStepEl(d)));
            
            // Fork: steps from forked checkpoint
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
}"""

content = content.replace(old_showTaskDetail, new_showTaskDetail)

# 4. Extract step creation to a helper to avoid duplication
old_makeStep = """function makeStep(d) {
    const div = document.createElement('div');
    let cls = 'step';
    if (['write_file','create_tool'].includes(d.tool)) cls += ' destructive';
    div.className = cls; div.id = 'step-'+d.index;
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
            <button class="act-btn" onclick="event.stopPropagation();rollbackTo(${d.index})">↩ rollback</button>
            <button class="act-btn" onclick="event.stopPropagation();forkFrom(${d.index})">⑃ fork</button>
        </div>
    </div><div class="step-preview">→ ${pre}</div>
    <div class="step-detail mono">${esc(String(d.response||''))}</div>`;
    tl.appendChild(div);
}"""

new_helpers = """function createStepEl(d) {
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
}"""

content = content.replace(old_makeStep, new_helpers)

# 5. Fix updateStats references
content = content.replace('updateStats();', 'updateStatsFrom([document.getElementById("tl-parent"), document.getElementById("tl-fork")].filter(Boolean));')

# 6. Update handlers to be PID aware
content = content.replace('function rollbackTo(step) {', "function rollbackTo(step) { rollbackToStep(activePid, step); }\\nfunction rollbackToStep(pid, step) {")
content = content.replace('function forkFrom(step) {', "function forkFrom(step) { forkFromStep(activePid, step); }\\nfunction forkFromStep(pid, step) {")
content = content.replace('/task/\' + activePid + \'/rollback/\' + step', "/task/' + pid + '/rollback/' + step")
content = content.replace('/task/\' + activePid + \'/fork/\' + step', "/task/' + pid + '/fork/' + step")

# 7. Update addRunning, addCompleted, completeStep to use the new root
content = content.replace("tl.appendChild(div);", "const targetTl = document.getElementById('tl-fork') || document.getElementById('timeline'); if(targetTl) targetTl.appendChild(div);")
content = content.replace("tl.appendChild(div);", "const targetTl = document.getElementById('tl-fork') || document.getElementById('timeline'); if(targetTl) targetTl.appendChild(div);") # second occurrence in completeStep/makeStep (but I replaced makeStep)

# Actually I need to fix addRunning to use createStepEl logic or similar
old_addRunning = """function addRunning(d) {
    hideEmpty();
    const div = document.createElement('div');
    div.className = 'step running'; div.id = 'step-'+d.index;
    div.innerHTML = `<div class="step-row">
        <div class="step-left"><span class="step-icon">${icon(d.tool)}</span>
        <span class="step-num">${d.index}</span>
        <span class="step-tool mono">${esc(d.tool)}</span></div>
        <div class="step-right"><span class="step-spin">◌</span></div>
    </div><div class="step-preview">Running...</div>`;
    tl.appendChild(div);
    div.scrollIntoView({behavior:'smooth',block:'nearest'});
}"""

new_addRunning = """function addRunning(d) {
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
}"""
content = content.replace(old_addRunning, new_addRunning)

content = content.replace("tl.innerHTML = '';", "const root = document.getElementById('timeline-root'); if(root) root.innerHTML = '';")
content = content.replace("tl.innerHTML = '<div", "const currentTl = document.getElementById('timeline'); if(currentTl) currentTl.innerHTML = '<div")

# Fix completeStep to use the correct target
old_completeStep = """function completeStep(d) {
    const el = document.getElementById('step-'+d.index);
    if (el) el.remove();
    makeStep(d);
}"""

new_completeStep = """function completeStep(d) {
    const el = document.getElementById('step-'+d.pid+'-'+d.index);
    if (el) el.remove();
    const targetTl = document.getElementById('tl-fork') || document.getElementById('timeline');
    if (targetTl) targetTl.appendChild(createStepEl(d));
}"""
content = content.replace(old_completeStep, new_completeStep)

# Fix addCompleted
content = content.replace("function addCompleted(d) {", "function addCompleted(d) {\n    const targetTl = document.getElementById('tl-fork') || document.getElementById('timeline');")
content = content.replace("makeStep(d);", "if(targetTl) targetTl.appendChild(createStepEl(d));")

with open(path, 'w') as f:
    f.write(content)
