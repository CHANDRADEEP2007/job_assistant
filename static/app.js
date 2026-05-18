document.addEventListener('DOMContentLoaded', () => {

    // ===== TAB LOGIC =====
    const tabBtns = document.querySelectorAll('.tab-btn');
    const tabContents = document.querySelectorAll('.tab-content');
    tabBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            tabBtns.forEach(b => b.classList.remove('active'));
            tabContents.forEach(c => c.classList.remove('active'));
            btn.classList.add('active');
            document.getElementById(btn.dataset.target).classList.add('active');
            if (btn.dataset.target === 'tab-applications') fetchAllApps();
            if (btn.dataset.target === 'tab-applied') fetchApplied();
            if (btn.dataset.target === 'tab-ledger') fetchLedger();
            if (btn.dataset.target === 'tab-settings') fetchSettings();
            if (btn.dataset.target === 'tab-credentials') fetchCreds();
        });
    });

    // ===== CHART.JS =====
    const ctx = document.getElementById('funnelChart').getContext('2d');
    const funnelChart = new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels: ['Skipped', 'Eligible', 'Applied'],
            datasets: [{
                data: [0, 0, 0],
                backgroundColor: ['#ff2a55', '#00f0ff', '#00ff88'],
                borderWidth: 0
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { position: 'right', labels: { color: '#e2e8f0' } } }
        }
    });

    // ===== DOM REFS =====
    let eventSource = null;
    const statusDot = document.getElementById('status-dot');
    const statusText = document.getElementById('status-text');
    const modeBadge = document.getElementById('mode-badge');
    const modeText = document.getElementById('current-mode-text');
    const startBtn = document.getElementById('start-btn');
    const stopBtn = document.getElementById('stop-btn');
    const clearBtn = document.getElementById('clear-btn');
    const terminal = document.getElementById('terminal');
    const trackingTable = document.getElementById('tracking-table').querySelector('tbody');

    // Review gate
    const reviewOverlay = document.getElementById('review-overlay');
    const approveBtn = document.getElementById('approve-btn');
    const rejectBtn = document.getElementById('reject-btn');
    let reviewTimerInterval = null;

    // Ledger toast
    const ledgerToast = document.getElementById('ledger-toast');
    let ledgerTimerInterval = null;
    let currentLedgerQuestion = null;

    // ===== HELPERS =====
    function truncate(text, maxLength = 48) {
        if (!text) return 'Idle';
        return text.length > maxLength ? `${text.slice(0, maxLength - 3)}...` : text;
    }

    function parseJsonSafely(value) {
        try { return JSON.parse(value); } catch (e) { return null; }
    }

    function formatTimer(seconds) {
        const m = Math.floor(seconds / 60);
        const s = seconds % 60;
        return `${m}:${String(s).padStart(2, '0')}`;
    }

    function escapeHtml(text) {
        if (text == null || text === undefined) return '';
        return String(text)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function hideApplyPausedBanner() {
        const el = document.getElementById('apply-paused-banner');
        if (el) el.style.display = 'none';
        const img = document.getElementById('pause-screenshot');
        if (img) {
            img.style.display = 'none';
            img.removeAttribute('src');
        }
    }

    function showApplyPausedBanner(payload) {
        const banner = document.getElementById('apply-paused-banner');
        if (!banner || !payload) return;
        document.getElementById('pause-company').textContent = payload.company || '';
        document.getElementById('pause-role').textContent = payload.role || '';
        document.getElementById('pause-detail').textContent = payload.message || 'Complete required fields in the browser window, then click Continue.';
        const fields = (payload.pending_fields || []).join(', ');
        document.getElementById('pause-fields').textContent = fields ? `Pending: ${fields}` : '';
        const img = document.getElementById('pause-screenshot');
        if (payload.screenshot_url) {
            img.src = payload.screenshot_url + (payload.screenshot_url.includes('?') ? '&' : '?') + 't=' + Date.now();
            img.style.display = 'block';
            img.onerror = () => { img.style.display = 'none'; };
        } else {
            img.style.display = 'none';
        }
        banner.style.display = 'flex';
    }

    // ===== FIX #1: PRE-FLIGHT CHECK =====
    async function runPreflight() {
        try {
            const res = await fetch('/api/preflight');
            const data = await res.json();
            renderPreflightChecks(data.checks, data.ready);
        } catch (e) {
            console.error('Preflight error', e);
        }
    }

    function renderPreflightChecks(checks, ready) {
        const grid = document.getElementById('preflight-checks');
        grid.innerHTML = checks.map(c => `
            <div class="preflight-item ${c.ok ? 'ok' : 'fail'}">
                <span class="preflight-icon">${c.ok ? 'OK' : 'X'}</span>
                <span class="preflight-name">${c.name}</span>
                ${!c.ok ? `<span class="preflight-fix">${c.fix}</span>` : ''}
            </div>
        `).join('');

        const hint = document.getElementById('preflight-hint');
        const startHint = document.getElementById('start-hint');

        if (ready) {
            startBtn.disabled = false;
            hint.style.display = 'none';
            startHint.style.display = 'none';
            document.getElementById('preflight-section').classList.remove('has-failures');
        } else {
            startBtn.disabled = true;
            hint.textContent = 'Fix the failed items above before starting the agent.';
            hint.style.display = 'block';
            startHint.style.display = 'block';
            document.getElementById('preflight-section').classList.add('has-failures');
        }
    }

    // ===== TRACKING TABLE ROW =====
    function addTrackingRow(payload) {
        const status = (payload.status || '').toLowerCase();
        let statusClass = '';
        if (status === 'applied') statusClass = 'status-applied';
        else if (status === 'eligible') statusClass = 'status-eligible';
        else if (status.includes('error')) statusClass = 'status-error';
        else statusClass = 'status-skipped';

        const tr = document.createElement('tr');
        tr.innerHTML = `<td>${payload.job}</td><td class="${statusClass}">${payload.status}</td><td>${payload.score || 0}</td><td>${payload.reason}</td>`;
        trackingTable.insertBefore(tr, trackingTable.firstChild);
    }

    // ===== KPI UPDATER =====
    function updateKPIs(data) {
        document.getElementById('kpi-evaluated').textContent = data.evaluated || 0;
        document.getElementById('kpi-eligible').textContent = data.eligible || 0;
        document.getElementById('kpi-applied').textContent = data.applied || 0;
        document.getElementById('kpi-skipped').textContent = data.skipped || 0;
        document.getElementById('kpi-errors').textContent = data.errors || 0;
        document.getElementById('kpi-current-job').textContent = truncate(data.current_job);

        funnelChart.data.datasets[0].data = [data.skipped || 0, data.eligible || 0, data.applied || 0];
        funnelChart.update();

        if (data.running) {
            statusDot.className = 'dot online';
            statusText.textContent = 'Running';
            startBtn.disabled = true;
            stopBtn.disabled = false;
        } else {
            statusDot.className = 'dot offline';
            statusText.textContent = 'Idle';
            stopBtn.disabled = true;
            // Re-check preflight when agent stops to re-enable start if appropriate
            runPreflight();
        }

        const isSim = (data.mode === 'simulation');
        modeBadge.textContent = isSim ? 'Simulation' : 'Live';
        modeBadge.className = isSim ? 'badge warning' : 'badge success';
        modeText.textContent = isSim ? 'simulation' : 'live';
    }

    async function fetchSummary() {
        try {
            const res = await fetch('/api/dashboard/summary');
            const data = await res.json();
            updateKPIs(data);
        } catch (e) { console.error('Summary fetch error', e); }
    }

    // ===== RESUME STATUS =====
    const resumeBadge = document.getElementById('resume-status-badge');
    async function checkResumeStatus() {
        try {
            const res = await fetch('/api/resume_status');
            const data = await res.json();
            resumeBadge.textContent = data.exists ? 'Resume Active' : 'No Resume Found';
            resumeBadge.className = data.exists ? 'badge success' : 'badge danger';
        } catch (e) { console.error(e); }
    }

    // ===== TERMINAL =====
    function appendLog(message) {
        const div = document.createElement('div');
        div.className = 'log-entry';
        if (message.includes('[Agent]')) div.classList.add('agent');
        else if (message.toLowerCase().includes('error') || message.toLowerCase().includes('blocked')) div.classList.add('error');
        else if (message.toLowerCase().includes('warning')) div.classList.add('warning');
        else div.classList.add('system');
        div.textContent = message;
        terminal.appendChild(div);
        terminal.scrollTop = terminal.scrollHeight;
    }

    function handleTerminalMessage(message) {
        const parsed = parseJsonSafely(message);
        if (parsed && parsed.type === 'job_decision') {
            addTrackingRow(parsed);
            fetchSummary();
            return;
        }
        appendLog(message);
    }

    // ===== FIX #3: REVIEW GATE =====
    function showReviewCard(payload) {
        document.getElementById('rev-company').textContent = payload.company;
        document.getElementById('rev-role').textContent = payload.role;
        document.getElementById('rev-score').textContent = payload.match_score;

        const formDataEl = document.getElementById('rev-form-data');
        formDataEl.innerHTML = Object.entries(payload.form_data || {})
            .map(([k, v]) => `<div class="form-data-row"><strong>${k}:</strong> ${v}</div>`)
            .join('');

        reviewOverlay.style.display = 'flex';

        // Countdown timer
        let remaining = payload.timeout_seconds || 300;
        document.getElementById('review-timer').textContent = formatTimer(remaining);
        document.getElementById('review-timer-hint').textContent = formatTimer(remaining);

        if (reviewTimerInterval) clearInterval(reviewTimerInterval);
        reviewTimerInterval = setInterval(() => {
            remaining--;
            const fmt = formatTimer(remaining);
            document.getElementById('review-timer').textContent = fmt;
            document.getElementById('review-timer-hint').textContent = fmt;
            if (remaining <= 0) {
                clearInterval(reviewTimerInterval);
                hideReviewCard();
                appendLog(`[Review Gate] Timed out for ${payload.company} - skipped because approval was not given.`);
            }
        }, 1000);
    }

    function hideReviewCard() {
        reviewOverlay.style.display = 'none';
        if (reviewTimerInterval) clearInterval(reviewTimerInterval);
    }

    approveBtn.addEventListener('click', async () => {
        hideReviewCard();
        await fetch('/api/review/respond', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ approved: true })
        });
        appendLog('[Review Gate] Application approved by user.');
    });

    rejectBtn.addEventListener('click', async () => {
        hideReviewCard();
        await fetch('/api/review/respond', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ approved: false })
        });
        appendLog('[Review Gate] Application rejected by user.');
    });

    // ===== FIX #3 & #6: LEDGER QUESTION TOAST =====
    function showLedgerToast(payload) {
        currentLedgerQuestion = payload.question;
        document.getElementById('ledger-question-text').textContent = payload.question;
        document.getElementById('ledger-answer-input').value = '';
        ledgerToast.style.display = 'block';

        let remaining = payload.timeout_seconds || 120;
        document.getElementById('ledger-timer').textContent = formatTimer(remaining);
        if (ledgerTimerInterval) clearInterval(ledgerTimerInterval);
        ledgerTimerInterval = setInterval(() => {
            remaining--;
            document.getElementById('ledger-timer').textContent = formatTimer(remaining);
            if (remaining <= 0) {
                clearInterval(ledgerTimerInterval);
                hideLedgerToast();
                appendLog('[Ledger] Timeout - AI generated a fallback answer.');
            }
        }, 1000);
    }

    function hideLedgerToast() {
        ledgerToast.style.display = 'none';
        if (ledgerTimerInterval) clearInterval(ledgerTimerInterval);
        currentLedgerQuestion = null;
    }

    document.getElementById('ledger-submit-btn').addEventListener('click', async () => {
        const answer = document.getElementById('ledger-answer-input').value.trim();
        if (!answer || !currentLedgerQuestion) return;
        const question = currentLedgerQuestion;
        hideLedgerToast();
        const res = await fetch('/api/ledger/answer', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ question, answer })
        });
        const data = await res.json();
        if (data.success) {
            appendLog(`[Ledger] Answer saved: "${answer.slice(0, 60)}"`);
        } else {
            appendLog(`[Ledger] Save failed: ${data.message || 'Unknown error'}`);
        }
    });

    document.getElementById('ledger-skip-btn').addEventListener('click', () => {
        hideLedgerToast();
        appendLog('[Ledger] Skipped - AI will generate a fallback answer.');
    });

    // ===== FIX #4: BOT CHALLENGE =====
    document.getElementById('resume-btn').addEventListener('click', async () => {
        document.getElementById('bot-challenge-banner').style.display = 'none';
        const res = await fetch('/api/agent/resume', { method: 'POST' });
        const data = await res.json();
        if (data.success) appendLog('[Agent] Agent resumed after bot challenge.');
        else appendLog(`[Agent] Resume failed: ${data.message}`);
    });

    document.getElementById('continue-pause-btn').addEventListener('click', async () => {
        const res = await fetch('/api/agent/continue_after_pause', { method: 'POST' });
        const data = await res.json();
        if (data.success) {
            appendLog('[Application] Continue signal sent — agent will retry the form.');
        } else {
            appendLog(`[Application] Continue failed: ${data.message || 'unknown'}`);
        }
    });

    // ===== SSE STREAM =====
    function handleStreamPayload(eventType, rawData) {
        const parsed = parseJsonSafely(rawData);

        if (eventType === 'terminal_log') {
            const msg = parsed && typeof parsed.message === 'string' ? parsed.message : rawData;
            handleTerminalMessage(msg);
            return;
        }

        if (eventType === 'agent_status' || eventType === 'settings_updated') {
            fetchSummary();
            return;
        }

        // Fix #2: Session expired
        if (eventType === 'session_expired') {
            const banner = document.getElementById('session-expired-banner');
            document.getElementById('session-msg').textContent = parsed?.message || 'Session expired.';
            banner.style.display = 'flex';
            appendLog(`[Agent] Warning: ${parsed?.message}`);
            return;
        }

        // Fix #4: Bot challenge
        if (eventType === 'bot_challenge') {
            document.getElementById('bot-challenge-banner').style.display = 'flex';
            appendLog(`[Agent] Bot challenge detected at: ${parsed?.url}`);
            return;
        }

        // Fix #3: Review request
        if (eventType === 'review_request') {
            if (parsed) showReviewCard(parsed);
            return;
        }

        if (eventType === 'review_resolved' || eventType === 'review_expired') {
            hideReviewCard();
            if (parsed?.auto_approved) {
                appendLog(`[Review Gate] Window closed for ${parsed.company} - auto-approved.`);
            } else if (parsed) {
                appendLog(`[Review Gate] Window closed for ${parsed.company} - skipped.`);
            }
            return;
        }

        // Watchdog: Agent crash
        if (eventType === 'agent_crash') {
            document.getElementById('agent-crash-banner').style.display = 'flex';
            document.getElementById('crash-msg').textContent = parsed?.error || 'Fatal Error';
            appendLog(`AGENT CRASH: ${parsed?.message}`);
            fetchSummary(); // Update UI state
            return;
        }

        // Fix #3 & #6: Ledger question
        if (eventType === 'ledger_question') {
            if (parsed) showLedgerToast(parsed);
            return;
        }

        if (eventType === 'ledger_answered') {
            hideLedgerToast();
            return;
        }

        if (eventType === 'apply_paused') {
            if (parsed) {
                showApplyPausedBanner(parsed);
                appendLog(`[Application] Paused for manual form fix: ${parsed.company || ''} — ${parsed.message || ''}`);
            }
            return;
        }

        if (eventType === 'apply_resumed') {
            hideApplyPausedBanner();
            if (parsed && parsed.timeout) {
                appendLog('[Application] Pause timed out — agent will report form failure.');
            } else {
                appendLog('[Application] Resumed after manual form step.');
            }
            return;
        }

        if (parsed && parsed.type === 'job_decision') {
            addTrackingRow(parsed);
            fetchSummary();
            return;
        }

        if (rawData && rawData !== ': heartbeat') {
            appendLog(rawData);
        }
    }

    function initSSE() {
        if (eventSource) eventSource.close();
        eventSource = new EventSource('/stream');
        const events = ['terminal_log', 'agent_status', 'settings_updated', 'session_expired',
                        'bot_challenge', 'review_request', 'review_resolved', 'review_expired',
                        'ledger_question', 'ledger_answered', 'agent_crash',
                        'apply_paused', 'apply_resumed'];
        events.forEach(evt => {
            eventSource.addEventListener(evt, (e) => handleStreamPayload(evt, e.data));
        });
        eventSource.onmessage = (e) => handleStreamPayload('message', e.data);
    }

    // ===== AGENT CONTROLS =====
    startBtn.addEventListener('click', async () => {
        appendLog('Initiating Agent...');
        const res = await fetch('/api/agent/start', { method: 'POST' });
        const data = await res.json();
        if (!data.success) {
            appendLog(`[Error] ${data.message}`);
            alert(data.message);
        }
    });

    stopBtn.addEventListener('click', async () => {
        appendLog('Stopping Agent...');
        await fetch('/api/agent/stop', { method: 'POST' });
    });

    clearBtn.addEventListener('click', () => { terminal.innerHTML = ''; });

    // ===== TABLE FETCHERS =====
    async function fetchAllApps() {
        const res = await fetch('/api/applications');
        const data = await res.json();
        const tbody = document.getElementById('all-table').querySelector('tbody');

        // Color-code by failure category (Fix #5)
        const categoryColors = {
            'SESSION_EXPIRED': 'cat-session',
            'CAPTCHA_DETECTED': 'cat-captcha',
            'UNSUPPORTED_PORTAL': 'cat-portal',
            'MISSING_LEDGER_ANSWER': 'cat-ledger',
            'SITE_TIMEOUT': 'cat-timeout',
            'score_too_low': 'cat-score',
            'blacklist': 'cat-blacklist',
        };

        tbody.innerHTML = data.map(i => {
            const catClass = categoryColors[i.decision_category] || '';
            return `<tr><td>${i.run_id}</td><td>${i.timestamp}</td><td>${i.company}</td><td>${i.title}</td><td>${i.match_score}</td><td>${i.decision_status}</td><td class="${catClass}">${i.decision_category}</td><td>${i.decision_detail}</td></tr>`;
        }).join('');
    }

    async function fetchApplied() {
        const res = await fetch('/api/applied');
        const data = await res.json();
        const tbody = document.getElementById('applied-table').querySelector('tbody');
        tbody.innerHTML = data.map(i => `<tr><td>${i.run_id}</td><td>${i['Date Applied']}</td><td>${i.Company}</td><td>${i.Role}</td><td>${i['Match Score']}</td><td>${i.Mode}</td></tr>`).join('');
    }

    async function fetchLedger() {
        const res = await fetch('/api/ledger');
        const data = await res.json();
        const tbody = document.getElementById('ledger-table').querySelector('tbody');
        tbody.innerHTML = (data || []).map((i) => {
            const src = (i.source || 'user').toLowerCase() === 'ai' ? 'ai' : 'user';
            const rowClass = src === 'ai' ? 'ledger-row-ai' : '';
            const badgeClass = src === 'ai' ? 'source-badge ai' : 'source-badge user';
            const badgeLabel = src === 'ai' ? 'AI' : 'User';
            const qEsc = escapeHtml(i.question);
            const aEsc = escapeHtml(i.answer);
            const qEnc = encodeURIComponent(i.question || '');
            return `<tr class="${rowClass} ledger-edit-row">
                <td>${qEsc}</td>
                <td class="ledger-answer-cell">${aEsc}</td>
                <td><span class="${badgeClass}">${badgeLabel}</span></td>
                <td class="ledger-actions">
                    <button type="button" class="btn small ledger-edit-btn" data-qenc="${qEnc}">Edit</button>
                    <button type="button" class="btn small danger ledger-del-btn" data-qenc="${qEnc}">Delete</button>
                </td>
            </tr>`;
        }).join('');

        tbody.querySelectorAll('.ledger-edit-btn').forEach((btn) => {
            btn.addEventListener('click', async () => {
                const raw = btn.getAttribute('data-qenc');
                const q = raw ? decodeURIComponent(raw) : '';
                if (!q) return;
                const row = (data || []).find((r) => r.question === q);
                const cur = row ? row.answer : '';
                const next = window.prompt('Edit answer:', cur);
                if (next == null || !String(next).trim()) return;
                const resUp = await fetch('/api/ledger/update', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ question: q, answer: String(next).trim() })
                });
                const up = await resUp.json();
                if (up.success) {
                    appendLog('[Ledger] Entry updated (marked as User).');
                    fetchLedger();
                } else {
                    appendLog(`[Ledger] Update failed: ${up.message || 'error'}`);
                }
            });
        });
        tbody.querySelectorAll('.ledger-del-btn').forEach((btn) => {
            btn.addEventListener('click', async () => {
                const raw = btn.getAttribute('data-qenc');
                const q = raw ? decodeURIComponent(raw) : '';
                if (!q || !window.confirm('Delete this saved answer from the ledger?')) return;
                const resDel = await fetch('/api/ledger/delete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ question: q })
                });
                const del = await resDel.json();
                if (del.success) {
                    appendLog('[Ledger] Entry deleted.');
                    fetchLedger();
                } else {
                    appendLog(`[Ledger] Delete failed: ${del.message || 'error'}`);
                }
            });
        });

        // Fix #6: Show pending questions
        const pendingRes = await fetch('/api/ledger/pending');
        const pendingData = await pendingRes.json();
        const pending = pendingData.pending || [];
        const pendingSection = document.getElementById('pending-questions-section');
        const pendingList = document.getElementById('pending-questions-list');

        if (pending.length > 0) {
            pendingSection.style.display = 'block';
            pendingList.innerHTML = pending.map(q => `
                <div class="pending-question-row">
                    <span class="pending-q">${q}</span>
                    <input type="text" class="pending-input" placeholder="Type answer..." data-question="${q}">
                    <button class="btn small primary pending-save-btn" data-question="${q}">Save</button>
                </div>
            `).join('');
            pendingList.querySelectorAll('.pending-save-btn').forEach(btn => {
                btn.addEventListener('click', async () => {
                    const q = btn.dataset.question;
                    const input = pendingList.querySelector(`.pending-input[data-question="${q}"]`);
                    const answer = input?.value?.trim();
                    if (!answer) return;
                    await fetch('/api/ledger/answer', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ question: q, answer })
                    });
                    appendLog(`[Ledger] Saved answer for: "${q.slice(0, 50)}"`);
                    fetchLedger();
                });
            });
        } else {
            pendingSection.style.display = 'none';
        }
    }

    async function fetchCreds() {
        const res = await fetch('/api/credentials');
        const data = await res.json();
        const tbody = document.getElementById('cred-table').querySelector('tbody');
        tbody.innerHTML = data.map(i => `<tr><td>${i.Company}</td><td>${i.User_ID}</td><td>${i.Password}</td></tr>`).join('');
    }

    // ===== SETTINGS =====
    async function fetchSettings() {
        const res = await fetch('/api/settings');
        const data = await res.json();
        document.getElementById('set-job-source').value = data.job_source || 'greenhouse';
        document.getElementById('set-mode').value = data.mode;
        document.getElementById('set-threshold').value = data.match_threshold;
        document.getElementById('set-max-apps').value = data.max_daily_applications;
        document.getElementById('set-queries').value = (data.search_queries || []).join(', ');
        document.getElementById('set-target-companies').value = (data.target_companies || []).join(', ');
        document.getElementById('set-locations').value = (data.preferred_locations || []).join(', ');
        document.getElementById('set-blacklist').value = (data.blacklisted_companies || []).join(', ');
        const stopErr = document.getElementById('set-stop-on-form-error');
        if (stopErr) stopErr.checked = !!data.stop_on_form_error;
    }

    document.getElementById('save-settings-btn').addEventListener('click', async () => {
        const payload = {
            job_source: document.getElementById('set-job-source').value,
            mode: document.getElementById('set-mode').value,
            match_threshold: parseInt(document.getElementById('set-threshold').value),
            max_daily_applications: parseInt(document.getElementById('set-max-apps').value),
            search_queries: document.getElementById('set-queries').value.split(',').map(s => s.trim()).filter(s => s),
            target_companies: document.getElementById('set-target-companies').value.split(',').map(s => s.trim()).filter(s => s),
            preferred_locations: document.getElementById('set-locations').value.split(',').map(s => s.trim()).filter(s => s),
            blacklisted_companies: document.getElementById('set-blacklist').value.split(',').map(s => s.trim()).filter(s => s),
            stop_on_form_error: document.getElementById('set-stop-on-form-error')?.checked || false
        };
        const res = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (data.success) appendLog('[Settings] Settings saved successfully.');
    });

    const refreshLedgerBtn = document.getElementById('refresh-ledger-btn');
    if (refreshLedgerBtn) refreshLedgerBtn.addEventListener('click', () => fetchLedger());

    // ===== INIT =====
    checkResumeStatus();
    fetchSummary();
    runPreflight();  // Fix #1: Run preflight on page load
    initSSE();

    // Refresh preflight every 30 seconds
    setInterval(runPreflight, 30000);
});
