/* Tealc HQ Dashboard — dashboard.js
   Vanilla JS, no frameworks, no bundler.
   Fetches /api/state every 30s, /api/settings on Control tab open.
   POSTs actions to /api/action, settings to /api/settings. */

'use strict';

/* ── Tiny markdown shim ───────────────────────────────────────────────────── */
function renderMd(text) {
  if (!text) return '';
  // Escape HTML first
  let s = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
  // **bold**
  s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  // *italic*
  s = s.replace(/\*(.+?)\*/g, '<em>$1</em>');
  // `code`
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  // Bullet list lines: lines starting with "- "
  s = s.replace(/^- (.+)$/gm, '<li>$1</li>');
  s = s.replace(/(<li>.*<\/li>\n?)+/g, (m) => `<ul>${m}</ul>`);
  // Line breaks (double newline = paragraph break)
  s = s.replace(/\n\n+/g, '</p><p>');
  s = s.replace(/\n/g, '<br>');
  return `<p>${s}</p>`;
}

/* ── Format timestamp ─────────────────────────────────────────────────────── */
function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' +
         d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

/* ── POST /api/action helper ──────────────────────────────────────────────── */
async function postAction(payload, errorEl) {
  try {
    const res = await fetch('/api/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const txt = await res.text();
      if (errorEl) errorEl.textContent = `Error ${res.status}: ${txt}`;
      return false;
    }
    // Fire-and-forget: kick publish_dashboard so data/dashboard_state.json
    // reflects this action within ~1 sec.  Without this, the next periodic
    // loadState() (every 30s) would re-render the just-dismissed card from
    // stale JSON until publish_dashboard's own 1-min cron tick fires.
    fetch('/api/run_job', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_name: 'publish_dashboard' }),
    }).catch(() => {});
    return true;
  } catch (e) {
    if (errorEl) errorEl.textContent = `Network error: ${e.message}`;
    return false;
  }
}

/* ── Action button factory ────────────────────────────────────────────────── */
function makeBtn(label, cls, onClick) {
  const b = document.createElement('button');
  b.className = 'btn ' + (cls || '');
  b.textContent = label;
  b.addEventListener('click', onClick);
  return b;
}

function actionsEl(...btns) {
  const div = document.createElement('div');
  div.className = 'actions';
  btns.forEach(b => div.appendChild(b));
  return div;
}

function errorEl() {
  const el = document.createElement('div');
  el.className = 'inline-error';
  return el;
}

/* ── Render helpers ───────────────────────────────────────────────────────── */

function renderStalledGoals(items) {
  const container = document.getElementById('stalledGoals');
  container.innerHTML = '';
  if (!items || !items.length) return;

  const h = document.createElement('h2');
  h.textContent = 'Stalled Goals';
  container.appendChild(h);

  items.forEach(item => {
    const card = document.createElement('div');
    card.className = 'card stalled';

    const title = document.createElement('div');
    title.className = 'card-title';
    title.textContent = item.name || item.id;

    const meta = document.createElement('div');
    meta.className = 'card-meta';
    meta.textContent = `Stalled ${item.days_stale ?? '?'} day${item.days_stale === 1 ? '' : 's'}`;

    if (item.description) {
      const body = document.createElement('div');
      body.className = 'card-body';
      body.textContent = item.description;
      card.appendChild(title);
      card.appendChild(meta);
      card.appendChild(body);
    } else {
      card.appendChild(title);
      card.appendChild(meta);
    }

    const err = errorEl();
    const actions = actionsEl(
      makeBtn('Work now', 'btn-primary', async () => {
        const ok = await postAction({ action: 'defer_stalled_goal', target_id: item.id, reason: 'working on it now' }, err);
        if (ok) card.remove();
      }),
      makeBtn('Defer', '', async () => {
        const reason = window.prompt('Reason for deferring?');
        if (reason === null) return;
        const ok = await postAction({ action: 'defer_stalled_goal', target_id: item.id, reason }, err);
        if (ok) card.remove();
      }),
    );
    card.appendChild(actions);
    card.appendChild(err);
    container.appendChild(card);
  });
}

function renderMeetingBriefs(items) {
  const container = document.getElementById('meetingBriefs');
  container.innerHTML = '';
  if (!items || !items.length) return;

  const h = document.createElement('h2');
  h.textContent = 'Meeting Briefs';
  container.appendChild(h);

  items.forEach(item => {
    const card = document.createElement('details');
    card.className = 'card meeting';

    const summary = document.createElement('summary');
    const titleSpan = document.createElement('span');
    titleSpan.textContent = item.title || item.id;
    summary.appendChild(titleSpan);

    const body = document.createElement('div');
    body.className = 'details-body';

    if (item.when) {
      const meta = document.createElement('div');
      meta.className = 'card-meta';
      meta.style.paddingTop = '10px';
      meta.textContent = fmtDate(item.when);
      body.appendChild(meta);
    }

    if (item.content_md) {
      const content = document.createElement('div');
      content.className = 'card-body';
      content.innerHTML = renderMd(item.content_md);
      body.appendChild(content);
    }

    const err = errorEl();
    const actions = actionsEl(
      makeBtn('Mark done', 'btn-primary', async () => {
        const ok = await postAction({ action: 'complete_briefing', target_id: item.id }, err);
        if (ok) card.remove();
      }),
    );
    body.appendChild(actions);
    body.appendChild(err);

    card.appendChild(summary);
    card.appendChild(body);
    container.appendChild(card);
  });
}

function renderDraftsUnreviewed(items) {
  const container = document.getElementById('draftsUnreviewed');
  container.innerHTML = '';
  if (!items || !items.length) return;

  const h = document.createElement('h2');
  h.textContent = 'Drafts Awaiting Review';
  container.appendChild(h);

  items.forEach(item => {
    const card = document.createElement('div');
    card.className = 'card draft';

    const header = document.createElement('div');
    header.style.display = 'flex';
    header.style.alignItems = 'center';
    header.style.gap = '8px';
    header.style.marginBottom = '4px';

    const title = document.createElement('div');
    title.className = 'card-title';
    title.style.margin = '0';
    title.textContent = item.title || item.id;
    header.appendChild(title);

    if (item.critic_score != null) {
      const score = item.critic_score;
      const cls = score >= 7 ? 'good' : score >= 4 ? 'warn' : 'bad';
      const badge = document.createElement('span');
      badge.className = `score-badge ${cls}`;
      badge.textContent = `Score: ${score}`;
      header.appendChild(badge);
    }

    card.appendChild(header);

    if (item.summary) {
      const body = document.createElement('div');
      body.className = 'card-body';
      body.textContent = item.summary;
      card.appendChild(body);
    }

    const err = errorEl();
    const actions = actionsEl(
      makeBtn('Accept', 'btn-primary', async () => {
        const ok = await postAction({ action: 'review_draft', target_id: item.id, outcome: 'accepted' }, err);
        if (ok) card.remove();
      }),
      makeBtn('Edit', '', async () => {
        const ok = await postAction({ action: 'review_draft', target_id: item.id, outcome: 'edited' }, err);
        if (ok) card.remove();
      }),
      makeBtn('Reject', 'btn-danger', async () => {
        const reason = window.prompt('Reason for rejection?');
        if (reason === null) return;
        const ok = await postAction({ action: 'review_draft', target_id: item.id, outcome: 'rejected', reason }, err);
        if (ok) card.remove();
      }),
    );
    card.appendChild(actions);
    card.appendChild(err);
    container.appendChild(card);
  });
}

function renderReviewInvitations(items) {
  const container = document.getElementById('reviewInvites');
  container.innerHTML = '';
  if (!items || !items.length) return;

  const h = document.createElement('h2');
  h.textContent = 'Review Invitations';
  container.appendChild(h);

  items.forEach(item => {
    const card = document.createElement('details');
    card.className = 'card invite';

    const summary = document.createElement('summary');
    const titleSpan = document.createElement('span');
    titleSpan.textContent = item.title || item.id;
    summary.appendChild(titleSpan);

    const body = document.createElement('div');
    body.className = 'details-body';

    if (item.deadline) {
      const meta = document.createElement('div');
      meta.className = 'card-meta';
      meta.style.paddingTop = '10px';
      meta.textContent = `Due: ${fmtDate(item.deadline)}`;
      body.appendChild(meta);
    }

    if (item.content_md) {
      const content = document.createElement('div');
      content.className = 'card-body';
      content.innerHTML = renderMd(item.content_md);
      body.appendChild(content);
    }

    const err = errorEl();
    const actions = actionsEl(
      makeBtn('Accept', 'btn-primary', async () => {
        const ok = await postAction({ action: 'complete_briefing', target_id: item.id, reason: 'accepted review invitation' }, err);
        if (ok) card.remove();
      }),
      makeBtn('Decline', 'btn-danger', async () => {
        const reason = window.prompt('Reason for declining?');
        if (reason === null) return;
        const ok = await postAction({ action: 'complete_briefing', target_id: item.id, reason: 'declined: ' + reason }, err);
        if (ok) card.remove();
      }),
      makeBtn('Snooze', '', async () => {
        const ok = await postAction({ action: 'defer_briefing', target_id: item.id, reason: 'snoozed' }, err);
        if (ok) card.remove();
      }),
    );
    body.appendChild(actions);
    body.appendChild(err);

    card.appendChild(summary);
    card.appendChild(body);
    container.appendChild(card);
  });
}

function renderHypothesesPending(items) {
  const container = document.getElementById('hypothesesPending');
  container.innerHTML = '';
  if (!items || !items.length) return;

  const h = document.createElement('h2');
  h.textContent = 'Pending Hypotheses';
  container.appendChild(h);

  items.forEach(item => {
    const card = document.createElement('details');
    card.className = 'card hyp';

    const summary = document.createElement('summary');
    const titleWrap = document.createElement('span');
    titleWrap.style.display = 'flex';
    titleWrap.style.alignItems = 'center';
    titleWrap.style.gap = '6px';
    titleWrap.style.flex = '1';

    const titleSpan = document.createElement('span');
    titleSpan.textContent = item.title || item.id;
    titleWrap.appendChild(titleSpan);

    if (item.novelty != null) {
      const b = document.createElement('span');
      b.className = 'score-badge';
      b.textContent = `Novelty: ${item.novelty}`;
      titleWrap.appendChild(b);
    }
    if (item.feasibility != null) {
      const b = document.createElement('span');
      b.className = 'score-badge';
      b.textContent = `Feasibility: ${item.feasibility}`;
      titleWrap.appendChild(b);
    }

    summary.appendChild(titleWrap);

    const body = document.createElement('div');
    body.className = 'details-body';

    if (item.content_md) {
      const content = document.createElement('div');
      content.className = 'card-body';
      content.innerHTML = renderMd(item.content_md);
      body.appendChild(content);
    }

    const err = errorEl();
    const actions = actionsEl(
      makeBtn('Adopt', 'btn-primary', async () => {
        const ok = await postAction({ action: 'adopt_hypothesis', target_id: item.id }, err);
        if (ok) card.remove();
      }),
      makeBtn('Reject', 'btn-danger', async () => {
        const reason = window.prompt('Reason for rejection?');
        if (reason === null) return;
        const ok = await postAction({ action: 'reject_hypothesis', target_id: item.id, reason }, err);
        if (ok) card.remove();
      }),
    );
    body.appendChild(actions);
    body.appendChild(err);

    card.appendChild(summary);
    card.appendChild(body);
    container.appendChild(card);
  });
}

function renderOtherBriefings(items) {
  const container = document.getElementById('otherBriefings');
  container.innerHTML = '';
  if (!items || !items.length) return;

  const h = document.createElement('h2');
  h.textContent = 'Other Briefings';
  container.appendChild(h);

  items.forEach(item => {
    const card = document.createElement('details');
    card.className = 'card';

    const summary = document.createElement('summary');
    const titleSpan = document.createElement('span');
    titleSpan.textContent = item.title || item.id;
    summary.appendChild(titleSpan);

    const body = document.createElement('div');
    body.className = 'details-body';

    if (item.content_md) {
      const content = document.createElement('div');
      content.className = 'card-body';
      content.innerHTML = renderMd(item.content_md);
      body.appendChild(content);
    }

    const err = errorEl();
    const actions = actionsEl(
      makeBtn('Acknowledge', 'btn-primary', async () => {
        const ok = await postAction({ action: 'complete_briefing', target_id: item.id }, err);
        if (ok) card.remove();
      }),
    );
    body.appendChild(actions);
    body.appendChild(err);

    card.appendChild(summary);
    card.appendChild(body);
    container.appendChild(card);
  });
}

/* ── Activity tab renderers ───────────────────────────────────────────────── */

function renderSchedulerBlock(scheduler) {
  const container = document.getElementById('schedulerBlock');
  container.innerHTML = '';
  if (!scheduler) return;

  const h = document.createElement('h2');
  h.textContent = 'Scheduler';
  container.appendChild(h);

  const row = document.createElement('div');
  row.className = 'scheduler-row';

  // Backend keys: alive (bool), age_seconds (int), pid (str)
  const statusText = scheduler.alive ? 'Alive' : 'Down';
  const ageText = scheduler.age_seconds != null ? `${scheduler.age_seconds}s since last heartbeat` : '—';
  const pairs = [
    ['Status', statusText],
    ['Heartbeat', ageText],
    ['PID', scheduler.pid ?? '—'],
  ];

  pairs.forEach(([key, val], idx) => {
    const wrap = document.createElement('span');
    wrap.innerHTML = `<span class="scheduler-key">${key}</span>&nbsp;${val ?? '—'}`;
    row.appendChild(wrap);
    if (idx < pairs.length - 1) {
      const sep = document.createElement('span');
      sep.style.color = 'var(--rule-strong)';
      sep.textContent = '  ·  ';
      row.appendChild(sep);
    }
  });

  container.appendChild(row);
}

function renderJobsTable(jobs) {
  const container = document.getElementById('jobsTable');
  container.innerHTML = '';
  if (!jobs || !jobs.length) return;

  const h = document.createElement('h2');
  h.textContent = 'Job Runs';
  container.appendChild(h);

  const table = document.createElement('table');
  table.className = 'data-table';

  const thead = table.createTHead();
  const hrow = thead.insertRow();
  ['Job', 'Runs', 'OK', 'Fail', 'Latest'].forEach(col => {
    const th = document.createElement('th');
    th.textContent = col;
    hrow.appendChild(th);
  });

  const tbody = table.createTBody();
  jobs.forEach(job => {
    const row = tbody.insertRow();
    // Backend keys: job_name, runs, ok, fail, latest_summary
    [
      job.job_name || job.name || '—',
      job.runs ?? '—',
      job.ok ?? '—',
      job.fail ?? '—',
      (job.latest_summary || '—').toString().slice(0, 80),
    ].forEach((val, i) => {
      const td = row.insertCell();
      td.textContent = val;
      if (i === 2) td.className = 'ok';
      if (i === 3) td.className = 'fail';
    });
  });

  container.appendChild(table);
}

function renderLedgerTable(ledger) {
  const container = document.getElementById('ledgerTable');
  container.innerHTML = '';
  if (!ledger || !ledger.length) return;

  const h = document.createElement('h2');
  h.textContent = 'Recent Ledger';
  container.appendChild(h);

  const table = document.createElement('table');
  table.className = 'data-table';

  const thead = table.createTHead();
  const hrow = thead.insertRow();
  ['Time', 'Kind', 'Project', 'Critic'].forEach(col => {
    const th = document.createElement('th');
    th.textContent = col;
    hrow.appendChild(th);
  });

  const tbody = table.createTBody();
  // Backend keys: id, kind, project_id, critic_score, created_at
  ledger.forEach(entry => {
    const row = tbody.insertRow();
    row.insertCell().textContent = entry.created_at ? fmtDate(entry.created_at) : '—';
    row.insertCell().textContent = entry.kind || '—';
    row.insertCell().textContent = entry.project_id || '—';
    const critic = row.insertCell();
    critic.className = 'num';
    critic.textContent = entry.critic_score != null ? `${entry.critic_score}/5` : '—';
  });

  container.appendChild(table);
}

function renderCostBlock(cost) {
  const container = document.getElementById('costBlock');
  container.innerHTML = '';
  if (!cost) return;

  const h = document.createElement('h2');
  h.textContent = 'System Health';
  container.appendChild(h);

  const grid = document.createElement('div');
  grid.className = 'cost-grid';

  // Backend keys: cost_24h_usd, retrieval_quality_7d_mean
  const items = [
    ['Cost (24h)', cost.cost_24h_usd != null ? '$' + Number(cost.cost_24h_usd).toFixed(3) : '—'],
    ['Retrieval quality (7d)', cost.retrieval_quality_7d_mean != null ? `${Number(cost.retrieval_quality_7d_mean).toFixed(2)}/5` : '—'],
  ];

  items.forEach(([label, value]) => {
    const item = document.createElement('div');
    item.className = 'cost-item';
    item.innerHTML = `<span class="cost-label">${label}</span><span class="cost-value">${value}</span>`;
    grid.appendChild(item);
  });

  container.appendChild(grid);
}

/* ── Control tab ──────────────────────────────────────────────────────────── */

const JOB_LABELS = {
  morning_briefing:            'Morning briefing',
  midday_check:                'Midday check-in',
  deadline_countdown:          'Deadline countdown',
  meeting_prep:                'Meeting prep',
  vip_email_watch:             'VIP email watch',
  paper_of_the_day:            'Paper of the day',
  daily_plan:                  'Daily plan',
  projects_mirror:             'Wiki · projects board',
  contradictions_index:        'Wiki · contradictions board',
  open_questions_index:        'Wiki · open-questions board',
  nightly_grant_drafter:       'Overnight grant drafting',
  nightly_literature_synthesis:'Overnight literature synthesis',
  surface_composer:            'Wiki · dual-register composer',
  weekly_hypothesis_generator: 'Weekly hypothesis proposals',
  weekly_comparative_analysis: 'Weekly comparative analysis',
  cross_project_synthesis:     'Cross-project synthesis',
  exploratory_analysis:        'Exploratory Python analysis',
  student_agenda_drafter:      'Student 1:1 agendas',
  student_pulse:               'Student pulse check',
  nas_pipeline_health:         'NAS pipeline health',
  nas_case_packet:             'Monthly NAS case packet',
  goal_conflict_check:         'Goal conflict detection',
  weekly_review:               'Weekly self-review',
  wiki_pipeline:               'Wiki · paper ingest pipeline',
  wiki_janitor:                'Wiki · janitor audit',
  refresh_enrichment:          'Wiki · related-topics refresh',
  improve_wiki:                'Wiki · Opus prose improvement',
  gloss_harvester:             'Wiki · concept harvester',
  method_promoter:             'Wiki · method-page promoter',
  sync_lab_projects:           'Lab projects · Drive sync + audit',
};

const DAILY_JOBS    = ['morning_briefing','midday_check','deadline_countdown','meeting_prep','vip_email_watch','paper_of_the_day','daily_plan','projects_mirror','contradictions_index','open_questions_index'];
const OVERNIGHT_JOBS = ['nightly_grant_drafter','nightly_literature_synthesis','student_agenda_drafter','surface_composer','gloss_harvester','method_promoter'];

const PRESET_DEFS = [
  { label: 'Balanced',          key: 'balanced' },
  { label: 'Grant crunch',      key: 'grant_crunch' },
  { label: 'Student focus',     key: 'student_focus' },
  { label: 'Research deep-dive',key: 'research_deep_dive' },
  { label: 'Quiet week',        key: 'quiet_week' },
];

/* Debounce helper */
function debounce(fn, ms) {
  let timer;
  return function(...args) {
    clearTimeout(timer);
    timer = setTimeout(() => fn.apply(this, args), ms);
  };
}

/* Flash the status footer */
function flashStatus(msg, isError = false) {
  const el = document.getElementById('controlStatus');
  el.textContent = msg;
  el.className = 'control-status' + (isError ? ' error' : ' ok');
  setTimeout(() => { el.textContent = ''; el.className = 'control-status'; }, 2000);
}

/* POST to /api/settings, then reload the control tab */
async function postSettings(payload) {
  try {
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    await res.json();
    flashStatus('Saved');
    await loadControl();
  } catch (e) {
    flashStatus('Error: ' + e.message, true);
  }
}

/* ── Preset section ───────────────────────────────────────────────────────── */
function renderPresets(activePreset) {
  const container = document.getElementById('presetBlock');
  container.innerHTML = '';

  const h = document.createElement('h2');
  h.textContent = 'Preset';
  container.appendChild(h);

  const grid = document.createElement('div');
  grid.className = 'preset-grid';

  PRESET_DEFS.forEach(({ label, key }) => {
    const btn = document.createElement('button');
    btn.className = 'preset-button' + (activePreset === key ? ' active' : '');
    btn.textContent = label;
    btn.addEventListener('click', () => postSettings({ preset_to_apply: key }));
    grid.appendChild(btn);
  });

  container.appendChild(grid);
}

/* ── Job modes section ────────────────────────────────────────────────────── */

/* Friendly labels for per-job toggles and inputs beyond the off/reduced/normal
   mode tri-state. Keys are the fields that appear in jobs.<name> in
   data/tealc_config.json. Missing fields are simply not rendered. */
const JOB_FLAG_LABELS = {
  dry_run:                    'Dry-run (no file writes)',
  digit_substring_blocking:   'Block findings failing digit check',
  editor_frozen:              'Editor-frozen (skip rewrites)',
  working_hours_guard:        'Working-hours guard',
  enabled:                    'Enabled',
};

const JOB_NUMBER_FIELDS = [
  { key: 'max_cost_usd_per_run', label: 'Max cost per run', unit: 'USD', min: 0, max: 50, step: 0.25 },
  { key: 'max_cost_usd_per_day', label: 'Max cost per day', unit: 'USD', min: 0, max: 200, step: 1 },
  { key: 'max_topics_per_run',   label: 'Max topics per run', unit: '',  min: 1, max: 50, step: 1 },
  { key: 'critic_sample_ratio',  label: 'Critic 1-in-N',      unit: '',  min: 1, max: 32, step: 1 },
];

async function runJobNow(jobKey, btn) {
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Running…';
  try {
    const res = await fetch('/api/run_job', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_name: jobKey, verbose: false }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      const msg = data.error || data.detail || ('HTTP ' + res.status);
      flashStatus(`${jobKey} failed: ${msg}`.slice(0, 160), true);
    } else {
      const result = (data.result || '').trim();
      flashStatus(result ? `${jobKey}: ${result}`.slice(0, 160) : `${jobKey}: ok`);
    }
  } catch (e) {
    flashStatus(`${jobKey} network error: ${e.message}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

function makeModeRow(jobKey, jobData) {
  const row = document.createElement('div');
  row.className = 'mode-row';

  const label = document.createElement('span');
  label.className = 'mode-label';
  label.textContent = JOB_LABELS[jobKey] || jobKey;
  row.appendChild(label);

  const group = document.createElement('div');
  group.className = 'mode-btn-group';

  const currentMode = (jobData && jobData.mode) || 'normal';
  ['off', 'reduced', 'normal'].forEach(mode => {
    const btn = document.createElement('button');
    btn.className = 'mode-button' + (currentMode === mode ? ' active' : '');
    btn.textContent = mode.charAt(0).toUpperCase() + mode.slice(1);
    btn.addEventListener('click', () => {
      postSettings({ jobs: { [jobKey]: { mode } } });
    });
    group.appendChild(btn);
  });

  // "Run now" — force-runs the job immediately via POST /api/run_job.
  // Bypasses the job's working-hours guard.  Visible on every job row.
  const runBtn = document.createElement('button');
  runBtn.className = 'mode-button mode-run-now';
  runBtn.type = 'button';
  runBtn.textContent = 'Run now';
  runBtn.title = 'Force-run this job now (bypasses working-hours guard)';
  runBtn.addEventListener('click', () => runJobNow(jobKey, runBtn));
  group.appendChild(runBtn);

  row.appendChild(group);

  // --- Extra flags (bool) and fields (number) for jobs that have them ---
  // Render only the keys present in the job's config; never invent defaults.
  const extras = document.createElement('div');
  extras.className = 'mode-extras';

  Object.keys(JOB_FLAG_LABELS).forEach(flagKey => {
    if (!jobData || jobData[flagKey] === undefined) return;
    const flagRow = document.createElement('label');
    flagRow.className = 'mode-flag';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = Boolean(jobData[flagKey]);
    cb.addEventListener('change', () => {
      postSettings({ jobs: { [jobKey]: { [flagKey]: cb.checked } } });
    });
    const span = document.createElement('span');
    span.textContent = JOB_FLAG_LABELS[flagKey];
    flagRow.appendChild(cb);
    flagRow.appendChild(span);
    extras.appendChild(flagRow);
  });

  JOB_NUMBER_FIELDS.forEach(({ key, label: nLabel, unit, min, max, step }) => {
    if (!jobData || jobData[key] === undefined) return;
    const nRow = document.createElement('label');
    nRow.className = 'mode-number';
    const lbl = document.createElement('span');
    lbl.textContent = nLabel;
    const input = document.createElement('input');
    input.type = 'number';
    input.min = String(min);
    input.max = String(max);
    input.step = String(step);
    input.value = String(jobData[key]);
    const debouncedSave = debounce(() => {
      const val = parseFloat(input.value);
      if (isNaN(val) || val < min || val > max) return;
      postSettings({ jobs: { [jobKey]: { [key]: val } } });
    }, 500);
    input.addEventListener('input', debouncedSave);
    const unitSpan = document.createElement('span');
    unitSpan.className = 'mode-number-unit';
    unitSpan.textContent = unit;
    nRow.appendChild(lbl);
    nRow.appendChild(input);
    if (unit) nRow.appendChild(unitSpan);
    extras.appendChild(nRow);
  });

  if (extras.childNodes.length) row.appendChild(extras);
  return row;
}

function renderJobModes(jobs) {
  const container = document.getElementById('jobModesBlock');
  container.innerHTML = '';

  const h = document.createElement('h2');
  h.textContent = 'Job Modes';
  container.appendChild(h);

  const weeklyJobs = Object.keys(jobs).filter(
    k => !DAILY_JOBS.includes(k) && !OVERNIGHT_JOBS.includes(k)
  );

  const sections = [
    { title: 'Daily proactive', keys: DAILY_JOBS },
    { title: 'Overnight work',  keys: OVERNIGHT_JOBS },
    { title: 'Weekly + deeper', keys: weeklyJobs },
  ];

  sections.forEach(({ title, keys }) => {
    const presentKeys = keys.filter(k => k in jobs || DAILY_JOBS.includes(k) || OVERNIGHT_JOBS.includes(k));
    const allKeys = keys.length ? keys : [];
    // Show section even if backend omits some keys — use empty object as fallback
    const h3 = document.createElement('h3');
    h3.textContent = title;
    container.appendChild(h3);

    allKeys.forEach(k => {
      container.appendChild(makeModeRow(k, jobs[k] || {}));
    });
  });
}

/* ── Thresholds section ───────────────────────────────────────────────────── */
const THRESHOLD_DEFS = [
  { key: 'stalled_flagship_days',      label: 'Nag about stalled NAS-critical goals after',  unit: 'days',   min: 7,  max: 60 },
  { key: 'drafter_pause_count',        label: 'Pause grant drafter after',                    unit: 'consecutive unreviewed', min: 1, max: 10 },
  { key: 'meeting_prep_lead_minutes',  label: 'Meeting prep',                                 unit: 'min before event', min: 15, max: 180 },
  { key: 'working_hours_start',        label: 'Working day starts at',                        unit: '(24h)',  min: 0,  max: 23 },
  { key: 'working_hours_end',          label: 'Working day ends at',                          unit: '(24h)',  min: 0,  max: 23 },
  { key: 'vip_email_check_minutes',    label: 'Check VIP inbox every',                        unit: 'min',    min: 1,  max: 30 },
  { key: 'deadline_countdown_days',    label: 'Surface deadlines within',                     unit: 'days',   min: 1,  max: 30 },
];

function renderThresholds(thresholds) {
  const container = document.getElementById('thresholdsBlock');
  container.innerHTML = '';

  const h = document.createElement('h2');
  h.textContent = 'Thresholds';
  container.appendChild(h);

  THRESHOLD_DEFS.forEach(({ key, label, unit, min, max }) => {
    const row = document.createElement('div');
    row.className = 'threshold-row';

    const lbl = document.createElement('span');
    lbl.className = 'threshold-label';
    lbl.textContent = label;

    const input = document.createElement('input');
    input.type = 'number';
    input.className = 'threshold-input';
    input.min = min;
    input.max = max;
    input.value = thresholds[key] != null ? thresholds[key] : '';

    const unitSpan = document.createElement('span');
    unitSpan.className = 'threshold-unit';
    unitSpan.textContent = unit;

    const debouncedSave = debounce(() => {
      const val = parseInt(input.value, 10);
      if (isNaN(val) || val < min || val > max) return;
      postSettings({ thresholds: { [key]: val } });
    }, 500);

    input.addEventListener('input', debouncedSave);

    row.appendChild(lbl);
    row.appendChild(input);
    row.appendChild(unitSpan);
    container.appendChild(row);
  });
}

/* ── Personality section ──────────────────────────────────────────────────── */
const PERSONALITY_DEFS = [
  { key: 'bluntness',   label: 'Bluntness',   lo: 'gentle',    hi: 'blunt' },
  { key: 'brevity',     label: 'Brevity',     lo: 'verbose',   hi: 'terse' },
  { key: 'skepticism',  label: 'Skepticism',  lo: 'optimistic',hi: 'skeptical' },
];

function renderPersonality(personality) {
  const container = document.getElementById('personalityBlock');
  container.innerHTML = '';

  const h = document.createElement('h2');
  h.textContent = 'Personality';
  container.appendChild(h);

  PERSONALITY_DEFS.forEach(({ key, label, lo, hi }) => {
    const row = document.createElement('div');
    row.className = 'personality-row';

    const lbl = document.createElement('span');
    lbl.className = 'personality-label';
    lbl.textContent = label;

    const loSpan = document.createElement('span');
    loSpan.className = 'personality-endpoints';
    loSpan.textContent = lo;

    const slider = document.createElement('input');
    slider.type = 'range';
    slider.className = 'personality-slider';
    slider.min = '0';
    slider.max = '1';
    slider.step = '0.05';
    slider.value = personality[key] != null ? personality[key] : '0.5';

    const valDisplay = document.createElement('span');
    valDisplay.className = 'personality-value';
    valDisplay.textContent = parseFloat(slider.value).toFixed(2);

    const hiSpan = document.createElement('span');
    hiSpan.className = 'personality-endpoints';
    hiSpan.textContent = hi;

    const debouncedSave = debounce(() => {
      postSettings({ personality: { [key]: parseFloat(slider.value) } });
    }, 500);

    slider.addEventListener('input', () => {
      valDisplay.textContent = parseFloat(slider.value).toFixed(2);
      debouncedSave();
    });

    row.appendChild(lbl);
    row.appendChild(loSpan);
    row.appendChild(slider);
    row.appendChild(hiSpan);
    row.appendChild(valDisplay);
    container.appendChild(row);
  });
}

/* ── Documents tab ────────────────────────────────────────────────────────── */

function renderDocuments(data) {
  const container = document.getElementById('documentsBlock');
  container.innerHTML = '';
  if (!data || !Array.isArray(data.categories)) {
    container.innerHTML = '<div class="empty">Documents index unavailable.</div>';
    return;
  }
  data.categories.forEach(cat => {
    const section = document.createElement('div');
    section.className = 'doc-category';
    const h = document.createElement('h3'); h.textContent = cat.title; section.appendChild(h);
    if (cat.description) {
      const d = document.createElement('div'); d.className = 'doc-category-pitch'; d.textContent = cat.description;
      section.appendChild(d);
    }
    if (!cat.items || cat.items.length === 0) {
      const empty = document.createElement('div'); empty.className = 'doc-item';
      empty.innerHTML = '<div class="doc-item-desc"><em>(no items yet)</em></div>';
      section.appendChild(empty);
    } else {
      // Group items by subcategory
      const bySub = {};
      cat.items.forEach(it => { const s = it.subcategory || ''; (bySub[s] = bySub[s] || []).push(it); });
      Object.entries(bySub).forEach(([sub, items]) => {
        const subWrap = document.createElement('div');
        subWrap.className = 'doc-sub';
        if (sub) {
          const subT = document.createElement('div');
          subT.className = 'doc-sub-title';
          subT.textContent = sub + '  (' + items.length + ')';
          subWrap.appendChild(subT);
        }
        items.forEach(it => subWrap.appendChild(renderDocItem(it)));
        section.appendChild(subWrap);
      });
    }
    container.appendChild(section);
  });
}

function renderDocItem(it) {
  const card = document.createElement('div');
  card.className = 'doc-item';
  const main = document.createElement('div');
  main.className = 'doc-item-main';
  const title = document.createElement('div');
  title.className = 'doc-item-title';
  title.textContent = it.title || '(untitled)';
  main.appendChild(title);
  if (it.description) {
    const d = document.createElement('div');
    d.className = 'doc-item-desc';
    d.textContent = it.description;
    main.appendChild(d);
  }
  const meta = document.createElement('div');
  meta.className = 'doc-item-meta';
  const bits = [];
  if (it.modified_iso) bits.push('modified ' + fmtDate(it.modified_iso));
  const extra = it.extra || {};
  if (extra.critic_score != null) bits.push('critic ' + extra.critic_score + '/5');
  if (extra.reviewed === false) bits.push('UNREVIEWED');
  else if (extra.reviewed === true) bits.push('reviewed');
  if (extra.outcome) bits.push(extra.outcome);
  if (extra.size_bytes != null) bits.push(fmtBytes(extra.size_bytes));
  meta.textContent = bits.join('  ·  ');
  main.appendChild(meta);
  card.appendChild(main);

  const actions = document.createElement('div');
  actions.className = 'doc-item-actions';
  if (it.link_type === 'external_url' && it.link) {
    const a = document.createElement('a');
    a.className = 'doc-link';
    a.href = it.link;
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = 'Open';
    actions.appendChild(a);
  } else if (it.link_type === 'local_file' && it.link) {
    const reveal = document.createElement('button');
    reveal.className = 'doc-reveal';
    reveal.textContent = 'Reveal';
    reveal.addEventListener('click', () => revealInFinder(it.link));
    actions.appendChild(reveal);
    const copyBtn = document.createElement('button');
    copyBtn.className = 'doc-copy-path';
    copyBtn.textContent = 'Copy path';
    copyBtn.addEventListener('click', () => {
      navigator.clipboard.writeText(it.link).then(() => flashDocsStatus('Path copied'));
    });
    actions.appendChild(copyBtn);
  }
  card.appendChild(actions);
  return card;
}

async function revealInFinder(path) {
  try {
    const res = await fetch('/api/reveal', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path}),
    });
    const j = await res.json();
    if (res.ok && j.ok) flashDocsStatus('Revealed in Finder');
    else flashDocsStatus('Reveal failed: ' + (j.error || res.status), true);
  } catch (e) {
    flashDocsStatus('Reveal error: ' + e.message, true);
  }
}

function flashDocsStatus(msg, isError=false) {
  const el = document.getElementById('documentsStatus');
  if (!el) return;
  el.textContent = msg;
  el.className = 'control-status' + (isError ? ' error' : ' ok');
  setTimeout(() => { el.textContent = ''; el.className = 'control-status'; }, 2000);
}

function fmtBytes(n) {
  if (n < 1024) return n + ' B';
  if (n < 1024*1024) return (n/1024).toFixed(0) + ' KB';
  return (n/1024/1024).toFixed(1) + ' MB';
}

async function loadDocuments() {
  try {
    const res = await fetch('/api/documents');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    renderDocuments(data);
  } catch (e) {
    document.getElementById('documentsBlock').innerHTML =
      '<div class="empty">Documents index error: ' + (e.message || e) + '</div>';
  }
}

/* ── Load control tab ─────────────────────────────────────────────────────── */
async function loadControl() {
  try {
    const res = await fetch('/api/settings');
    if (!res.ok) return;
    const cfg = await res.json();
    renderPresets(cfg.active_preset);
    renderJobModes(cfg.jobs || {});
    renderThresholds(cfg.thresholds || {});
    renderPersonality(cfg.personality || {});
  } catch (e) {
    document.getElementById('tab-control').innerHTML =
      '<div class="empty">Settings unavailable: ' + e.message + '</div>';
  }
}

/* ── Status dot + header ──────────────────────────────────────────────────── */

function updateStatus(generatedAt) {
  const dot = document.getElementById('statusDot');
  const text = document.getElementById('statusText');
  const genEl = document.getElementById('generatedAt');

  if (!generatedAt) {
    dot.classList.add('offline');
    text.textContent = 'No data';
    return;
  }

  const age = (Date.now() - new Date(generatedAt)) / 1000;
  if (age > 180) {
    dot.classList.add('offline');
    text.textContent = 'Stale';
  } else {
    dot.classList.remove('offline');
    text.textContent = 'Live';
  }

  genEl.textContent = 'as of ' + fmtTime(generatedAt);
}

/* ── Count plate items for badge ──────────────────────────────────────────── */

function countPlateItems(state) {
  const t = state.tasks || {};
  return [
    t.stalled_goals,
    t.meeting_briefs,
    t.drafts_unreviewed,
    t.review_invitations,
    t.hypotheses_pending,
    t.other_briefings,
  ].reduce((n, arr) => n + (Array.isArray(arr) ? arr.length : 0), 0);
}

/* ── Main state load ──────────────────────────────────────────────────────── */

async function loadState() {
  try {
    const res = await fetch('/api/state');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const state = await res.json();

    updateStatus(state.generated_at);

    // Backend shape: { tasks: {...}, recent_activity: {...}, scheduler_status: {...} }
    const tasks = state.tasks || {};
    const activity = state.recent_activity || {};

    // Plate tab
    renderStalledGoals(tasks.stalled_goals);
    renderMeetingBriefs(tasks.meeting_briefs);
    renderDraftsUnreviewed(tasks.drafts_unreviewed);
    renderReviewInvitations(tasks.review_invitations);
    renderHypothesesPending(tasks.hypotheses_pending);
    renderOtherBriefings(tasks.other_briefings);

    // Empty state
    const count = countPlateItems(state);
    document.getElementById('empty').hidden = count > 0;
    const badge = document.getElementById('plateBadge');
    badge.textContent = count > 0 ? String(count) : '';

    // Activity tab
    renderSchedulerBlock(state.scheduler_status);
    renderJobsTable(activity.last_24h_jobs);
    renderLedgerTable(activity.recent_ledger);
    renderCostBlock({ cost_24h_usd: activity.cost_24h_usd, retrieval_quality_7d_mean: activity.retrieval_quality_7d_mean });

  } catch (e) {
    const dot = document.getElementById('statusDot');
    const text = document.getElementById('statusText');
    dot.classList.add('offline');
    text.textContent = 'Offline — ' + e.message;
  }
}


/* ── Goals tab ────────────────────────────────────────────────────────────── */
const GOAL_STATUSES = ['proposed', 'active', 'paused', 'done', 'retired'];
const MILESTONE_STATUSES = ['open', 'in_progress', 'done', 'abandoned'];
const NAS_LEVELS = ['high', 'med', 'low'];
const TIME_HORIZONS = ['week', 'month', 'quarter', 'year', 'career'];

async function loadGoals() {
  try {
    const res = await fetch('/api/goals');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    renderGoals(data);
  } catch (e) {
    document.getElementById('goalsBlock').innerHTML =
      `<div class="empty">Goals unavailable: ${e.message}</div>`;
  }
}

let _showArchivedGoals = false;

function renderGoals(data) {
  const header = document.getElementById('goalsHeader');
  header.innerHTML = '';

  const allGoals = data.goals || [];
  const archived = allGoals.filter(g => g.status === 'retired' || g.status === 'done');
  const visible = _showArchivedGoals
    ? allGoals
    : allGoals.filter(g => g.status !== 'retired' && g.status !== 'done');

  const bar = document.createElement('div');
  bar.className = 'new-goal-bar';
  const info = document.createElement('span');
  info.textContent = `${visible.length} shown · ${archived.length} archived · sorted by importance + NAS relevance`;
  info.style.fontFamily = 'var(--font-mono)';
  info.style.fontSize = '12px';
  info.style.color = 'var(--ink-faint)';
  bar.appendChild(info);

  const toggleBtn = document.createElement('button');
  toggleBtn.className = 'btn';
  toggleBtn.style.marginLeft = 'auto';
  toggleBtn.textContent = _showArchivedGoals ? 'Hide archived' : `Show archived (${archived.length})`;
  toggleBtn.addEventListener('click', () => {
    _showArchivedGoals = !_showArchivedGoals;
    renderGoals(data);
  });
  bar.appendChild(toggleBtn);

  const newBtn = document.createElement('button');
  newBtn.className = 'btn btn-primary';
  newBtn.textContent = '+ new goal';
  newBtn.style.marginLeft = '8px';
  newBtn.addEventListener('click', createNewGoal);
  bar.appendChild(newBtn);
  header.appendChild(bar);

  const block = document.getElementById('goalsBlock');
  block.innerHTML = '';
  if (visible.length === 0) {
    block.innerHTML = _showArchivedGoals
      ? '<div class="empty">No goals yet. Click "+ new goal" to create one.</div>'
      : '<div class="empty">No active goals. Click "Show archived" to see retired/done ones, or "+ new goal" to create one.</div>';
    return;
  }
  visible.forEach(g => block.appendChild(renderGoalCard(g)));
}

function renderGoalCard(goal) {
  const card = document.createElement('div');
  card.className = 'goal-card status-' + (goal.status || 'active');
  card.dataset.goalId = goal.id;

  // Header row
  const header = document.createElement('div');
  header.className = 'goal-header';

  const nameEl = document.createElement('div');
  nameEl.className = 'goal-name-display';
  nameEl.textContent = goal.name || '(unnamed)';
  nameEl.title = 'Click to edit';
  nameEl.addEventListener('click', () => editInline(nameEl, goal, 'name'));
  header.appendChild(nameEl);

  const badges = document.createElement('div');
  badges.className = 'goal-badges';
  badges.style.display = 'flex'; badges.style.gap = '6px'; badges.style.marginLeft = 'auto';

  const impBadge = document.createElement('span');
  impBadge.className = 'importance-badge imp-' + (goal.importance || 3);
  impBadge.textContent = `★${goal.importance || '?'}`;
  badges.appendChild(impBadge);

  const relPill = document.createElement('span');
  relPill.className = 'relevance-pill rel-' + (goal.nas_relevance || 'med');
  relPill.textContent = goal.nas_relevance || 'med';
  badges.appendChild(relPill);

  const statusPill = document.createElement('span');
  statusPill.className = 'status-pill st-' + (goal.status || 'active');
  statusPill.textContent = goal.status || 'active';
  badges.appendChild(statusPill);

  header.appendChild(badges);

  const toggle = document.createElement('button');
  toggle.className = 'collapse-btn';
  toggle.textContent = '▸';
  toggle.addEventListener('click', () => {
    const body = card.querySelector('.goal-body');
    const isHidden = body.style.display === 'none';
    body.style.display = isHidden ? '' : 'none';
    toggle.textContent = isHidden ? '▾' : '▸';
  });
  header.appendChild(toggle);

  card.appendChild(header);

  // Meta
  const meta = document.createElement('div');
  meta.className = 'goal-meta';
  const bits = [];
  if (goal.time_horizon) bits.push('horizon: ' + goal.time_horizon);
  if (goal.days_since_touch != null) bits.push(`last touched ${goal.days_since_touch}d ago`);
  if (goal.owner) bits.push('owner: ' + goal.owner);
  meta.textContent = bits.join('  ·  ');
  card.appendChild(meta);

  // Body (editable fields)
  const body = document.createElement('div');
  body.className = 'goal-body';
  body.style.display = 'none';  // collapsed by default

  // Importance slider
  body.appendChild(makeFieldRow('Importance (1-5)',
    makeSlider('importance-' + goal.id, goal.importance || 3, 1, 5, v => saveGoalField(goal.id, 'importance', parseInt(v, 10)))));

  // NAS relevance
  body.appendChild(makeFieldRow('NAS relevance',
    makeSelect('rel-' + goal.id, NAS_LEVELS, goal.nas_relevance || 'med',
      v => saveGoalField(goal.id, 'nas_relevance', v))));

  // Status
  body.appendChild(makeFieldRow('Status',
    makeSelect('status-' + goal.id, GOAL_STATUSES, goal.status || 'active',
      v => saveGoalField(goal.id, 'status', v))));

  // Time horizon
  body.appendChild(makeFieldRow('Time horizon',
    makeSelect('horizon-' + goal.id, TIME_HORIZONS, goal.time_horizon || 'quarter',
      v => saveGoalField(goal.id, 'time_horizon', v))));

  // Success metric
  body.appendChild(makeFieldRow('Success metric',
    makeTextInput('metric-' + goal.id, goal.success_metric || '',
      v => saveGoalField(goal.id, 'success_metric', v))));

  // Why
  body.appendChild(makeFieldRow('Why', makeTextarea('why-' + goal.id, goal.why || '',
    v => saveGoalField(goal.id, 'why', v))));

  // Notes
  body.appendChild(makeFieldRow('Notes', makeTextarea('notes-' + goal.id, goal.notes || '',
    v => saveGoalField(goal.id, 'notes', v))));

  // Milestones
  const msBlock = document.createElement('div');
  msBlock.className = 'milestones-block';
  const msTitle = document.createElement('div');
  msTitle.className = 'milestones-title';
  msTitle.textContent = `Milestones (${(goal.milestones || []).length})`;
  msBlock.appendChild(msTitle);
  (goal.milestones || []).forEach(m => msBlock.appendChild(renderMilestoneRow(m, goal.id)));
  const addMsBtn = document.createElement('button');
  addMsBtn.className = 'btn';
  addMsBtn.textContent = '+ add milestone';
  addMsBtn.addEventListener('click', () => createNewMilestone(goal.id));
  msBlock.appendChild(addMsBtn);
  body.appendChild(msBlock);

  // Actions
  const actions = document.createElement('div');
  actions.className = 'goal-actions';
  actions.style.marginTop = '10px';
  const archiveBtn = document.createElement('button');
  archiveBtn.className = 'btn btn-danger';
  archiveBtn.textContent = 'Archive goal';
  archiveBtn.addEventListener('click', async () => {
    if (!confirm(`Archive goal "${goal.name}"? This sets status to 'retired'.`)) return;
    try {
      const res = await fetch('/api/goals/' + encodeURIComponent(goal.id), {method: 'DELETE'});
      if (!res.ok) throw new Error('HTTP ' + res.status);
      flashGoalsStatus('Archived');
      loadGoals();
    } catch (e) {
      flashGoalsStatus('Archive failed: ' + e.message, true);
    }
  });
  actions.appendChild(archiveBtn);
  body.appendChild(actions);

  card.appendChild(body);
  return card;
}

function renderMilestoneRow(m, goalId) {
  const row = document.createElement('div');
  row.className = 'milestone-row';
  row.dataset.milestoneId = m.id;

  const desc = document.createElement('input');
  desc.type = 'text';
  desc.className = 'field-input';
  desc.value = m.milestone || '';
  desc.style.flex = '1';
  desc.addEventListener('change', () => saveMilestoneField(m.id, 'milestone', desc.value));

  const status = makeSelect('ms-status-' + m.id, MILESTONE_STATUSES, m.status || 'open',
    v => saveMilestoneField(m.id, 'status', v));

  const target = document.createElement('input');
  target.type = 'date';
  target.className = 'field-input';
  target.value = (m.target_iso || '').slice(0, 10);
  target.style.width = '140px';
  target.addEventListener('change', () => saveMilestoneField(m.id, 'target_iso', target.value));

  const del = document.createElement('button');
  del.className = 'milestone-delete';
  del.textContent = 'delete';
  del.addEventListener('click', async () => {
    if (!confirm('Delete milestone?')) return;
    try {
      const res = await fetch('/api/milestones/' + encodeURIComponent(m.id), {method: 'DELETE'});
      if (!res.ok) throw new Error('HTTP ' + res.status);
      flashGoalsStatus('Deleted');
      loadGoals();
    } catch (e) { flashGoalsStatus('Delete failed: ' + e.message, true); }
  });

  row.style.display = 'flex';
  row.style.gap = '8px';
  row.style.alignItems = 'center';
  row.style.marginBottom = '6px';
  row.appendChild(desc);
  row.appendChild(status);
  row.appendChild(target);
  row.appendChild(del);
  return row;
}

// --- Edit helpers ---
const _saveDebouncers = {};
function debouncedSave(key, fn, ms = 500) {
  clearTimeout(_saveDebouncers[key]);
  _saveDebouncers[key] = setTimeout(fn, ms);
}

async function saveGoalField(goalId, field, value) {
  debouncedSave(`goal-${goalId}-${field}`, async () => {
    try {
      const res = await fetch('/api/goals/' + encodeURIComponent(goalId), {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({[field]: value}),
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      flashGoalsStatus('Saved');
    } catch (e) { flashGoalsStatus('Save failed: ' + e.message, true); }
  });
}

async function saveMilestoneField(msId, field, value) {
  debouncedSave(`ms-${msId}-${field}`, async () => {
    try {
      const res = await fetch('/api/milestones/' + encodeURIComponent(msId), {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({[field]: value}),
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      flashGoalsStatus('Saved');
    } catch (e) { flashGoalsStatus('Save failed: ' + e.message, true); }
  });
}

async function createNewGoal() {
  const name = prompt('Name for the new goal?');
  if (!name || !name.trim()) return;
  try {
    const res = await fetch('/api/goals', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name.trim()}),
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    flashGoalsStatus('Created');
    loadGoals();
  } catch (e) { flashGoalsStatus('Create failed: ' + e.message, true); }
}

async function createNewMilestone(goalId) {
  const milestone = prompt('Milestone description?');
  if (!milestone || !milestone.trim()) return;
  try {
    const res = await fetch('/api/milestones', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({goal_id: goalId, milestone: milestone.trim()}),
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    flashGoalsStatus('Added');
    loadGoals();
  } catch (e) { flashGoalsStatus('Add failed: ' + e.message, true); }
}

// --- Input factories ---
function makeFieldRow(label, input) {
  const row = document.createElement('div');
  row.className = 'field-row';
  const lab = document.createElement('label');
  lab.className = 'field-label';
  lab.textContent = label;
  row.appendChild(lab);
  row.appendChild(input);
  return row;
}

function makeTextInput(id, value, onChange) {
  const el = document.createElement('input');
  el.type = 'text'; el.id = id; el.className = 'field-input';
  el.value = value || '';
  el.addEventListener('input', () => onChange(el.value));
  return el;
}

function makeTextarea(id, value, onChange) {
  const el = document.createElement('textarea');
  el.id = id; el.className = 'field-textarea';
  el.value = value || '';
  el.addEventListener('input', () => onChange(el.value));
  return el;
}

function makeSelect(id, options, current, onChange) {
  const sel = document.createElement('select');
  sel.id = id; sel.className = 'select';
  options.forEach(o => {
    const opt = document.createElement('option');
    opt.value = o; opt.textContent = o;
    if (o === current) opt.selected = true;
    sel.appendChild(opt);
  });
  sel.addEventListener('change', () => onChange(sel.value));
  return sel;
}

function makeSlider(id, value, min, max, onChange) {
  const wrap = document.createElement('div');
  wrap.style.display = 'flex'; wrap.style.alignItems = 'center'; wrap.style.gap = '10px';
  const el = document.createElement('input');
  el.type = 'range'; el.id = id; el.className = 'slider-importance';
  el.min = String(min); el.max = String(max); el.value = String(value || min);
  const display = document.createElement('span');
  display.style.fontFamily = 'var(--font-mono)'; display.style.fontSize = '13px';
  display.textContent = el.value;
  el.addEventListener('input', () => { display.textContent = el.value; onChange(el.value); });
  wrap.appendChild(el); wrap.appendChild(display);
  return wrap;
}

function editInline(el, goal, field) {
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'goal-name-edit';
  input.value = goal[field] || '';
  input.style.width = '100%';
  el.replaceWith(input);
  input.focus();
  input.select();
  const commit = () => {
    const newVal = input.value.trim();
    if (newVal && newVal !== goal[field]) {
      saveGoalField(goal.id, field, newVal);
      goal[field] = newVal;
    }
    const restored = document.createElement('div');
    restored.className = 'goal-name-display';
    restored.textContent = newVal || '(unnamed)';
    restored.addEventListener('click', () => editInline(restored, goal, field));
    input.replaceWith(restored);
  };
  input.addEventListener('blur', commit);
  input.addEventListener('keydown', e => { if (e.key === 'Enter') commit(); });
}

function flashGoalsStatus(msg, isError=false) {
  const el = document.getElementById('goalsStatus');
  if (!el) return;
  el.textContent = msg;
  el.className = 'control-status' + (isError ? ' error' : ' ok');
  setTimeout(() => { el.textContent = ''; el.className = 'control-status'; }, 2000);
}

/* ── Knowledge Map tab ────────────────────────────────────────────────────── */

// State
let _kmStatusFilter = ['confirmed'];   // active status toggles
let _kmSearchQuery  = '';
let _kmData         = null;            // last loaded catalog response

const KIND_OPTIONS = [
  'research_project','google_doc','google_sheet','drive_folder','local_dir',
  'grant','github_repo','email_contact','external_url','other',
];

const DRIVE_KINDS = new Set(['google_doc','google_sheet','drive_folder','research_project']);

function flashKnowledgeStatus(msg, isError = false) {
  const el = document.getElementById('knowledgeStatus');
  if (!el) return;
  el.textContent = msg;
  el.className = 'control-status' + (isError ? ' error' : ' ok');
  setTimeout(() => { el.textContent = ''; el.className = 'control-status'; }, 2500);
}

async function loadKnowledge() {
  // Fetch all statuses so client-side filtering works, or fetch what's needed
  const statuses = _kmStatusFilter.length ? _kmStatusFilter.join(',') : 'confirmed';
  // We always fetch all statuses and filter client-side so toggles are instant
  try {
    const res = await fetch('/api/catalog?status=all');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    _kmData = await res.json();
    renderKnowledge(_kmData);
  } catch (e) {
    document.getElementById('knowledgeBlock').innerHTML =
      '<div class="empty">Knowledge Map unavailable: ' + (e.message || e) + '</div>';
  }
}

function renderKnowledge(data) {
  renderKnowledgeHeader(data);
  renderKnowledgeFilters(data);
  renderKnowledgeBlock(data);
}

function renderKnowledgeHeader(data) {
  const el = document.getElementById('knowledgeHeader');
  el.innerHTML = '';

  const bar = document.createElement('div');
  bar.className = 'km-header-bar';

  const counters = document.createElement('div');
  counters.className = 'km-counters';

  // Count confirmed items across all categories
  let confirmedCount = 0;
  (data.categories || []).forEach(cat => {
    (cat.items || []).forEach(it => { if (it.status === 'confirmed') confirmedCount++; });
  });

  const unc = data.unconfirmed_count || 0;
  const stale = data.stale_count || 0;

  counters.textContent = `${confirmedCount} confirmed`;
  if (unc > 0) {
    const b = document.createElement('span');
    b.className = 'km-counter-badge unconfirmed';
    b.textContent = `${unc} unconfirmed`;
    counters.appendChild(b);
  }
  if (stale > 0) {
    const b = document.createElement('span');
    b.className = 'km-counter-badge stale';
    b.textContent = `${stale} stale`;
    counters.appendChild(b);
  }
  bar.appendChild(counters);

  const addBtn = document.createElement('button');
  addBtn.className = 'btn btn-primary';
  addBtn.textContent = '+ Add resource';
  addBtn.style.marginLeft = 'auto';
  addBtn.addEventListener('click', () => {
    const existing = document.getElementById('km-add-form-wrap');
    if (existing) { existing.remove(); return; }
    renderAddForm();
  });
  bar.appendChild(addBtn);
  el.appendChild(bar);
}

function renderKnowledgeFilters(data) {
  const el = document.getElementById('knowledgeFilters');
  el.innerHTML = '';

  const bar = document.createElement('div');
  bar.className = 'km-filter-bar';

  // Count each status bucket from full data
  let proposedCount = 0, dismissedCount = 0;
  (data.categories || []).forEach(cat => {
    (cat.items || []).forEach(it => {
      if (it.status === 'proposed') proposedCount++;
      if (it.status === 'dismissed') dismissedCount++;
    });
  });

  const filters = [
    { key: 'confirmed',  label: 'Confirmed' },
    { key: 'proposed',   label: `Unconfirmed (${proposedCount})` },
    { key: 'dismissed',  label: 'Dismissed' },
  ];

  filters.forEach(({ key, label }) => {
    const btn = document.createElement('button');
    btn.className = 'km-filter-btn' + (_kmStatusFilter.includes(key) ? ' active' : '');
    btn.textContent = label;
    btn.addEventListener('click', () => {
      if (_kmStatusFilter.includes(key)) {
        _kmStatusFilter = _kmStatusFilter.filter(s => s !== key);
      } else {
        _kmStatusFilter.push(key);
      }
      if (_kmStatusFilter.length === 0) _kmStatusFilter = ['confirmed'];
      renderKnowledge(_kmData);
    });
    bar.appendChild(btn);
  });

  const search = document.createElement('input');
  search.type = 'search';
  search.className = 'km-search';
  search.placeholder = 'Search…';
  search.value = _kmSearchQuery;
  search.addEventListener('input', () => {
    _kmSearchQuery = search.value;
    renderKnowledgeBlock(_kmData);
  });
  bar.appendChild(search);

  el.appendChild(bar);
}

function kmItemMatchesFilter(it) {
  if (!_kmStatusFilter.includes(it.status)) return false;
  if (!_kmSearchQuery) return true;
  const q = _kmSearchQuery.toLowerCase();
  return (
    (it.display_name || '').toLowerCase().includes(q) ||
    (it.purpose || '').toLowerCase().includes(q) ||
    (it.handle || '').toLowerCase().includes(q) ||
    (it.tags || []).some(t => t.toLowerCase().includes(q))
  );
}

function renderKnowledgeBlock(data) {
  const block = document.getElementById('knowledgeBlock');
  block.innerHTML = '';

  (data.categories || []).forEach(cat => {
    const visibleItems = (cat.items || []).filter(kmItemMatchesFilter);
    const section = document.createElement('div');
    section.className = 'km-category' + (visibleItems.length === 0 ? ' empty-category' : '');

    const head = document.createElement('div');
    head.className = 'km-category-head';
    const title = document.createElement('span');
    title.className = 'km-category-title';
    title.textContent = cat.title;
    const cnt = document.createElement('span');
    cnt.className = 'km-category-count';
    cnt.textContent = `(${visibleItems.length})`;
    head.appendChild(title);
    head.appendChild(cnt);
    section.appendChild(head);

    if (cat.description) {
      const desc = document.createElement('div');
      desc.className = 'km-category-desc';
      desc.textContent = cat.description;
      section.appendChild(desc);
    }

    if (visibleItems.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'km-empty';
      empty.textContent = 'Nothing here yet.';
      section.appendChild(empty);
    } else {
      visibleItems.forEach(it => section.appendChild(renderKmItem(it)));
    }

    block.appendChild(section);
  });

  // Add-form placeholder anchor
  const anchor = document.createElement('div');
  anchor.id = 'km-add-form-anchor';
  block.appendChild(anchor);
}

function renderKmItem(it) {
  const card = document.createElement('div');
  card.className = `km-item kind-${it.kind || 'other'}`;
  card.dataset.resId = it.id;

  // Name row
  const nameRow = document.createElement('div');
  nameRow.className = 'km-item-name-row';

  const pill = document.createElement('span');
  pill.className = `km-kind-pill kind-${it.kind || 'other'}`;
  pill.textContent = (it.kind || 'other').replace(/_/g, ' ');
  nameRow.appendChild(pill);

  const nameEl = document.createElement('div');
  nameEl.className = 'km-item-name';
  // If handle is a URL-ish string, make it linkable
  const isUrl = /^https?:\/\//i.test(it.handle || '');
  if (isUrl) {
    const a = document.createElement('a');
    a.href = it.handle;
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = it.display_name || it.handle;
    nameEl.appendChild(a);
  } else {
    nameEl.textContent = it.display_name || it.handle || '(unnamed)';
  }
  nameRow.appendChild(nameEl);
  card.appendChild(nameRow);

  // Purpose
  if (it.purpose) {
    const p = document.createElement('div');
    p.className = 'km-item-purpose';
    p.textContent = it.purpose;
    card.appendChild(p);
  }

  // Handle row
  if (it.handle) {
    const handleRow = document.createElement('div');
    handleRow.className = 'km-handle-row';
    const h = document.createElement('span');
    h.className = 'km-handle';
    h.textContent = it.handle;
    h.title = it.handle;
    handleRow.appendChild(h);

    const copyBtn = document.createElement('button');
    copyBtn.className = 'km-handle-copy';
    copyBtn.textContent = 'copy';
    copyBtn.addEventListener('click', () => {
      navigator.clipboard.writeText(it.handle).then(() => flashKnowledgeStatus('Copied'));
    });
    handleRow.appendChild(copyBtn);

    // Drive/Docs open link
    if (DRIVE_KINDS.has(it.kind) && isUrl) {
      const link = document.createElement('a');
      link.className = 'km-handle-link';
      link.href = it.handle;
      link.target = '_blank';
      link.rel = 'noopener';
      link.textContent = 'Open in Drive \u2197';
      handleRow.appendChild(link);
    }
    card.appendChild(handleRow);
  }

  // Tags + linked projects
  const tags = it.tags || [];
  const projects = it.linked_project_ids || [];
  if (tags.length || projects.length) {
    const pillsRow = document.createElement('div');
    pillsRow.className = 'km-pills-row';
    tags.forEach(t => {
      const s = document.createElement('span');
      s.className = 'km-tag';
      s.textContent = t;
      pillsRow.appendChild(s);
    });
    projects.forEach(pid => {
      const s = document.createElement('span');
      s.className = 'km-project-id';
      s.textContent = pid;
      pillsRow.appendChild(s);
    });
    card.appendChild(pillsRow);
  }

  // Action row
  const actionRow = document.createElement('div');
  actionRow.className = 'km-action-row';

  if (it.status === 'proposed') {
    const confirmBtn = document.createElement('button');
    confirmBtn.className = 'km-btn km-btn-confirm';
    confirmBtn.textContent = 'Confirm';
    confirmBtn.addEventListener('click', async () => {
      try {
        const res = await fetch(`/api/catalog/${encodeURIComponent(it.id)}/confirm`, { method: 'POST' });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const badge = document.createElement('span');
        badge.className = 'km-confirmed-badge';
        badge.textContent = '\u2713 confirmed';
        actionRow.innerHTML = '';
        actionRow.appendChild(badge);
        // refresh quietly in background
        flashKnowledgeStatus('Confirmed');
        loadKnowledge();
      } catch (e) { flashKnowledgeStatus('Error: ' + e.message, true); }
    });
    actionRow.appendChild(confirmBtn);
  } else if (it.status === 'confirmed') {
    // no Confirm button for already-confirmed items
  }

  const editBtn = document.createElement('button');
  editBtn.className = 'km-btn';
  editBtn.textContent = 'Edit';
  editBtn.addEventListener('click', () => showEditForm(it, card));
  actionRow.appendChild(editBtn);

  if (it.status !== 'dismissed') {
    const dismissBtn = document.createElement('button');
    dismissBtn.className = 'km-btn km-btn-dismiss';
    dismissBtn.textContent = 'Dismiss';
    dismissBtn.addEventListener('click', async () => {
      if (!confirm(`Dismiss "${it.display_name}"?`)) return;
      try {
        const res = await fetch(`/api/catalog/${encodeURIComponent(it.id)}`, { method: 'DELETE' });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        flashKnowledgeStatus('Dismissed');
        loadKnowledge();
      } catch (e) { flashKnowledgeStatus('Error: ' + e.message, true); }
    });
    actionRow.appendChild(dismissBtn);
  }

  card.appendChild(actionRow);

  // Meta line
  const meta = document.createElement('div');
  meta.className = 'km-item-meta';
  const bits = [];
  if (it.proposed_by) bits.push('proposed by ' + it.proposed_by);
  if (it.last_confirmed_iso) {
    const d = new Date(it.last_confirmed_iso);
    const days = Math.floor((Date.now() - d) / 86400000);
    bits.push('last confirmed ' + (days === 0 ? 'today' : days + 'd ago'));
  }
  if (it.status === 'dismissed') bits.push('dismissed');
  meta.textContent = bits.join(' · ');
  card.appendChild(meta);

  return card;
}

function showEditForm(it, card) {
  // If an edit form is already showing for this card, remove it
  const existing = card.querySelector('.km-edit-form');
  if (existing) { existing.remove(); return; }

  const form = document.createElement('div');
  form.className = 'km-edit-form';

  function makeRow(label, inputEl) {
    const row = document.createElement('div');
    row.className = 'km-form-row';
    const lbl = document.createElement('div');
    lbl.className = 'km-form-label';
    lbl.textContent = label;
    row.appendChild(lbl);
    row.appendChild(inputEl);
    return row;
  }

  function mkInput(val) {
    const el = document.createElement('input');
    el.type = 'text';
    el.className = 'km-form-input';
    el.value = val || '';
    return el;
  }

  function mkTextarea(val) {
    const el = document.createElement('textarea');
    el.className = 'km-form-textarea';
    el.value = val || '';
    return el;
  }

  function mkSelect(val) {
    const sel = document.createElement('select');
    sel.className = 'km-form-select';
    KIND_OPTIONS.forEach(k => {
      const o = document.createElement('option');
      o.value = k; o.textContent = k.replace(/_/g,' ');
      if (k === val) o.selected = true;
      sel.appendChild(o);
    });
    return sel;
  }

  const kindSel = mkSelect(it.kind);
  const handleIn = mkInput(it.handle);
  const nameIn = mkInput(it.display_name);
  const purposeIn = mkInput(it.purpose);
  const tagsIn = mkInput((it.tags || []).join(', '));
  const projIn = mkInput((it.linked_project_ids || []).join(', '));
  const notesIn = mkTextarea(it.notes);

  form.appendChild(makeRow('Kind', kindSel));
  form.appendChild(makeRow('Handle', handleIn));
  form.appendChild(makeRow('Display name', nameIn));
  form.appendChild(makeRow('Purpose', purposeIn));
  form.appendChild(makeRow('Tags', tagsIn));
  form.appendChild(makeRow('Projects', projIn));
  form.appendChild(makeRow('Notes', notesIn));

  const actRow = document.createElement('div');
  actRow.className = 'km-form-actions';

  const saveBtn = document.createElement('button');
  saveBtn.className = 'btn btn-primary';
  saveBtn.textContent = 'Save';
  saveBtn.addEventListener('click', async () => {
    const payload = {
      kind: kindSel.value,
      handle: handleIn.value.trim(),
      display_name: nameIn.value.trim(),
      purpose: purposeIn.value.trim(),
      tags: tagsIn.value.split(',').map(s => s.trim()).filter(Boolean),
      linked_project_ids: projIn.value.split(',').map(s => s.trim()).filter(Boolean),
      notes: notesIn.value.trim(),
    };
    try {
      const res = await fetch(`/api/catalog/${encodeURIComponent(it.id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      flashKnowledgeStatus('Saved');
      form.remove();
      loadKnowledge();
    } catch (e) { flashKnowledgeStatus('Save failed: ' + e.message, true); }
  });

  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'btn';
  cancelBtn.textContent = 'Cancel';
  cancelBtn.addEventListener('click', () => form.remove());

  actRow.appendChild(cancelBtn);
  actRow.appendChild(saveBtn);
  form.appendChild(actRow);

  card.appendChild(form);
}

function renderAddForm() {
  const anchor = document.getElementById('km-add-form-anchor');
  if (!anchor) return;

  const wrap = document.createElement('div');
  wrap.className = 'km-add-form-wrap';
  wrap.id = 'km-add-form-wrap';

  const title = document.createElement('div');
  title.className = 'km-add-form-title';
  title.textContent = 'Add resource';
  wrap.appendChild(title);

  function makeRow(label, inputEl) {
    const row = document.createElement('div');
    row.className = 'km-form-row';
    const lbl = document.createElement('div');
    lbl.className = 'km-form-label';
    lbl.textContent = label;
    row.appendChild(lbl);
    row.appendChild(inputEl);
    return row;
  }

  function mkInput(placeholder) {
    const el = document.createElement('input');
    el.type = 'text';
    el.className = 'km-form-input';
    el.placeholder = placeholder || '';
    return el;
  }

  function mkTextarea(placeholder) {
    const el = document.createElement('textarea');
    el.className = 'km-form-textarea';
    el.placeholder = placeholder || '';
    return el;
  }

  function mkSelect() {
    const sel = document.createElement('select');
    sel.className = 'km-form-select';
    KIND_OPTIONS.forEach(k => {
      const o = document.createElement('option');
      o.value = k; o.textContent = k.replace(/_/g,' ');
      sel.appendChild(o);
    });
    return sel;
  }

  const kindSel = mkSelect();
  const handleIn = mkInput('URL, path, or identifier');
  const nameIn = mkInput('Display name');
  const purposeIn = mkInput('What this is for');
  const tagsIn = mkInput('tag1, tag2');
  const projIn = mkInput('p_001, p_002');
  const notesIn = mkTextarea('Optional notes');

  wrap.appendChild(makeRow('Kind', kindSel));
  wrap.appendChild(makeRow('Handle', handleIn));
  wrap.appendChild(makeRow('Display name', nameIn));
  wrap.appendChild(makeRow('Purpose', purposeIn));
  wrap.appendChild(makeRow('Tags', tagsIn));
  wrap.appendChild(makeRow('Projects', projIn));
  wrap.appendChild(makeRow('Notes', notesIn));

  const actRow = document.createElement('div');
  actRow.className = 'km-form-actions';

  const saveBtn = document.createElement('button');
  saveBtn.className = 'btn btn-primary';
  saveBtn.textContent = 'Save';
  saveBtn.addEventListener('click', async () => {
    const dn = nameIn.value.trim();
    const handle = handleIn.value.trim();
    if (!dn || !handle) {
      flashKnowledgeStatus('Display name and handle are required', true);
      return;
    }
    const payload = {
      kind: kindSel.value,
      handle,
      display_name: dn,
      purpose: purposeIn.value.trim(),
      tags: tagsIn.value.split(',').map(s => s.trim()).filter(Boolean),
      linked_project_ids: projIn.value.split(',').map(s => s.trim()).filter(Boolean),
      notes: notesIn.value.trim(),
    };
    try {
      const res = await fetch('/api/catalog', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      flashKnowledgeStatus('Created');
      wrap.remove();
      loadKnowledge();
    } catch (e) { flashKnowledgeStatus('Create failed: ' + e.message, true); }
  });

  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'btn';
  cancelBtn.textContent = 'Cancel';
  cancelBtn.addEventListener('click', () => wrap.remove());

  actRow.appendChild(cancelBtn);
  actRow.appendChild(saveBtn);
  wrap.appendChild(actRow);

  anchor.parentNode.insertBefore(wrap, anchor);
  nameIn.focus();
}

/* ── Projects tab ─────────────────────────────────────────────────────────── */

let _projectsCache = null;
let _projectsStatusFilter = new Set(['active']);
let _projectsTypeFilter = new Set(['paper','grant','software','database','teaching','general','other','none']);
let _projectsSort = 'last_touched_iso';
let _projectsSearch = '';

async function loadProjects() {
  try {
    const res = await fetch('/api/projects?status=all');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    _projectsCache = data;
    renderProjects();
  } catch (e) {
    document.getElementById('projectsBlock').innerHTML =
      `<div class="empty">Projects unavailable: ${e.message}</div>`;
  }
}

function renderProjects() {
  if (!_projectsCache) return;
  const data = _projectsCache;
  const students = data.students || [];

  // --- Header: counters + add-new ---
  const header = document.getElementById('projectsHeader');
  header.innerHTML = '';
  const counts = {active: 0, paused: 0, done: 0, archived: 0};
  (data.projects || []).forEach(p => {
    if (counts[p.status] !== undefined) counts[p.status]++;
  });
  const countLine = document.createElement('div');
  countLine.style.display = 'flex';
  countLine.style.gap = '16px';
  countLine.style.alignItems = 'center';
  countLine.innerHTML = `<span style="font-family:var(--font-mono);font-size:12px;color:var(--ink-faint)">
    ${counts.active} active · ${counts.paused} paused · ${counts.done} done · ${counts.archived} archived
  </span>`;
  const newBtn = document.createElement('button');
  newBtn.className = 'btn btn-primary';
  newBtn.textContent = '+ new project';
  newBtn.style.marginLeft = 'auto';
  newBtn.addEventListener('click', createNewProject);
  countLine.appendChild(newBtn);

  const autoBtn = document.createElement('button');
  autoBtn.className = 'btn';
  autoBtn.textContent = 'Auto-classify all';
  autoBtn.title = 'Guess project_type for projects without one (by name keywords)';
  autoBtn.style.marginLeft = '6px';
  autoBtn.addEventListener('click', async () => {
    if (!confirm('Auto-classify all untyped projects by name? Any you disagree with, just edit afterward.')) return;
    try {
      const res = await fetch('/api/projects/auto_classify', {method: 'POST'});
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const j = await res.json();
      flashProjectsStatus(`Classified ${j.classified || 0}`);
      loadProjects();
    } catch (e) { flashProjectsStatus('Error: ' + e.message, true); }
  });
  countLine.appendChild(autoBtn);
  header.appendChild(countLine);

  // --- Filters: status toggles + search + sort ---
  const filters = document.getElementById('projectsFilters');
  filters.innerHTML = '';
  const fbar = document.createElement('div');
  fbar.style.display = 'flex'; fbar.style.gap = '10px';
  fbar.style.alignItems = 'center'; fbar.style.flexWrap = 'wrap';
  ['active','paused','done','archived'].forEach(s => {
    const btn = document.createElement('button');
    btn.className = 'btn';
    btn.textContent = `${s} (${counts[s]})`;
    if (_projectsStatusFilter.has(s)) btn.classList.add('btn-primary');
    btn.addEventListener('click', () => {
      if (_projectsStatusFilter.has(s)) _projectsStatusFilter.delete(s);
      else _projectsStatusFilter.add(s);
      if (_projectsStatusFilter.size === 0) _projectsStatusFilter.add('active');
      renderProjects();
    });
    fbar.appendChild(btn);
  });
  const search = document.createElement('input');
  search.type = 'text';
  search.placeholder = 'search…';
  search.className = 'field-input';
  search.style.flex = '0 0 200px';
  search.value = _projectsSearch;
  search.addEventListener('input', () => { _projectsSearch = search.value; renderProjects(); });
  fbar.appendChild(search);
  const sortSel = document.createElement('select');
  sortSel.className = 'select';
  [['last_touched_iso','Last activity'],['name','Name'],['status','Status'],['lead_name','Lead']]
    .forEach(([v,l]) => {
      const o = document.createElement('option'); o.value=v; o.textContent='sort: '+l;
      if (v === _projectsSort) o.selected = true;
      sortSel.appendChild(o);
    });
  sortSel.addEventListener('change', () => { _projectsSort = sortSel.value; renderProjects(); });
  fbar.appendChild(sortSel);
  filters.appendChild(fbar);

  // --- Type filter row ---
  const typeRow = document.createElement('div');
  typeRow.className = 'proj-type-filter-row';
  const typeLbl = document.createElement('span');
  typeLbl.style.fontFamily = 'var(--font-mono)'; typeLbl.style.fontSize = '11px';
  typeLbl.style.color = 'var(--ink-faint)'; typeLbl.style.textTransform = 'uppercase';
  typeLbl.style.letterSpacing = '0.06em'; typeLbl.style.whiteSpace = 'nowrap';
  typeLbl.textContent = 'type:';
  typeRow.appendChild(typeLbl);
  ['paper','grant','software','database','teaching','general','other','none'].forEach(t => {
    const tb = document.createElement('button');
    tb.className = 'btn';
    tb.textContent = t;
    if (_projectsTypeFilter.has(t)) tb.classList.add('btn-primary');
    tb.addEventListener('click', () => {
      if (_projectsTypeFilter.has(t)) _projectsTypeFilter.delete(t);
      else _projectsTypeFilter.add(t);
      if (_projectsTypeFilter.size === 0) _projectsTypeFilter.add('none');
      renderProjects();
    });
    typeRow.appendChild(tb);
  });
  filters.appendChild(typeRow);

  // --- Filter + sort the list ---
  let projects = (data.projects || []).filter(p => _projectsStatusFilter.has(p.status));
  projects = projects.filter(p => {
    const t = p.project_type || 'none';
    return _projectsTypeFilter.has(t);
  });
  if (_projectsSearch) {
    const q = _projectsSearch.toLowerCase();
    projects = projects.filter(p => {
      const hay = [p.name, p.description, p.lead_name, p.current_hypothesis, p.next_action, p.keywords]
        .filter(Boolean).join(' ').toLowerCase();
      return hay.includes(q);
    });
  }
  const statusOrder = {active:0, paused:1, done:2, archived:3};
  projects.sort((a,b) => {
    if (_projectsSort === 'name') return (a.name||'').localeCompare(b.name||'');
    if (_projectsSort === 'status') return (statusOrder[a.status]||9) - (statusOrder[b.status]||9);
    if (_projectsSort === 'lead_name') return (a.lead_name||'~').localeCompare(b.lead_name||'~');
    // default: last_touched_iso desc (nulls last)
    const av = a.last_touched_iso || ''; const bv = b.last_touched_iso || '';
    return bv.localeCompare(av);
  });

  // --- Render rows ---
  const block = document.getElementById('projectsBlock');
  block.innerHTML = '';
  if (projects.length === 0) {
    block.innerHTML = '<div class="empty">No projects match the current filters.</div>';
    return;
  }
  const journals = data.journals || [];
  // Column header strip (matches renderProjectRow grid)
  const head = document.createElement('div');
  head.style.display = 'grid';
  head.style.gridTemplateColumns = 'auto minmax(240px,2fr) auto minmax(110px,auto) minmax(170px,auto) minmax(170px,auto) minmax(170px,auto) auto';
  head.style.gap = '10px';
  head.style.alignItems = 'center';
  head.style.padding = '6px 10px';
  head.style.borderBottom = '1px solid var(--rule)';
  head.style.fontFamily = 'var(--font-mono)';
  head.style.fontSize = '11px';
  head.style.color = 'var(--ink-faint)';
  head.style.textTransform = 'uppercase';
  head.style.letterSpacing = '0.06em';
  ['Wiki', 'Project', 'Type', 'Status', 'Stage', 'Lead', 'Target journal', ''].forEach(label => {
    const c = document.createElement('span');
    c.textContent = label;
    head.appendChild(c);
  });
  block.appendChild(head);
  projects.forEach(p => block.appendChild(renderProjectRow(p, students, journals)));
}

function renderProjectRow(p, students, journals) {
  const row = document.createElement('div');
  row.className = 'project-row status-' + (p.status || 'active');
  row.dataset.projectId = p.id;

  const header = document.createElement('div');
  header.className = 'project-header';
  header.style.display = 'grid';
  header.style.gridTemplateColumns = 'auto minmax(240px,2fr) auto minmax(110px,auto) minmax(170px,auto) minmax(170px,auto) minmax(170px,auto) auto';
  header.style.gap = '10px';
  header.style.alignItems = 'center';

  // 1. Include checkbox (controls public projects-page visibility)
  const incWrap = document.createElement('div');
  incWrap.title = 'Include on the public projects page';
  incWrap.style.display = 'flex'; incWrap.style.justifyContent = 'center';
  const incBox = document.createElement('input');
  incBox.type = 'checkbox';
  incBox.checked = !!p.include_in_wiki;
  incBox.style.cursor = 'pointer';
  incBox.addEventListener('change', () => saveProjectField(p.id, {include_in_wiki: incBox.checked ? 1 : 0}));
  incWrap.appendChild(incBox);
  header.appendChild(incWrap);

  // 2. Name + warning + activity chips
  const nameCell = document.createElement('div');
  const nameWrap = document.createElement('div');
  nameWrap.style.cursor = 'pointer';
  const nameEl = document.createElement('span');
  nameEl.className = 'project-name';
  nameEl.textContent = p.name || '(unnamed)';
  nameWrap.appendChild(nameEl);
  if (!p.next_action || !p.next_action.trim()) {
    const warn = document.createElement('span');
    warn.textContent = ' ⚠ no next action';
    warn.style.fontSize = '11px'; warn.style.fontFamily = 'var(--font-mono)';
    warn.style.color = '#b85c00'; warn.style.marginLeft = '8px';
    nameWrap.appendChild(warn);
  }
  nameWrap.addEventListener('click', () => toggleProjectExpanded(row, p, students));
  nameCell.appendChild(nameWrap);

  const ra = p.recent_activity || {};
  const chips = [];
  if (ra.unreviewed_drafts > 0) chips.push([`📝 ${ra.unreviewed_drafts}`, '#b85c00', `${ra.unreviewed_drafts} unreviewed drafts`]);
  if (ra.pending_hypotheses > 0) chips.push([`🧪 ${ra.pending_hypotheses}`, '#2d7a7a', `${ra.pending_hypotheses} pending hypotheses`]);
  if (ra.literature_notes_last_30d > 0) chips.push([`📚 ${ra.literature_notes_last_30d}`, '#3a5d9a', `${ra.literature_notes_last_30d} literature notes (30d)`]);
  if (ra.ledger_entries_last_30d > 0) chips.push([`📊 ${ra.ledger_entries_last_30d}`, 'var(--ink-faint)', `${ra.ledger_entries_last_30d} ledger entries (30d)`]);
  if (p.days_since_touch != null) {
    const d = p.days_since_touch;
    const label = d < 1 ? 'today' : d < 14 ? `${d}d` : d < 60 ? `${Math.round(d/7)}w` : `${Math.round(d/30)}mo`;
    const color = d < 7 ? '#2d7a2d' : d < 30 ? 'var(--ink-faint)' : '#c53030';
    chips.push([`✎ ${label}`, color, `Last touched ${label}`]);
  }
  if (chips.length > 0) {
    const chipsDiv = document.createElement('div');
    chipsDiv.style.display = 'flex'; chipsDiv.style.gap = '4px'; chipsDiv.style.flexWrap = 'wrap';
    chipsDiv.style.marginTop = '4px';
    chips.forEach(([text, color, title]) => {
      const c = document.createElement('span');
      c.style.fontFamily = 'var(--font-mono)'; c.style.fontSize = '10px';
      c.style.padding = '1px 5px'; c.style.background = 'var(--paper-sunk)';
      c.style.border = '1px solid var(--rule)'; c.style.borderRadius = '3px';
      c.style.color = color;
      c.textContent = text; c.title = title;
      chipsDiv.appendChild(c);
    });
    nameCell.appendChild(chipsDiv);
  }
  header.appendChild(nameCell);

  // 3. Type pill
  const ptype = p.project_type || null;
  const typePill = document.createElement('span');
  typePill.className = 'project-type-pill ' + (ptype ? 'type-' + ptype : 'type-none');
  typePill.textContent = ptype || '(no type)';
  header.appendChild(typePill);

  // 4. Status dropdown
  header.appendChild(projSelect(p.id, 'status', ['active','paused','done','archived'], p.status || 'active'));

  // 5. Stage dropdown (lifecycle)
  header.appendChild(projStageSelect(p.id, p.stage || ''));

  // 6. Lead dropdown
  header.appendChild(projLeadSelect(p.id, p.lead_student_id, p.lead_name, students));

  // 7. Target-journal dropdown (paper-type projects only; placeholder otherwise)
  if (ptype === 'paper') {
    header.appendChild(projJournalSelect(p.id, p.journal || '', journals));
  } else {
    const ph = document.createElement('span');
    ph.style.color = 'var(--ink-faint)';
    ph.style.fontSize = '11px';
    ph.style.fontFamily = 'var(--font-mono)';
    ph.style.textAlign = 'center';
    ph.textContent = '—';
    header.appendChild(ph);
  }

  // 8. Actions
  const actions = document.createElement('div');
  actions.style.display = 'flex'; actions.style.gap = '4px';
  const editBtn = document.createElement('button');
  editBtn.className = 'btn'; editBtn.textContent = '✎';
  editBtn.title = 'Open detail editor';
  editBtn.addEventListener('click', (e) => { e.stopPropagation(); toggleProjectExpanded(row, p, students); });
  actions.appendChild(editBtn);
  if (p.linked_artifact_url) {
    const a = document.createElement('a');
    a.href = p.linked_artifact_url; a.target = '_blank'; a.rel = 'noopener';
    a.className = 'btn'; a.textContent = '↗'; a.title = 'Open linked doc';
    actions.appendChild(a);
  }
  if (p.data_dir_url) {
    const a = document.createElement('a');
    a.href = p.data_dir_url; a.target = '_blank'; a.rel = 'noopener';
    a.className = 'btn'; a.textContent = '🗂'; a.title = 'Open data dir';
    actions.appendChild(a);
  }
  header.appendChild(actions);

  row.appendChild(header);
  return row;
}

function toggleProjectExpanded(row, p, students) {
  const existing = row.querySelector('.project-expanded');
  if (existing) { existing.remove(); return; }
  const body = document.createElement('div');
  body.className = 'project-expanded';
  body.style.marginTop = '12px'; body.style.paddingTop = '12px';
  body.style.borderTop = '1px dashed var(--rule)';
  body.style.display = 'grid';
  body.style.gridTemplateColumns = 'minmax(120px, 160px) 1fr';
  body.style.gap = '8px 12px';

  const mk = (label, input) => {
    const l = document.createElement('label');
    l.className = 'field-label'; l.textContent = label;
    body.appendChild(l);
    body.appendChild(input);
  };

  // Name
  mk('Name', projTextInput(p.id, 'name', p.name || ''));
  // Status
  mk('Status', projSelect(p.id, 'status', ['active','paused','done','archived'], p.status || 'active'));

  // Type dropdown (always shown) — custom handler to re-render card on change
  const typeSel = document.createElement('select');
  typeSel.className = 'select';
  ['','paper','grant','software','database','teaching','general','other'].forEach(o => {
    const op = document.createElement('option'); op.value = o; op.textContent = o || '(no type)';
    if (o === (p.project_type || '')) op.selected = true;
    typeSel.appendChild(op);
  });
  typeSel.addEventListener('change', () => {
    const newType = typeSel.value || null;
    saveProjectField(p.id, {project_type: newType});
    // Re-render expanded card after debounced save + cache update
    setTimeout(() => {
      const targetRow = document.querySelector('.project-row[data-project-id="' + p.id + '"]');
      if (targetRow) {
        targetRow.querySelector('.project-expanded')?.remove();
        const updated = _projectsCache && _projectsCache.projects.find(x => x.id === p.id);
        if (updated) toggleProjectExpanded(targetRow, updated, students);
      }
    }, 600);
  });
  mk('Type', typeSel);

  // Conditional paper fields
  if ((p.project_type || null) === 'paper') {
    mk('Journal', projTextInput(p.id, 'journal', p.journal || ''));
    mk('Paper status', projSelect(p.id, 'paper_status',
      ['','submitted','under_review','revision_resubmit','revision_new_journal','accepted','in_press','published'],
      p.paper_status || ''));
  }

  // Conditional grant fields
  if ((p.project_type || null) === 'grant') {
    mk('Agency', projTextInput(p.id, 'agency', p.agency || ''));
    mk('Program', projTextInput(p.id, 'program', p.program || ''));
    mk('Grant status', projSelect(p.id, 'grant_status',
      ['','in_prep','submitted','under_review','awarded','declined','deferred'],
      p.grant_status || ''));
  }

  // Lead (dropdown of students + unassigned)
  const leadSel = document.createElement('select');
  leadSel.className = 'select';
  const unassigned = document.createElement('option');
  unassigned.value = ''; unassigned.textContent = '(unassigned)';
  if (!p.lead_student_id && !p.lead_name) unassigned.selected = true;
  leadSel.appendChild(unassigned);
  (students || []).forEach(s => {
    const o = document.createElement('option');
    o.value = 'sid:' + s.id;
    o.textContent = `${s.full_name} (${s.role})`;
    if (p.lead_student_id === s.id) o.selected = true;
    leadSel.appendChild(o);
  });
  // Add Heath + free-text options
  const heath = document.createElement('option');
  heath.value = 'name:Heath'; heath.textContent = 'Heath';
  if ((p.lead_name || '').toLowerCase() === 'heath') heath.selected = true;
  leadSel.appendChild(heath);
  const other = document.createElement('option');
  other.value = '__custom__'; other.textContent = '(other — prompt for name)';
  leadSel.appendChild(other);
  leadSel.addEventListener('change', () => {
    const v = leadSel.value;
    if (v === '__custom__') {
      const name = prompt('Lead name?');
      if (name) {
        saveProjectField(p.id, {lead_student_id: null, lead_name: name.trim()});
      }
      return;
    }
    if (v === '') saveProjectField(p.id, {lead_student_id: null, lead_name: ''});
    else if (v.startsWith('sid:')) saveProjectField(p.id, {lead_student_id: parseInt(v.slice(4),10), lead_name: ''});
    else if (v.startsWith('name:')) saveProjectField(p.id, {lead_student_id: null, lead_name: v.slice(5)});
  });
  mk('Lead', leadSel);

  mk('Current hypothesis', projTextarea(p.id, 'current_hypothesis', p.current_hypothesis || ''));
  mk('Next action', projTextInput(p.id, 'next_action', p.next_action || ''));
  mk('Keywords', projTextInput(p.id, 'keywords', p.keywords || ''));
  mk('Data dir', projTextInput(p.id, 'data_dir', p.data_dir || ''));
  mk('Linked doc ID', projTextInput(p.id, 'linked_artifact_id', p.linked_artifact_id || ''));
  mk('Linked goals', projTextInput(p.id, 'linked_goal_ids', p.linked_goal_ids || ''));
  mk('Notes', projTextarea(p.id, 'notes', p.notes || ''));
  mk('Description', projTextarea(p.id, 'description', p.description || ''));

  row.appendChild(body);
}

function projTextInput(id, field, value) {
  const el = document.createElement('input');
  el.type = 'text'; el.className = 'field-input'; el.value = value;
  el.addEventListener('input', () => saveProjectField(id, {[field]: el.value}));
  return el;
}
function projTextarea(id, field, value) {
  const el = document.createElement('textarea');
  el.className = 'field-textarea'; el.value = value;
  el.addEventListener('input', () => saveProjectField(id, {[field]: el.value}));
  return el;
}
function projSelect(id, field, options, current) {
  const el = document.createElement('select'); el.className = 'select';
  options.forEach(o => {
    const op = document.createElement('option'); op.value = o; op.textContent = o;
    if (o === current) op.selected = true;
    el.appendChild(op);
  });
  el.addEventListener('change', () => saveProjectField(id, {[field]: el.value}));
  return el;
}

const _STAGE_LABELS = {
  '': '(no stage)',
  'in_development': 'In development',
  'data_collection': 'Data collection',
  'finalizing_manuscript': 'Finalizing manuscript',
  'under_review': 'Under review',
  'published': 'Published',
};
const _STAGE_ORDER = ['', 'in_development', 'data_collection', 'finalizing_manuscript', 'under_review', 'published'];

function projStageSelect(id, current) {
  const el = document.createElement('select'); el.className = 'select';
  _STAGE_ORDER.forEach(v => {
    const op = document.createElement('option');
    op.value = v; op.textContent = _STAGE_LABELS[v];
    if (v === current) op.selected = true;
    el.appendChild(op);
  });
  el.addEventListener('change', () => {
    saveProjectField(id, {stage: el.value || null});
    // 'published' filters the row off the public projects page; reload to reflect counts
    if (el.value === 'published') setTimeout(() => loadProjects(), 700);
  });
  return el;
}

function projLeadSelect(id, currentStudentId, currentName, students) {
  const el = document.createElement('select'); el.className = 'select';
  const unassigned = document.createElement('option');
  unassigned.value = ''; unassigned.textContent = '(unassigned)';
  if (!currentStudentId && !currentName) unassigned.selected = true;
  el.appendChild(unassigned);
  let matchedStudent = false;
  (students || []).forEach(s => {
    const o = document.createElement('option');
    o.value = 'sid:' + s.id;
    o.textContent = `${s.full_name} (${s.role})`;
    if (currentStudentId === s.id) { o.selected = true; matchedStudent = true; }
    el.appendChild(o);
  });
  const heath = document.createElement('option');
  heath.value = 'name:Heath'; heath.textContent = 'Heath';
  if (!matchedStudent && (currentName || '').toLowerCase() === 'heath') heath.selected = true;
  el.appendChild(heath);
  // Custom name fallback (lead_name set, not a known student, not Heath)
  if (!matchedStudent && currentName && currentName.toLowerCase() !== 'heath') {
    const custom = document.createElement('option');
    custom.value = 'name:' + currentName;
    custom.textContent = currentName + ' (custom)';
    custom.selected = true;
    el.appendChild(custom);
  }
  const other = document.createElement('option');
  other.value = '__custom__'; other.textContent = '(other — write-in)';
  el.appendChild(other);
  el.addEventListener('change', () => {
    const v = el.value;
    if (v === '__custom__') {
      const name = prompt('Lead name?');
      if (name && name.trim()) {
        saveProjectField(id, {lead_student_id: null, lead_name: name.trim()});
        setTimeout(() => loadProjects(), 700);
      } else {
        el.value = '';
      }
      return;
    }
    if (v === '') saveProjectField(id, {lead_student_id: null, lead_name: ''});
    else if (v.startsWith('sid:')) saveProjectField(id, {lead_student_id: parseInt(v.slice(4),10), lead_name: ''});
    else if (v.startsWith('name:')) saveProjectField(id, {lead_student_id: null, lead_name: v.slice(5)});
  });
  return el;
}

function projJournalSelect(id, current, journals) {
  const el = document.createElement('select'); el.className = 'select';
  const empty = document.createElement('option');
  empty.value = ''; empty.textContent = '(no journal)';
  if (!current) empty.selected = true;
  el.appendChild(empty);
  const known = journals || [];
  known.forEach(j => {
    const o = document.createElement('option');
    o.value = j; o.textContent = j;
    if (j === current) o.selected = true;
    el.appendChild(o);
  });
  if (current && !known.includes(current)) {
    const custom = document.createElement('option');
    custom.value = current; custom.textContent = current + ' (custom)';
    custom.selected = true;
    el.appendChild(custom);
  }
  const other = document.createElement('option');
  other.value = '__custom__'; other.textContent = 'Other (write-in)…';
  el.appendChild(other);
  el.addEventListener('change', () => {
    const v = el.value;
    if (v === '__custom__') {
      const name = prompt('Journal name?');
      if (name && name.trim()) {
        saveProjectField(id, {journal: name.trim()});
        setTimeout(() => loadProjects(), 700);
      } else {
        el.value = current || '';
      }
      return;
    }
    saveProjectField(id, {journal: v || null});
  });
  return el;
}

const _projectSavers = {};
function saveProjectField(projectId, body) {
  clearTimeout(_projectSavers[projectId]);
  _projectSavers[projectId] = setTimeout(async () => {
    try {
      const res = await fetch('/api/projects/' + encodeURIComponent(projectId), {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      flashProjectsStatus('Saved');
      // Update cache so counters/chips refresh without full reload
      const updated = await res.json();
      if (_projectsCache && updated && updated.id) {
        const idx = _projectsCache.projects.findIndex(x => x.id === updated.id);
        if (idx >= 0) _projectsCache.projects[idx] = updated;
      }
    } catch (e) { flashProjectsStatus('Save failed: ' + e.message, true); }
  }, 500);
}

async function createNewProject() {
  const name = prompt('Name for the new project?');
  if (!name || !name.trim()) return;
  try {
    const res = await fetch('/api/projects', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name.trim()}),
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    flashProjectsStatus('Created');
    loadProjects();
  } catch (e) { flashProjectsStatus('Create failed: ' + e.message, true); }
}

function flashProjectsStatus(msg, isError=false) {
  const el = document.getElementById('projectsStatus');
  if (!el) return;
  el.textContent = msg;
  el.className = 'control-status' + (isError ? ' error' : ' ok');
  setTimeout(() => { el.textContent = ''; el.className = 'control-status'; }, 2000);
}

/* ── Inbox tab — unified feedback queue ──────────────────────────────────── */

function flashInboxStatus(msg, isError=false) {
  const el = document.getElementById('unifiedInboxStatus');
  if (!el) return;
  el.textContent = msg;
  el.className = 'control-status' + (isError ? ' error' : ' ok');
  setTimeout(() => { el.textContent = ''; el.className = 'control-status'; }, 2500);
}

/* Optimistic action: POST /api/action, hide the card on success.  Errors
   restore the card and flash a message — no wait for state.json to refresh. */
async function inboxAction(card, payload) {
  const err = card ? card.querySelector('.inline-error') : null;
  if (err) err.textContent = '';
  // Visual: dim the card immediately so the click feels instant
  if (card) { card.style.opacity = '0.4'; card.style.pointerEvents = 'none'; }
  try {
    const res = await fetch('/api/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) {
      const msg = (data && (data.error || data.detail)) || ('HTTP ' + res.status);
      flashInboxStatus(`${payload.action}: ${String(msg).slice(0, 160)}`, true);
      if (card) { card.style.opacity = '1'; card.style.pointerEvents = ''; }
      if (err) err.textContent = msg;
      return false;
    }
    flashInboxStatus(`${payload.action.replace(/_/g,' ')} ✓`);
    if (card) card.remove();
    return true;
  } catch (e) {
    flashInboxStatus(`network: ${e.message}`, true);
    if (card) { card.style.opacity = '1'; card.style.pointerEvents = ''; }
    return false;
  }
}

/* ── Prereg Ledger tab ────────────────────────────────────────────────────── */

function _verdictBadge(verdict) {
  if (!verdict) return '<span class="badge" style="background:var(--ink-faint)">pending</span>';
  const colors = {
    supported: '#2a9d58',
    refuted:   '#c0392b',
    null:      '#7f8c8d',
    aborted:   '#e67e22',
    pending:   '#999',
  };
  const v = verdict.toLowerCase();
  const bg = colors[v] || '#999';
  return `<span class="badge" style="background:${bg};color:#fff;padding:2px 6px;border-radius:4px;font-size:11px">${v}</span>`;
}

async function loadPrereg() {
  const hdr = document.getElementById('preregHeader');
  const block = document.getElementById('preregBlock');
  const status = document.getElementById('preregStatus');
  hdr.innerHTML = '';
  block.innerHTML = '';
  status.textContent = '';

  // Header bar
  const h2 = document.createElement('h2');
  h2.textContent = 'Prereg Ledger';
  h2.style.marginBottom = '4px';
  hdr.appendChild(h2);
  const sub = document.createElement('p');
  sub.style.color = 'var(--ink-faint)';
  sub.style.fontSize = '13px';
  sub.style.marginTop = '0';
  sub.textContent =
    'Preregistrations generated Monday 04:00 CT · adjudicated automatically at T+7 days · verdict is deterministic (p-value + direction, no LLM judge)';
  hdr.appendChild(sub);

  const bar = document.createElement('div');
  bar.className = 'actions';
  bar.appendChild(makeBtn('Refresh', 'btn-primary', () => loadPrereg()));
  hdr.appendChild(bar);

  try {
    const res = await fetch('/api/prereg_ledger');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    const rows = data.rows || [];

    if (rows.length === 0) {
      block.innerHTML = '<div class="empty">No preregistrations yet. The first one publishes Monday 04:00 CT.</div>';
      return;
    }

    // Build table
    const wrap = document.createElement('div');
    wrap.className = 'section';
    wrap.style.overflowX = 'auto';

    const table = document.createElement('table');
    table.style.width = '100%';
    table.style.borderCollapse = 'collapse';
    table.style.fontSize = '13px';

    // Header row
    const thead = document.createElement('thead');
    thead.innerHTML = `<tr style="border-bottom:2px solid var(--border)">
      <th style="text-align:left;padding:6px 8px;font-weight:600">Date prereg'd</th>
      <th style="text-align:left;padding:6px 8px;font-weight:600">Hypothesis</th>
      <th style="text-align:left;padding:6px 8px;font-weight:600">DB</th>
      <th style="text-align:left;padding:6px 8px;font-weight:600">Test</th>
      <th style="text-align:left;padding:6px 8px;font-weight:600">p-thr</th>
      <th style="text-align:left;padding:6px 8px;font-weight:600">T+7 verdict</th>
      <th style="text-align:left;padding:6px 8px;font-weight:600">Adjudicated</th>
    </tr>`;
    table.appendChild(thead);

    const tbody = document.createElement('tbody');
    rows.forEach((r, i) => {
      const tr = document.createElement('tr');
      tr.style.borderBottom = '1px solid var(--border)';
      tr.style.background = i % 2 === 0 ? 'transparent' : 'rgba(0,0,0,0.015)';

      // Truncate hypothesis text
      const hyp = (r.hypothesis_md || '').replace(/\n/g, ' ');
      const hypShort = hyp.length > 120 ? hyp.slice(0, 120) + '…' : hyp;

      const adjDate = r.adjudicated_at ? fmtDate(r.adjudicated_at) : '—';

      tr.innerHTML = `
        <td style="padding:6px 8px;white-space:nowrap;color:var(--ink-faint)">${fmtDate(r.prereg_published_at)}</td>
        <td style="padding:6px 8px;max-width:320px" title="${(r.hypothesis_md||'').replace(/"/g,'&quot;')}">${renderMd(hypShort)}</td>
        <td style="padding:6px 8px;font-family:var(--font-mono);font-size:11px">${r.db_name || '—'}</td>
        <td style="padding:6px 8px;font-family:var(--font-mono);font-size:11px">${r.test_name || '—'}</td>
        <td style="padding:6px 8px;font-family:var(--font-mono);font-size:11px">${r.p_threshold != null ? r.p_threshold : '—'}</td>
        <td style="padding:6px 8px">${_verdictBadge(r.adjudication)}</td>
        <td style="padding:6px 8px;white-space:nowrap;color:var(--ink-faint)">${adjDate}</td>
      `;

      // Expandable rationale row
      tr.style.cursor = 'pointer';
      tr.addEventListener('click', () => {
        const existing = document.getElementById('prereg-expand-' + r.id);
        if (existing) { existing.remove(); return; }
        if (!r.adjudication_rationale_md && !r.prereg_md) return;
        const detail = document.createElement('tr');
        detail.id = 'prereg-expand-' + r.id;
        detail.innerHTML = `<td colspan="7" style="padding:12px 16px;background:var(--bg-subtle,#f9f9f9);border-bottom:1px solid var(--border)">
          ${r.adjudication_rationale_md
            ? `<strong>Adjudication rationale:</strong><br>${renderMd(r.adjudication_rationale_md)}`
            : ''}
          ${r.prereg_md
            ? `<details style="margin-top:8px"><summary style="cursor:pointer;font-weight:600">Prereg block</summary><div style="margin-top:6px">${renderMd(r.prereg_md)}</div></details>`
            : ''}
        </td>`;
        tr.after(detail);
      });

      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);

    const count = document.createElement('p');
    count.style.fontSize = '12px';
    count.style.color = 'var(--ink-faint)';
    count.style.marginTop = '8px';
    const supported = rows.filter(r => r.adjudication === 'supported').length;
    const refuted   = rows.filter(r => r.adjudication === 'refuted').length;
    const nullr     = rows.filter(r => r.adjudication === 'null').length;
    const pending   = rows.filter(r => !r.adjudication).length;
    count.textContent =
      `${rows.length} total · ${supported} supported · ${refuted} refuted · ${nullr} null · ${pending} pending`;
    wrap.appendChild(count);
    block.appendChild(wrap);

  } catch (e) {
    status.textContent = 'Load failed: ' + e.message;
  }
}

/* ══════════════════════════════════════════════════════════════════════════
   INBOX DETAIL MODAL
   ══════════════════════════════════════════════════════════════════════════ */

/* Returns the single modal DOM node, ensuring it has the inner box.
 *
 * Idempotent. Handles three states:
 *   1. No #inboxDetailModal exists      → create it + the inner #inboxDetailBox
 *   2. #inboxDetailModal exists, has box → return as-is (re-applies styles defensively)
 *   3. #inboxDetailModal exists, NO box  → add the box (this is the case where the
 *      page's HTML preloads an empty placeholder div with id="inboxDetailModal";
 *      previously this caused getElementById('inboxDetailBox') to return null and
 *      blow up openInboxModal — silent failure of the entire feedback loop).
 */
function _getInboxModal() {
  let m = document.getElementById('inboxDetailModal');
  const created = !m;
  if (created) {
    m = document.createElement('div');
    m.id = 'inboxDetailModal';
    document.body.appendChild(m);
  }
  // Apply outer modal styles unconditionally — idempotent and inexpensive.
  m.style.cssText = [
    'display:none',          // initial; openInboxModal flips to 'flex' on show
    'position:fixed',
    'inset:0',
    'z-index:9000',
    'background:rgba(0,0,0,0.55)',
    'align-items:flex-start',
    'justify-content:center',
    'padding:40px 16px',
    'overflow-y:auto',
  ].join(';');

  // Ensure the inner box exists.
  let box = m.querySelector('#inboxDetailBox');
  if (!box) {
    box = document.createElement('div');
    box.id = 'inboxDetailBox';
    box.style.cssText = [
      'background:var(--paper-raised,#fff)',
      'border:1px solid var(--rule,#d8ccba)',
      'border-radius:8px',
      'width:100%',
      'max-width:760px',
      'padding:24px 28px',
      'position:relative',
      'box-shadow:0 8px 32px rgba(0,0,0,0.18)',
    ].join(';');
    m.appendChild(box);
  }

  // Attach the backdrop-click listener exactly once.
  if (!m.__backdropClickWired) {
    m.addEventListener('click', (e) => { if (e.target === m) closeInboxModal(); });
    m.__backdropClickWired = true;
  }

  return m;
}

function closeInboxModal() {
  const m = document.getElementById('inboxDetailModal');
  if (m) { m.style.display = 'none'; m.style.alignItems = ''; }
}

/* Open the modal and populate it for a given unified-inbox item + its card. */
function openInboxModal(item, card) {
  const m = _getInboxModal();
  const box = document.getElementById('inboxDetailBox');
  box.innerHTML = '';

  // ── Close button ──────────────────────────────────────────────────────
  const closeBtn = document.createElement('button');
  closeBtn.textContent = '×';
  closeBtn.title = 'Close';
  closeBtn.style.cssText = [
    'position:absolute',
    'top:14px',
    'right:16px',
    'background:none',
    'border:none',
    'font-size:22px',
    'line-height:1',
    'cursor:pointer',
    'color:var(--ink-faint,#8b7a72)',
    'padding:0 4px',
  ].join(';');
  closeBtn.addEventListener('click', closeInboxModal);
  box.appendChild(closeBtn);

  // ── Header ────────────────────────────────────────────────────────────
  const kindBadge = document.createElement('span');
  kindBadge.style.cssText = `background:${KIND_COLOR[item.kind] || '#64748b'};color:#fff;font-size:10px;font-weight:700;padding:2px 7px;border-radius:4px;letter-spacing:.5px;margin-right:8px`;
  kindBadge.textContent = KIND_LABEL[item.kind] || (item.kind || '').toUpperCase();

  const titleEl = document.createElement('h2');
  titleEl.style.cssText = 'font-size:1.15rem;margin:0 0 4px 0;display:flex;align-items:center;gap:6px;flex-wrap:wrap;padding-right:28px';
  titleEl.appendChild(kindBadge);
  titleEl.appendChild(document.createTextNode(item.title || '(untitled)'));
  box.appendChild(titleEl);

  // ── Metadata ──────────────────────────────────────────────────────────
  const metaParts = [
    item.created_at ? fmtDate(item.created_at) : '',
    item.urgency != null ? `urgency ${item.urgency}/5` : '',
  ].filter(Boolean);
  if (metaParts.length) {
    const metaEl = document.createElement('div');
    metaEl.style.cssText = 'font-size:12px;color:var(--ink-faint,#8b7a72);margin-bottom:14px';
    metaEl.textContent = metaParts.join(' · ');
    box.appendChild(metaEl);
  }

  // ── Content ───────────────────────────────────────────────────────────
  if (item.content_md) {
    const pre = document.createElement('pre');
    pre.style.cssText = [
      'font-family:var(--font-mono,monospace)',
      'font-size:12px',
      'line-height:1.55',
      'white-space:pre-wrap',
      'word-break:break-word',
      'background:var(--paper-sunk,#f3ece0)',
      'border:1px solid var(--rule,#d8ccba)',
      'border-radius:6px',
      'padding:14px 16px',
      'max-height:400px',
      'overflow-y:auto',
      'margin-bottom:18px',
      'color:var(--ink,#1e1713)',
    ].join(';');
    pre.textContent = item.content_md;
    box.appendChild(pre);
  }

  // ── Error display ─────────────────────────────────────────────────────
  const errEl = document.createElement('div');
  errEl.className = 'inline-error';
  errEl.style.cssText = 'margin-bottom:8px';
  box.appendChild(errEl);

  // Helper: run action, close modal & remove card on success
  const doAction = async (payload) => {
    errEl.textContent = '';
    const ok = await inboxAction(null, payload);
    if (ok) {
      closeInboxModal();
      if (card) card.remove();
    } else {
      errEl.textContent = 'Action failed — see status bar';
    }
  };

  // ── Quality rating row ────────────────────────────────────────────────
  // Universal across kinds. Good/OK/Bad writes a preference_signals row,
  // updates output_ledger.user_action for kind=ledger, and dismisses from
  // the inbox queue. This is the actual feedback channel that teaches
  // Tealc what the user values — paper_radar, grant_radar, and the
  // weekly_hypothesis_generator reranker prompts read these signals.
  const rateRow = document.createElement('div');
  rateRow.style.cssText = 'display:flex;align-items:center;gap:8px;margin-bottom:12px;flex-wrap:wrap';
  const rateLabel = document.createElement('span');
  rateLabel.style.cssText = 'font-size:12px;color:var(--ink-faint,#8b7a72)';
  rateLabel.textContent = 'Was this useful?';
  rateRow.appendChild(rateLabel);

  const RATING_BUTTONS = [
    ['Good',  'good', '#16a34a'],  // green
    ['OK',    'ok',   '#94a3b8'],  // slate
    ['Bad',   'bad',  '#dc2626'],  // red
  ];
  RATING_BUTTONS.forEach(([label, rating, color]) => {
    const btn = document.createElement('button');
    btn.className = 'btn';
    btn.style.cssText = `background:${color};color:#fff;border-color:${color};font-size:12px;padding:4px 12px;font-weight:600`;
    btn.textContent = label;
    btn.addEventListener('click', () => doAction({
      action: 'rate_inbox_item', kind: item.kind, target_id: item.id, rating,
    }));
    rateRow.appendChild(btn);
  });
  box.appendChild(rateRow);

  // ── Action buttons ────────────────────────────────────────────────────
  const actionsDiv = document.createElement('div');
  actionsDiv.style.cssText = 'display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px';

  // Per-kind PRIMARY actions (workflow-state changes — distinct from rating).
  // For 'ledger' kind there is no separate workflow action; Good/OK/Bad
  // above is the only feedback path. (Pre-2026-05-02 there was a "Mark
  // reviewed" button here that wrote a dismissal but no quality signal —
  // removed because Good/OK/Bad supersedes it.)
  if (item.kind === 'ledger') {
    // No additional action — Good/OK/Bad above is the full feedback.

  } else if (item.kind === 'hypothesis') {
    // target_id for hypothesis actions is the integer part (strip "hyp_" prefix)
    const hypId = String(item.id).replace(/^hyp_/i, '');
    const adoptBtn = document.createElement('button');
    adoptBtn.className = 'btn btn-primary';
    adoptBtn.textContent = 'Adopt';
    adoptBtn.addEventListener('click', () => doAction({ action: 'adopt_hypothesis', target_id: hypId }));
    actionsDiv.appendChild(adoptBtn);

    const rejectBtn = document.createElement('button');
    rejectBtn.className = 'btn';
    rejectBtn.textContent = 'Reject';
    rejectBtn.addEventListener('click', () => {
      const reason = window.prompt('Reject reason?') || 'out of lab scope';
      doAction({ action: 'reject_hypothesis', target_id: hypId, reason });
    });
    actionsDiv.appendChild(rejectBtn);

  } else if (item.kind === 'briefing') {
    // target_id for briefing actions strips "briefing_" prefix
    const briefId = String(item.id).replace(/^briefing_/i, '');
    const ackBtn = document.createElement('button');
    ackBtn.className = 'btn btn-primary';
    ackBtn.textContent = 'Acknowledge';
    ackBtn.addEventListener('click', () => doAction({ action: 'complete_briefing', target_id: briefId }));
    actionsDiv.appendChild(ackBtn);
  }

  // Dismiss with reason — all kinds
  const dismissBtn = document.createElement('button');
  dismissBtn.className = 'btn';
  dismissBtn.textContent = 'Dismiss with reason';
  dismissBtn.addEventListener('click', () => {
    // Swap button for an inline reason-picker inside the modal
    const picker = document.createElement('div');
    picker.style.cssText = 'display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:12px';
    const label = document.createElement('span');
    label.style.cssText = 'color:var(--ink-faint,#8b7a72)';
    label.textContent = 'Why?';
    picker.appendChild(label);

    const reasons = _dismissReasonsFor(item.kind);
    reasons.forEach(r => {
      const chip = document.createElement('button');
      chip.className = 'btn';
      chip.style.cssText = 'font-size:11px;padding:2px 8px';
      chip.textContent = r;
      chip.addEventListener('click', () => doAction({
        action: 'dismiss_inbox_item', kind: item.kind, target_id: item.id, reason: r,
      }));
      picker.appendChild(chip);
    });
    const other = document.createElement('button');
    other.className = 'btn';
    other.style.cssText = 'font-size:11px;padding:2px 8px';
    other.textContent = 'other…';
    other.addEventListener('click', () => {
      const txt = window.prompt('Dismiss reason?') || '';
      if (txt.trim()) doAction({
        action: 'dismiss_inbox_item', kind: item.kind, target_id: item.id, reason: txt.trim(),
      });
    });
    picker.appendChild(other);
    const cancel = document.createElement('button');
    cancel.className = 'btn';
    cancel.style.cssText = 'font-size:11px;padding:2px 8px;color:var(--ink-faint,#8b7a72)';
    cancel.textContent = '×';
    cancel.addEventListener('click', () => picker.replaceWith(dismissBtn));
    picker.appendChild(cancel);
    dismissBtn.replaceWith(picker);
  });
  actionsDiv.appendChild(dismissBtn);

  // Open in chat — placeholder stub
  const chatBtn = document.createElement('button');
  chatBtn.className = 'btn';
  chatBtn.textContent = 'Open in chat';
  chatBtn.style.cssText = 'color:var(--ink-faint,#8b7a72)';
  chatBtn.addEventListener('click', () => alert('Coming soon — chat routing not yet wired.'));
  actionsDiv.appendChild(chatBtn);

  box.appendChild(actionsDiv);

  // Show modal
  m.style.display = 'flex';
  m.style.alignItems = 'flex-start';

  // ESC to close
  const onKey = (e) => { if (e.key === 'Escape') { closeInboxModal(); document.removeEventListener('keydown', onKey); } };
  document.addEventListener('keydown', onKey);
}

/* ══════════════════════════════════════════════════════════════════════════
   UNIFIED INBOX TAB
   ══════════════════════════════════════════════════════════════════════════ */

const KIND_COLOR = {
  draft:               '#d97706',  // amber
  hypothesis:          '#7c3aed',  // violet
  grant:               '#059669',  // emerald
  analysis:            '#2563eb',  // blue
  ledger:              '#0891b2',  // cyan
  prereg:              '#9333ea',  // purple
  reviewer_invitation: '#dc2626',  // red
  briefing:            '#64748b',  // slate
  intention:           '#94a3b8',  // light slate
};

const KIND_LABEL = {
  draft:               'DRAFT',
  hypothesis:          'HYPOTHESIS',
  grant:               'GRANT',
  analysis:            'ANALYSIS',
  ledger:              'LEDGER',
  prereg:              'PREREG',
  reviewer_invitation: 'REVIEWER INV.',
  briefing:            'BRIEFING',
  intention:           'INTENTION',
};

const URGENCY_COLOR = {
  5: '#dc2626',  // red
  4: '#ea580c',  // orange
  3: '#ca8a04',  // yellow
  2: '#64748b',  // slate
  1: '#94a3b8',  // light
};

/* Per-kind dismiss-reason chips. Selected chip → preference_signals row →
   grant_radar Haiku prompt next run, closing the triage-improvement loop. */
const DISMISS_REASONS = {
  grant: [
    'not relevant',
    'wet-lab focus',
    'wrong career stage',
    'PI eligibility',
    'deadline too soon',
    'too small',
    'duplicate',
  ],
  hypothesis: [
    'not novel',
    'flawed logic',
    'out of lab scope',
    'too speculative',
    'already tested',
  ],
  briefing: [
    'not actionable',
    'already handled',
    'low priority',
  ],
  draft: [
    'wrong angle',
    'rewrite needed',
    'not ready',
  ],
  prereg: [
    'not testable',
    'wrong DB',
    'flawed test',
  ],
  ledger: [
    'low quality',
    'duplicate',
    'not interesting',
  ],
  intention: [
    'completed',
    'no longer relevant',
    'deferred',
  ],
  reviewer_invitation: [
    'wrong domain',
    'not now',
  ],
  analysis: [
    'flawed method',
    'low quality',
    'duplicate',
  ],
};
const DISMISS_REASONS_DEFAULT = ['not relevant', 'low priority', 'duplicate'];
function _dismissReasonsFor(kind) {
  return DISMISS_REASONS[kind] || DISMISS_REASONS_DEFAULT;
}

/* Active filter: null = All, else a kind string */
let _inboxFilterKind = null;
let _inboxData = null;

function _relativeAge(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  const days = Math.floor((Date.now() - d) / 86400000);
  if (days === 0) return 'today';
  if (days === 1) return '1 day ago';
  if (days < 30) return `${days} days ago`;
  const months = Math.floor(days / 30);
  return `${months} month${months > 1 ? 's' : ''} ago`;
}

function renderUnifiedInboxBanner(summary) {
  const el = document.getElementById('unifiedInboxBanner');
  el.innerHTML = '';

  const total = summary.total_pending || 0;
  const high  = summary.high_urgency_count || 0;

  const wrap = document.createElement('div');
  wrap.style.cssText = 'display:flex;align-items:center;gap:16px;flex-wrap:wrap;padding:8px 0';

  const numEl = document.createElement('span');
  numEl.style.cssText = `font-size:2.4rem;font-weight:700;color:${total > 10 ? '#dc2626' : total >= 5 ? '#d97706' : '#16a34a'}`;
  numEl.textContent = String(total);
  wrap.appendChild(numEl);

  const labelEl = document.createElement('span');
  labelEl.style.cssText = 'font-size:1.1rem;color:var(--ink-faint)';
  labelEl.textContent = total === 1 ? 'item awaiting your review' : 'items awaiting your review';
  wrap.appendChild(labelEl);

  if (high > 0) {
    const highEl = document.createElement('span');
    highEl.style.cssText = 'background:#dc2626;color:#fff;padding:2px 10px;border-radius:12px;font-size:13px;font-weight:600';
    highEl.textContent = `${high} high urgency`;
    wrap.appendChild(highEl);
  }

  const refreshBtn = document.createElement('button');
  refreshBtn.className = 'btn btn-primary';
  refreshBtn.style.marginLeft = 'auto';
  refreshBtn.textContent = 'Refresh';
  refreshBtn.addEventListener('click', () => loadUnifiedInbox());
  wrap.appendChild(refreshBtn);

  el.appendChild(wrap);
}

function renderUnifiedInboxFilters(byKind) {
  const el = document.getElementById('unifiedInboxFilters');
  el.innerHTML = '';

  const bar = document.createElement('div');
  bar.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;padding:4px 0';

  const allBtn = document.createElement('button');
  allBtn.className = 'km-filter-btn' + (_inboxFilterKind === null ? ' active' : '');
  allBtn.textContent = 'All';
  allBtn.addEventListener('click', () => { _inboxFilterKind = null; renderUnifiedInboxCards(_inboxData); renderUnifiedInboxFilters(byKind); });
  bar.appendChild(allBtn);

  const highBtn = document.createElement('button');
  highBtn.className = 'km-filter-btn' + (_inboxFilterKind === '__high__' ? ' active' : '');
  highBtn.textContent = 'High urgency only';
  highBtn.addEventListener('click', () => { _inboxFilterKind = '__high__'; renderUnifiedInboxCards(_inboxData); renderUnifiedInboxFilters(byKind); });
  bar.appendChild(highBtn);

  Object.entries(byKind).forEach(([kind, count]) => {
    const btn = document.createElement('button');
    btn.className = 'km-filter-btn' + (_inboxFilterKind === kind ? ' active' : '');
    btn.textContent = `${KIND_LABEL[kind] || kind} (${count})`;
    btn.style.borderColor = KIND_COLOR[kind] || '#888';
    btn.addEventListener('click', () => { _inboxFilterKind = kind; renderUnifiedInboxCards(_inboxData); renderUnifiedInboxFilters(byKind); });
    bar.appendChild(btn);
  });

  el.appendChild(bar);
}

function renderUnifiedInboxCards(data) {
  const cardsEl = document.getElementById('unifiedInboxCards');
  const emptyEl = document.getElementById('unifiedInboxEmpty');
  cardsEl.innerHTML = '';

  const items = (data && data.items) || [];
  let visible = items;
  if (_inboxFilterKind === '__high__') {
    visible = items.filter(i => (i.urgency || 0) >= 4);
  } else if (_inboxFilterKind) {
    visible = items.filter(i => i.kind === _inboxFilterKind);
  }

  emptyEl.hidden = visible.length > 0;

  visible.forEach(item => {
    const card = document.createElement('div');
    card.className = 'card';
    card.style.cssText = 'margin-bottom:10px;padding:14px 16px;position:relative';

    // Header row
    const hdr = document.createElement('div');
    hdr.style.cssText = 'display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap';

    // Urgency dot
    const dot = document.createElement('span');
    const urgency = item.urgency || 1;
    dot.style.cssText = `width:10px;height:10px;border-radius:50%;display:inline-block;flex-shrink:0;background:${URGENCY_COLOR[urgency] || '#aaa'}`;
    dot.title = `Urgency ${urgency}/5`;
    hdr.appendChild(dot);

    // Kind badge
    const badge = document.createElement('span');
    badge.style.cssText = `background:${KIND_COLOR[item.kind] || '#64748b'};color:#fff;font-size:10px;font-weight:700;padding:2px 7px;border-radius:4px;letter-spacing:.5px`;
    badge.textContent = KIND_LABEL[item.kind] || (item.kind || '').toUpperCase();
    hdr.appendChild(badge);

    // Title
    const title = document.createElement('strong');
    title.style.cssText = 'font-size:14px;flex:1;min-width:0';
    title.textContent = item.title || '(untitled)';
    hdr.appendChild(title);

    card.appendChild(hdr);

    // Summary
    if (item.summary) {
      const sumEl = document.createElement('div');
      sumEl.style.cssText = 'font-size:13px;color:var(--ink-faint);margin-bottom:6px;white-space:pre-wrap';
      sumEl.textContent = item.summary.slice(0, 200) + (item.summary.length > 200 ? '…' : '');
      card.appendChild(sumEl);
    }

    // Meta row
    const meta = document.createElement('div');
    meta.style.cssText = 'font-size:12px;color:var(--ink-faint);margin-bottom:8px';
    const age = _relativeAge(item.created_at);
    meta.textContent = [
      item.created_at ? fmtDate(item.created_at) : '',
      age ? `(${age})` : '',
    ].filter(Boolean).join(' ');
    card.appendChild(meta);

    // Action hint
    if (item.action_hint) {
      const hint = document.createElement('div');
      hint.style.cssText = 'font-size:12px;font-style:italic;color:var(--ink-faint);margin-bottom:8px';
      hint.textContent = item.action_hint;
      card.appendChild(hint);
    }

    // Buttons row
    const btns = document.createElement('div');
    btns.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap';

    // Three behaviours for the primary action button:
    //   1. content_md present  → open detail modal
    //   2. link_url starts '#' → switch to that tab in this SPA (no nav)
    //   3. link_url starts http → open in new tab
    //   else: no button shown
    if (item.content_md) {
      const expandBtn = document.createElement('button');
      expandBtn.className = 'btn btn-primary';
      expandBtn.style.cssText = 'font-size:12px';
      expandBtn.textContent = item.link_text || 'Expand';
      expandBtn.addEventListener('click', () => openInboxModal(item, card));
      btns.appendChild(expandBtn);
    } else if (item.link_url && item.link_url.startsWith('#')) {
      // SPA tab switch — no navigation, just activate the right tab
      const tabBtn = document.createElement('button');
      tabBtn.className = 'btn btn-primary';
      tabBtn.style.cssText = 'font-size:12px';
      tabBtn.textContent = item.link_text || 'Open';
      tabBtn.addEventListener('click', () => {
        const tabName = item.link_url.slice(1);  // strip the '#'
        const tabBtn = document.querySelector(`button.tab[data-tab="${tabName}"]`);
        if (tabBtn) tabBtn.click();
        else window.location.hash = item.link_url;  // fallback
      });
      btns.appendChild(tabBtn);
    } else if (item.link_url && /^https?:\/\//i.test(item.link_url)) {
      const link = document.createElement('a');
      link.className = 'btn btn-primary';
      link.style.cssText = 'text-decoration:none;font-size:12px';
      link.href = item.link_url;
      link.target = '_blank';
      link.rel = 'noopener';
      link.textContent = item.link_text || 'Open';
      btns.appendChild(link);
    }

    // Dismiss button: click expands an inline reason picker.  The selected
    // chip becomes the dismiss reason — these get fed into preference_signals
    // and the next grant_radar Haiku prompt, so Tealc learns to triage better.
    const dismissBtn = document.createElement('button');
    dismissBtn.className = 'btn';
    dismissBtn.style.cssText = 'font-size:12px';
    dismissBtn.textContent = 'Dismiss';
    dismissBtn.addEventListener('click', () => {
      // Replace the dismiss button with a small reason-picker row in place.
      const picker = document.createElement('div');
      picker.style.cssText = 'display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:11px';
      const why = document.createElement('span');
      why.style.cssText = 'color:var(--ink-faint);margin-right:4px';
      why.textContent = 'Why?';
      picker.appendChild(why);

      const reasons = _dismissReasonsFor(item.kind);
      const submit = (reasonText) => {
        inboxAction(card, {
          action: 'dismiss_inbox_item',
          kind: item.kind,
          target_id: item.id,
          reason: reasonText,
        });
      };
      reasons.forEach(r => {
        const chip = document.createElement('button');
        chip.className = 'btn';
        chip.style.cssText = 'font-size:11px;padding:2px 8px';
        chip.textContent = r;
        chip.addEventListener('click', () => submit(r));
        picker.appendChild(chip);
      });
      const other = document.createElement('button');
      other.className = 'btn';
      other.style.cssText = 'font-size:11px;padding:2px 8px';
      other.textContent = 'other…';
      other.addEventListener('click', () => {
        const txt = window.prompt('Why dismiss?') || '';
        if (txt.trim()) submit(txt.trim());
      });
      picker.appendChild(other);
      const cancel = document.createElement('button');
      cancel.className = 'btn';
      cancel.style.cssText = 'font-size:11px;padding:2px 8px;color:var(--ink-faint)';
      cancel.textContent = '×';
      cancel.title = 'cancel';
      cancel.addEventListener('click', () => { picker.replaceWith(dismissBtn); });
      picker.appendChild(cancel);

      dismissBtn.replaceWith(picker);
    });
    btns.appendChild(dismissBtn);

    card.appendChild(btns);
    cardsEl.appendChild(card);
  });
}

async function loadUnifiedInbox() {
  try {
    const res = await fetch('/api/inbox');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    _inboxData = data;

    const summary = data.inbox_summary || {};
    const byKind  = summary.by_kind || {};

    renderUnifiedInboxBanner(summary);
    renderUnifiedInboxFilters(byKind);
    renderUnifiedInboxCards(data);

    // Update badge
    const badge = document.getElementById('unifiedInboxBadge');
    const total = summary.total_pending || 0;
    badge.textContent = total > 0 ? String(total) : '';
    badge.style.background = total > 10 ? '#dc2626' : total >= 5 ? '#d97706' : '';

  } catch (e) {
    document.getElementById('unifiedInboxStatus').textContent = 'Load failed: ' + e.message;
  }
}


/* ══════════════════════════════════════════════════════════════════════════
   REVIEWER CIRCLE TAB
   ══════════════════════════════════════════════════════════════════════════ */

const STATUS_CHIP_COLORS = {
  draft:   { bg: '#e0e7ff', text: '#3730a3' },
  sent:    { bg: '#dbeafe', text: '#1d4ed8' },
  replied: { bg: '#dcfce7', text: '#15803d' },
  expired: { bg: '#fee2e2', text: '#dc2626' },
};

function renderReviewerCircleHeader(data) {
  const el = document.getElementById('reviewerCircleHeader');
  el.innerHTML = '';

  const h = document.createElement('h2');
  h.textContent = 'Reviewer Circle';
  el.appendChild(h);

  // Configure button if not set up
  if (!data.reviewers_configured) {
    const banner = document.createElement('div');
    banner.style.cssText = 'background:#fef9c3;border:1px solid #ca8a04;border-radius:6px;padding:10px 14px;margin-bottom:12px;font-size:13px';
    banner.innerHTML = 'Reviewer emails not yet configured. ';
    const cfgLink = document.createElement('a');
    const absPath = 'data/reviewer_circle/reviewers.json';
    cfgLink.href = 'file://' + absPath;
    cfgLink.textContent = 'Open reviewers.json';
    cfgLink.style.fontWeight = '600';
    banner.appendChild(cfgLink);
    banner.appendChild(document.createTextNode(' and fill in email addresses.'));
    el.appendChild(banner);
  }

  // Status count chips
  const chips = document.createElement('div');
  chips.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px';
  const inv = data.invitations_by_status || {};
  Object.entries(inv).forEach(([status, count]) => {
    const chip = document.createElement('span');
    const colors = STATUS_CHIP_COLORS[status] || { bg: '#e5e7eb', text: '#374151' };
    chip.style.cssText = `background:${colors.bg};color:${colors.text};padding:3px 10px;border-radius:12px;font-size:12px;font-weight:600`;
    chip.textContent = `${status}: ${count}`;
    chips.appendChild(chip);
  });
  el.appendChild(chips);

  // If no invitations at all, show pilot banner
  const totalInv = Object.values(inv).reduce((a, b) => a + b, 0);
  if (totalInv === 0) {
    const pilot = document.createElement('div');
    pilot.style.cssText = 'background:var(--bg-subtle,#f8fafc);border:1px solid var(--border);border-radius:6px;padding:12px 16px;font-size:13px;color:var(--ink-faint)';
    pilot.innerHTML = '<strong>Reviewer Circle pilot</strong><br>Tealc will generate blinded review batches for your hypotheses and analyses, send them to external evolutionary biologists, and compute inter-rater correlations. No invitations have been sent yet — configure reviewers to begin.';
    el.appendChild(pilot);
  }
}

function renderReviewerCircleInvitations(invitations) {
  const el = document.getElementById('reviewerCircleInvitations');
  el.innerHTML = '';
  if (!invitations || !invitations.length) return;

  const h = document.createElement('h3');
  h.textContent = `Invitations (${invitations.length} most recent)`;
  el.appendChild(h);

  const table = document.createElement('table');
  table.className = 'data-table';
  const thead = table.createTHead();
  const hrow = thead.insertRow();
  ['Pseudonym', 'Domain', 'Status', 'SLA', 'Sent'].forEach(col => {
    const th = document.createElement('th');
    th.textContent = col;
    hrow.appendChild(th);
  });

  const tbody = table.createTBody();
  invitations.forEach(inv => {
    const row = tbody.insertRow();
    row.insertCell().textContent = inv.pseudonym || '—';
    row.insertCell().textContent = (inv.domain || '—').replace(/_/g, ' ');

    const statusCell = row.insertCell();
    const colors = STATUS_CHIP_COLORS[inv.status] || { bg: '#e5e7eb', text: '#374151' };
    statusCell.innerHTML = `<span style="background:${colors.bg};color:${colors.text};padding:1px 7px;border-radius:8px;font-size:11px;font-weight:600">${inv.status}</span>`;

    row.insertCell().textContent = inv.sla_iso ? inv.sla_iso.slice(0, 10) : '—';
    row.insertCell().textContent = inv.sent_at ? fmtDate(inv.sent_at) : '—';
  });
  table.appendChild(tbody);
  el.appendChild(table);
}

function renderReviewerCircleCorrelations(correlations) {
  const el = document.getElementById('reviewerCircleCorrelations');
  el.innerHTML = '';

  const h = document.createElement('h3');
  h.textContent = 'Inter-rater Correlations';
  el.appendChild(h);

  if (!correlations || !correlations.length) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = 'No correlations computed yet — these appear once reviewers have scored the same items.';
    el.appendChild(empty);
    return;
  }

  const table = document.createElement('table');
  table.className = 'data-table';
  const thead = table.createTHead();
  const hrow = thead.insertRow();
  ['Domain', 'Dimension', 'N pairs', 'Spearman r', '95% CI', 'Computed'].forEach(col => {
    const th = document.createElement('th');
    th.textContent = col;
    hrow.appendChild(th);
  });

  const tbody = table.createTBody();
  correlations.forEach(c => {
    const row = tbody.insertRow();
    row.insertCell().textContent = (c.domain || '—').replace(/_/g, ' ');
    row.insertCell().textContent = (c.dimension || '—').replace(/_/g, ' ');
    row.insertCell().textContent = c.n_pairs != null ? c.n_pairs : '—';

    const rCell = row.insertCell();
    rCell.className = 'num';
    rCell.textContent = c.spearman_r != null ? Number(c.spearman_r).toFixed(3) : '—';

    const ciCell = row.insertCell();
    ciCell.className = 'num';
    ciCell.textContent = (c.ci_lo != null && c.ci_hi != null)
      ? `[${Number(c.ci_lo).toFixed(2)}, ${Number(c.ci_hi).toFixed(2)}]`
      : '—';

    row.insertCell().textContent = c.computed_at ? fmtDate(c.computed_at) : '—';
  });
  table.appendChild(tbody);
  el.appendChild(table);
}

async function loadReviewerCircle() {
  try {
    const res = await fetch('/api/reviewer_circle');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();

    renderReviewerCircleHeader(data);
    renderReviewerCircleInvitations(data.invitations || []);
    renderReviewerCircleCorrelations(data.correlations_latest || []);

    // Update badge — show count of draft invitations needing to be sent
    const draftCount = (data.invitations_by_status || {}).draft || 0;
    const badge = document.getElementById('reviewerCircleBadge');
    badge.textContent = draftCount > 0 ? String(draftCount) : '';

  } catch (e) {
    document.getElementById('reviewerCircleStatus').textContent = 'Load failed: ' + e.message;
  }
}


/* ── Tab switching ────────────────────────────────────────────────────────── */

function initTabs() {
  const tabs = document.querySelectorAll('.tab');
  tabs.forEach(tab => {
    tab.addEventListener('click', () => {
      const target = tab.dataset.tab;

      tabs.forEach(t => t.classList.remove('active'));
      tab.classList.add('active');

      document.querySelectorAll('.tab-content').forEach(panel => {
        panel.classList.remove('active');
      });
      const panel = document.getElementById('tab-' + target);
      if (panel) panel.classList.add('active');

      if (target === 'unified-inbox') loadUnifiedInbox();
      // 'inbox' tab removed — unified-inbox tab is the default
      if (target === 'control') loadControl();
      if (target === 'documents') loadDocuments();
      if (target === 'goals') loadGoals();
      if (target === 'knowledge') loadKnowledge();
      if (target === 'projects') loadProjects();
      if (target === 'prereg') loadPrereg();
      if (target === 'reviewer-circle') loadReviewerCircle();
    });
  });
}

/* ── Bootstrap ────────────────────────────────────────────────────────────── */

document.addEventListener('DOMContentLoaded', () => {
  initTabs();

  loadState();
  setInterval(loadState, 30_000);

  // Default tab is now Unified Inbox — load it immediately.
  loadUnifiedInbox();
  // Auto-refresh inbox every 60s
  setInterval(loadUnifiedInbox, 60_000);
});
