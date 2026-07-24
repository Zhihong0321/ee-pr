let currentCandidates = [];
let pollingTimer = null;

document.addEventListener('DOMContentLoaded', () => {
  checkConnections();
  fetchDiscovery();
  fetchHistory();
  startStatusPolling();
});

// 1. Connection Diagnostic Status
async function checkConnections() {
  const btn = document.getElementById('btnCheckConn');
  if (btn) btn.disabled = true;

  try {
    const res = await fetch('/api/connections');
    const data = await res.json();

    updateBadge('statusR2', data.r2);
    updateBadge('statusPcloud', data.pcloud_api);
    updateBadge('statusFiledn', data.filedn_public);
    updateBadge('statusPg', data.pg_proxy);
  } catch (err) {
    console.error('Connection check failed:', err);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function updateBadge(elementId, result) {
  const el = document.getElementById(elementId);
  if (!el) return;

  if (result.status === 'connected') {
    el.innerHTML = `<span class="badge-dot connected"></span> Connected (${result.details})`;
    el.style.color = 'var(--success)';
  } else if (result.status === 'warning') {
    el.innerHTML = `<span class="badge-dot"></span> Warning (${result.details})`;
    el.style.color = 'var(--warning)';
  } else {
    el.innerHTML = `<span class="badge-dot error"></span> Error (${result.details || 'Failed'})`;
    el.style.color = 'var(--danger)';
  }
}

// 2. Discovery Candidates Search
async function fetchDiscovery() {
  const age = document.getElementById('filterAge').value || 60;
  const status = document.getElementById('filterStatus').value || 'Approved';
  const tbody = document.getElementById('candidateTableBody');

  tbody.innerHTML = `<tr><td colspan="7" style="text-align:center; padding: 24px; color: var(--text-muted);">Searching PostgreSQL for eligible records...</td></tr>`;

  try {
    const res = await fetch(`/api/discovery?age_days=${age}&status=${status}`);
    const data = await res.json();

    if (!data.success) {
      tbody.innerHTML = `<tr><td colspan="7" style="text-align:center; color: var(--danger);">Error: ${data.error}</td></tr>`;
      return;
    }

    currentCandidates = data.candidates || [];
    document.getElementById('discoveryCount').innerText = `${data.total_candidate_files} files in ${data.matching_records_count} DB records`;

    if (currentCandidates.length === 0) {
      tbody.innerHTML = `<tr><td colspan="7" style="text-align:center; padding: 24px; color: var(--text-muted);">No candidates matching age >= ${age} days and status '${status}'.</td></tr>`;
      return;
    }

    tbody.innerHTML = currentCandidates.map((c, i) => `
      <tr>
        <td><input type="checkbox" class="candidate-select" data-index="${i}" checked></td>
        <td><strong>#${c.db_id}</strong></td>
        <td>${escapeHtml(c.applicant_name)}</td>
        <td><span style="font-size: 11px; padding: 2px 8px; border-radius: 10px; background: rgba(16, 185, 129, 0.2); color: var(--success);">${escapeHtml(c.seda_status)}</span></td>
        <td class="code-font">${escapeHtml(c.column_name)}</td>
        <td title="${escapeHtml(c.original_url)}"><a href="${escapeHtml(c.original_url)}" target="_blank" style="color: var(--primary); text-decoration: none;">${escapeHtml(c.filename)}</a></td>
        <td class="code-font" title="${escapeHtml(c.target_public_url)}">${escapeHtml(c.target_pcloud_path)}</td>
      </tr>
    `).join('');
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="7" style="text-align:center; color: var(--danger);">Failed to query discovery candidates: ${err.message}</td></tr>`;
  }
}

function toggleSelectAll(master) {
  const checkboxes = document.querySelectorAll('.candidate-select');
  checkboxes.forEach(cb => cb.checked = master.checked);
}

// 3. Migration Execution
function getSelectedItems() {
  const checkboxes = document.querySelectorAll('.candidate-select:checked');
  const selected = [];
  checkboxes.forEach(cb => {
    const idx = parseInt(cb.getAttribute('data-index'), 10);
    if (currentCandidates[idx]) selected.push(currentCandidates[idx]);
  });
  return selected;
}

async function startMigration(mode) {
  const selected = getSelectedItems();
  if (selected.length === 0) {
    alert('Please select at least one candidate file from the table.');
    return;
  }

  try {
    const res = await fetch('/api/job/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode, items: selected })
    });
    const data = await res.json();
    if (data.success) {
      alert(`Started batch '${data.batch_id}' in ${mode.toUpperCase()} mode for ${data.total_items} items.`);
    } else {
      alert(`Error starting job: ${data.error}`);
    }
  } catch (err) {
    alert(`Failed to trigger migration: ${err.message}`);
  }
}

function confirmLiveMigration() {
  const selected = getSelectedItems();
  if (selected.length === 0) {
    alert('Please select at least one candidate file from the table.');
    return;
  }

  if (confirm(`🚨 SAFETY CONFIRMATION:\nAre you sure you want to start a LIVE migration for ${selected.length} items?\n\nThis will stream files to pCloud, take full SQLite snapshots, update PostgreSQL URLs, and retain deferred R2 backups.`)) {
    startMigration('live');
  }
}

// 4. Job Status & Log Polling
function startStatusPolling() {
  if (pollingTimer) clearInterval(pollingTimer);
  pollingTimer = setInterval(pollJobStatus, 1500);
}

async function pollJobStatus() {
  try {
    const res = await fetch('/api/job/status');
    const job = await res.json();

    const modeBadge = document.getElementById('jobModeBadge');
    if (modeBadge) {
      modeBadge.innerText = `${job.mode.toUpperCase()} (${job.status.toUpperCase()})`;
    }

    const progressFill = document.getElementById('progressBarFill');
    const pct = job.stats.total > 0 ? Math.round((job.stats.completed + job.stats.failed) / job.stats.total * 100) : 0;
    if (progressFill) progressFill.style.width = `${pct}%`;

    document.getElementById('jobProgressText').innerText = `${job.stats.completed + job.stats.failed} / ${job.stats.total}`;
    document.getElementById('jobCompletedCount').innerText = job.stats.completed;
    document.getElementById('jobFailedCount').innerText = job.stats.failed;

    // Toggle controls
    document.getElementById('btnPause').disabled = (job.status !== 'running');
    document.getElementById('btnResume').disabled = (job.status !== 'paused');
    document.getElementById('btnStop').disabled = (job.status !== 'running' && job.status !== 'paused');

    // Render Logs
    if (job.logs && job.logs.length > 0) {
      const term = document.getElementById('terminalWindow');
      term.innerHTML = job.logs.map(l => `
        <div class="log-entry ${l.type}">
          [${l.timestamp.substring(11, 19)}] ${escapeHtml(l.msg)}
        </div>
      `).join('');
    }
  } catch (err) {
    console.error('Job status polling error:', err);
  }
}

async function controlJob(action) {
  try {
    await fetch('/api/job/control', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action })
    });
  } catch (err) {
    alert(`Control action failed: ${err.message}`);
  }
}

function clearLogs() {
  const term = document.getElementById('terminalWindow');
  if (term) term.innerHTML = '<div class="log-entry info">[SYSTEM] Logs cleared.</div>';
}

// 5. History & Rollback
async function fetchHistory() {
  const tbody = document.getElementById('historyTableBody');
  try {
    const res = await fetch('/api/history');
    const rows = await res.json();

    if (rows.length === 0) {
      tbody.innerHTML = `<tr><td colspan="8" style="text-align: center; color: var(--text-muted); padding: 20px;">No migration history yet.</td></tr>`;
      return;
    }

    tbody.innerHTML = rows.map(r => `
      <tr>
        <td>#${r.id}</td>
        <td class="code-font">${escapeHtml(r.batch_id)}</td>
        <td><strong>#${r.record_id}</strong></td>
        <td class="code-font">${escapeHtml(r.column_name)}</td>
        <td><span style="padding: 2px 8px; border-radius: 10px; font-size: 11px; background: ${r.state === 'reverted' ? 'rgba(239, 68, 68, 0.2)' : 'rgba(16, 185, 129, 0.2)'}; color: ${r.state === 'reverted' ? 'var(--danger)' : 'var(--success)'};">${escapeHtml(r.state)}</span></td>
        <td class="code-font">${escapeHtml(r.new_pcloud_url)}</td>
        <td style="color: var(--danger); font-size: 11px;">${escapeHtml(r.error_log || '-')}</td>
        <td>
          ${(r.state === 'completed' || r.state === 'db_updated' || r.state === 'dry_run_passed') ? `
            <button class="btn btn-secondary" style="padding: 4px 8px; font-size: 11px;" onclick="rollbackRecord(${r.record_id})">
              ↩️ Revert
            </button>
          ` : '<span style="color: var(--text-dim); font-size: 11px;">N/A</span>'}
        </td>
      </tr>
    `).join('');
  } catch (err) {
    console.error('Failed to fetch history:', err);
  }
}

async function rollbackRecord(recordId) {
  if (!confirm(`Are you sure you want to revert PostgreSQL Record #${recordId} back to its original URL?`)) return;

  try {
    const res = await fetch('/api/rollback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ record_id: recordId })
    });
    const data = await res.json();

    if (data.success) {
      alert(`Successfully reverted ${data.reverted_count} record(s).`);
      fetchHistory();
      fetchDiscovery();
    } else {
      alert(`Rollback failed: ${data.error}`);
    }
  } catch (err) {
    alert(`Rollback request failed: ${err.message}`);
  }
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
