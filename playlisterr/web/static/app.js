/* Playlisterr web UI.
 *
 * No framework and no build step on purpose: this ships inside a self-hosted
 * container where "clone it and run it" has to stay true.
 *
 * The settings form is *described* (see SECTIONS) rather than hand-written,
 * so one place decides what a field means, when it is relevant, and how it is
 * explained. Rules a human would state out loud — Ollama never wants an API
 * key, OpenAI always does, library names should be ticked not typed — live in
 * `showIf` and `type` here instead of being scattered through markup.
 */
const $ = (id) => document.getElementById(id);
let SETTINGS = null;
let LIBRARIES = null;
let LAST_STATUS = null;
let POLLING = null;
let LOG_POLL = null;
let RUN_TIMER = null;
// Opaque reference to the Plex token the server holds for the chosen server;
// sent back on save so the real token never travels through the browser.
let PLEX_TOKEN_REF = '';

// The server injects the API key into the page (window.PLAYLISTERR_KEY). Sending
// it on every request means turning on auth_required never locks the UI out,
// and — because a cross-origin page can neither read this value nor set a
// custom header — it doubles as the CSRF token the sensitive endpoints require.
const API_KEY = (typeof window !== 'undefined' && window.PLAYLISTERR_KEY) || '';

async function api(path, options) {
  const opts = options || {};
  const headers = Object.assign({ 'Content-Type': 'application/json' },
    opts.headers || {});
  if (API_KEY) headers['X-Api-Key'] = API_KEY;
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}

function msg(el, kind, text) {
  if (el) el.innerHTML = text
    ? `<div class="msg ${kind}">${escapeHtml(text)}</div>` : '';
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

/* ---------------------------------------------------------------- nav */
document.querySelectorAll('nav button').forEach((b) => {
  b.onclick = () => show(b.dataset.page);
});

function show(page) {
  document.querySelectorAll('nav button').forEach(
    (b) => b.classList.toggle('active', b.dataset.page === page));
  ['home', 'users', 'settings', 'system', 'setup'].forEach((p) => {
    const el = $(`page-${p}`);
    if (el) el.classList.toggle('hidden', p !== page);
  });
  // Deep-linkable: reflect the tab in the URL hash so /#settings opens there.
  if (page !== 'setup' && location.hash.slice(1) !== page) {
    history.replaceState(null, '', `#${page}`);
  }
  if (page === 'users') loadUsers();
  if (page === 'settings') loadSettings();
  if (page === 'system') { loadRuns(); loadLogs(); }
}

// Open the tab named in the URL hash on load (and on back/forward).
function showFromHash() {
  const page = location.hash.slice(1);
  if (['home', 'users', 'settings', 'system'].includes(page)) show(page);
}
window.addEventListener('hashchange', showFromHash);

/* -------------------------------------------------------------- status */
async function loadStatus() {
  let s;
  try {
    s = await api('/api/status');
  } catch (e) {
    $('statusPill').className = 'pill bad';
    $('statusPill').textContent = 'offline';
    return;
  }
  LAST_STATUS = s;
  $('version').textContent = s.version;
  $('schedPill').textContent = s.next_run === 'disabled'
    ? 'manual only' : `next: ${s.next_run}`;
  $('statusPill').className = `pill ${s.configured ? 'ok' : 'bad'}`;
  $('statusPill').textContent = s.configured ? 'ready' : 'setup needed';

  if (!s.configured) {
    document.querySelectorAll('nav button').forEach(
      (b) => b.classList.remove('active'));
    ['home', 'users', 'settings', 'system'].forEach(
      (p) => $(`page-${p}`).classList.add('hidden'));
    $('page-setup').classList.remove('hidden');
    if (!SETTINGS) await loadSettings(true);
    return;
  }
  if (!$('page-setup').classList.contains('hidden')) {
    $('page-setup').classList.add('hidden');
    show('home');
  }

  const last = s.last_run;
  $('homeSub').textContent = last
    ? `Last run ${new Date(last.at * 1000).toLocaleString()} · `
      + `${last.dry ? 'generated, nothing written' : 'published'} · `
      + `${last.seconds}s`
    : 'Nothing generated yet. Press Generate — it writes nothing.';
  $('homeStats').innerHTML = [
    ['People', last ? last.users : '—'],
    ['Personalised', last ? last.personalized : '—'],
    ['House list', last ? last.house : '—'],
    ['Titles in library', last ? last.catalog_items : '—'],
  ].map(([l, n]) => `<div class="card stat"><div class="n">${n}</div>
      <div class="l">${l}</div></div>`).join('');

  renderRun(s.run);
  if (last && last.picks && !s.run.running) renderPicks(last.picks);
  updatePreviews();
  loadHealth();
}

/* Health is a separate, slower probe (it reaches out to Plex/Ollama), cached
   server-side. It drives an amber header pill and a Home banner so a dead
   dependency shows up now, not at 03:30. */
let HEALTH_AT = 0;
async function loadHealth(force) {
  if (!force && Date.now() - HEALTH_AT < 60000) return;
  HEALTH_AT = Date.now();
  let h;
  try { h = await api('/api/health'); } catch (e) { return; }
  const pill = $('healthPill');
  if (pill) {
    pill.className = `pill ${h.healthy ? 'ok' : 'bad'}`;
    pill.textContent = h.healthy ? 'all systems go'
      : `${h.problems.length} problem${h.problems.length === 1 ? '' : 's'}`;
    pill.title = h.healthy ? 'Plex and the model are reachable'
      : h.problems.join(' · ');
  }
  const banner = $('healthBanner');
  if (banner) {
    banner.innerHTML = h.healthy ? '' : `<div class="msg bad">
      <b>Something isn't reachable:</b> ${h.problems.map(escapeHtml)
        .join('<br>')}<br><span class="sub">A scheduled run would fail until
      this is fixed. Check Settings → the relevant connection.</span></div>`;
  }
}

function renderRun(run) {
  const busy = run && run.running;
  $('btnDry').disabled = busy;
  $('btnPub').disabled = busy;

  // "Publish these picks" is only honest when a set has been generated.
  const saved = LAST_STATUS && LAST_STATUS.preview;
  const btn = $('btnSaved');
  if (btn) {
    btn.disabled = busy || !saved;
    const note = $('previewNote');
    if (note) {
      note.innerHTML = saved
        ? `Publishes what was generated on <b>${new Date(saved.at * 1000)
            .toLocaleString()}</b> — ${saved.rows} rows, ${saved.titles}
           titles — unchanged. No model call, so you get exactly what you
           looked at.`
        : 'Nothing generated yet. Press Generate first, then this publishes '
          + 'those exact picks without deciding them again.';
    }
  }
  paintRunPill();
  const logBox = $('runLog');
  if (busy) {
    if (!POLLING) POLLING = setInterval(loadStatus, 2500);
    // Tick the elapsed timer every second between the slower status polls, so
    // the counter keeps moving and the run visibly looks alive.
    if (!RUN_TIMER) RUN_TIMER = setInterval(paintRunPill, 1000);
    if (logBox) logBox.classList.remove('hidden');
    if (!LOG_POLL) { pollRunLog(); LOG_POLL = setInterval(pollRunLog, 1500); }
  } else {
    if (POLLING) { clearInterval(POLLING); POLLING = null; }
    if (RUN_TIMER) { clearInterval(RUN_TIMER); RUN_TIMER = null; }
    // One last fetch so the final lines land, then leave the log on screen.
    if (LOG_POLL) { clearInterval(LOG_POLL); LOG_POLL = null; pollRunLog(); }
  }
  if (run && run.summary && run.summary.picks) renderPicks(run.summary.picks);
}

function fmtElapsed(sec) {
  sec = Math.max(0, Math.floor(sec));
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`;
}

// Painted both by renderRun (on each status poll) and by a 1s timer while a run
// is live, so the seconds keep climbing between polls.
function paintRunPill() {
  const pill = $('runState');
  if (!pill) return;
  const run = LAST_STATUS && LAST_STATUS.run;
  if (run && run.running) {
    const since = run.started ? (Date.now() / 1000 - run.started) : 0;
    pill.className = 'pill';
    pill.textContent = `running (${run.progress}) · ${fmtElapsed(since)}`;
  } else if (run && run.error) {
    pill.className = 'pill bad';
    pill.textContent = `failed: ${run.error}`;
  } else {
    pill.className = 'pill ok';
    pill.textContent = 'idle';
  }
}

// Live tail of the log file while a run is going, so the user sees real
// activity rather than a frozen "Generating…" line.
async function pollRunLog() {
  const box = $('runLog');
  if (!box) return;
  try {
    const r = await api('/api/logs?lines=60');
    box.textContent = (r.lines || []).join('\n') || '(waiting for output…)';
    box.scrollTop = box.scrollHeight;   // keep pinned to the newest line
  } catch (e) { /* keep whatever is already shown */ }
}

let PICKS_SIG = null;

function renderPicks(picks) {
  // The status poll fires every 15s; re-rendering identical picks would snap
  // shut any panel the user has open. Skip when nothing changed, and preserve
  // which panels are expanded across a real re-render.
  const sig = JSON.stringify(Object.keys(picks).map(
    (n) => [n, picks[n].map((p) => p.title)]));
  if (sig === PICKS_SIG && $('results').children.length) return;
  PICKS_SIG = sig;

  const openNow = new Set(
    [...$('results').querySelectorAll('details[open] > summary')]
      .map((s) => s.dataset.name));

  $('results').innerHTML = Object.keys(picks).map((name, idx) => {
    const rows = picks[name].map((p) => `<div class="pick">
        <button class="veto" title="Never recommend this"
          onclick="vetoTitle(this, ${JSON.stringify(p.title)
            .replace(/"/g, '&quot;')})">✕</button>
        <span class="t">${escapeHtml(p.title)}</span>
        <span class="via">${p.via === 'llm' ? '' : ' · house list'}</span><br>
        <span class="r">${escapeHtml(p.reason)}</span></div>`).join('');
    // In the demo, open the first panel so a screenshot shows real picks.
    const demoOpen = window.PLAYLISTERR_DEMO && idx === 0;
    const open = (openNow.has(name) || demoOpen) ? ' open' : '';
    return `<details class="card result"${open}>
      <summary data-name="${escapeHtml(name)}">${escapeHtml(name)}
      — ${picks[name].length} picks</summary>${rows}</details>`;
  }).join('') || '<div class="card">No picks yet.</div>';
}

async function vetoTitle(btn, title) {
  try {
    const r = await api('/api/exclude', {
      method: 'POST', body: JSON.stringify({ title }),
    });
    if (r.ok) {
      btn.textContent = '✓';
      btn.disabled = true;
      btn.closest('.pick').style.opacity = '.5';
      msg($('runMsg'), 'ok', `"${r.title}" won't be recommended again. `
        + 'It stays until the next run rebuilds the rows.');
    } else {
      msg($('runMsg'), 'bad', r.error || 'could not exclude');
    }
  } catch (e) {
    msg($('runMsg'), 'bad', e.message);
  }
}

function primeRunLog() {
  const box = $('runLog');
  if (box) { box.textContent = 'starting…'; box.classList.remove('hidden'); }
}

async function startRun(dry) {
  msg($('runMsg'), 'info', dry
    ? 'Generating. Nothing will be written to Plex — when it finishes you '
      + 'can publish exactly what you see.'
    : 'Generating a fresh set and publishing it.');
  primeRunLog();
  try {
    await api('/api/run', { method: 'POST', body: JSON.stringify({ dry }) });
  } catch (e) {
    msg($('runMsg'), 'bad', e.message);
    return;
  }
  loadStatus();
}

async function publishPreview() {
  msg($('runMsg'), 'info', 'Publishing exactly what was generated…');
  primeRunLog();
  try {
    await api('/api/run', {
      method: 'POST', body: JSON.stringify({ publish_preview: true }),
    });
  } catch (e) {
    msg($('runMsg'), 'bad', e.message);
    return;
  }
  loadStatus();
}

/* --------------------------------------------------------------- users */
/* USERS is the source of truth while the page is open; the checkboxes write
 * back into it so a search that filters rows out cannot silently drop
 * somebody's setting. */
let USERS = [];
let USERS_MODE = 'all';

async function loadUsers() {
  const tbody = $('usersTable').querySelector('tbody');
  tbody.innerHTML = '<tr><td colspan="5">reading history…</td></tr>';
  try {
    const d = await api('/api/users');
    USERS = d.users;
    USERS_MODE = d.mode || 'all';
    $('usersSub').textContent =
      `${d.users.length} people · history from ${d.history_source} · `
      + `${d.catalog} titles · ${d.window_days}-day window`;
    pickUsersMode(USERS_MODE, true);
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5">${escapeHtml(e.message)}</td></tr>`;
  }
}

function pickUsersMode(mode, quiet) {
  USERS_MODE = mode;
  const box = $('usersMode');
  box.dataset.value = mode;
  box.querySelectorAll('.opt').forEach(
    (o) => o.classList.toggle('sel', o.dataset.v === mode));
  $('userPicker').classList.toggle('hidden', mode !== 'selected');
  $('thPick').textContent = mode === 'selected' ? '' : '';
  if (mode === 'all') USERS.forEach((u) => { u.enabled = true; });
  renderUserRows();
  if (!quiet) msg($('usersMsg'), 'info', 'Press Save to apply.');
}

function renderUserRows() {
  const q = ($('userSearch') ? $('userSearch').value : '').trim().toLowerCase();
  const picking = USERS_MODE === 'selected';
  const shown = USERS.filter((u) => !q
    || u.username.toLowerCase().includes(q)
    || u.display_name.toLowerCase().includes(q));
  const tbody = $('usersTable').querySelector('tbody');
  tbody.innerHTML = shown.map((u) => `<tr class="${u.enabled ? '' : 'off'}">
      <td>${picking ? `<input type="checkbox" ${u.enabled ? 'checked' : ''}
        onchange="toggleUser('${u.account_id}', this.checked)">` : ''}</td>
      <td>${escapeHtml(u.display_name)}
        <span class="tag">${escapeHtml(u.username)}</span></td>
      <td class="num">${u.plays}</td><td class="num">${u.titles}</td>
      <td><span class="tag ${u.tier}">${u.tier === 'personalized'
        ? 'personal row' : 'house list'}</span></td>
    </tr>`).join('')
    || '<tr><td colspan="5">nobody matches that search</td></tr>';
  const on = USERS.filter((u) => u.enabled).length;
  if ($('userCount')) {
    $('userCount').textContent = `${on} of ${USERS.length} selected`;
  }
}

function toggleUser(id, on) {
  const u = USERS.find((x) => x.account_id === id);
  if (u) u.enabled = on;
  renderUserRows();
}

/* Bulk actions apply to what the search is currently showing, which is what
 * makes "search 'kids', select all" do the obvious thing. */
function setAllUsers(what) {
  const q = ($('userSearch') ? $('userSearch').value : '').trim().toLowerCase();
  USERS.forEach((u) => {
    const visible = !q || u.username.toLowerCase().includes(q)
      || u.display_name.toLowerCase().includes(q);
    if (!visible) return;
    u.enabled = what === 'personal' ? u.tier === 'personalized' : !!what;
  });
  renderUserRows();
  msg($('usersMsg'), 'info', 'Press Save to apply.');
}

async function saveUsers() {
  const chosen = USERS.filter((u) => u.enabled);
  try {
    const r = await api('/api/users', {
      method: 'POST',
      body: JSON.stringify({
        mode: USERS_MODE,
        selected: chosen.map((u) => u.account_id),
        names: chosen.map((u) => u.username),
      }),
    });
    if (!r.ok) { msg($('usersMsg'), 'bad', r.error); return; }
    msg($('usersMsg'), 'ok', USERS_MODE === 'all'
      ? 'Saved — rows will be built for everyone.'
      : `Saved — ${r.count} ${r.count === 1 ? 'person' : 'people'} selected. `
        + 'Anyone switched off loses their rows on the next run.');
    loadStatus();
  } catch (e) {
    msg($('usersMsg'), 'bad', e.message);
  }
}

/* ------------------------------------------------------------ settings */
const SECTIONS = [
  {
    title: 'Plex',
    blurb: 'Where your library and everyone’s watch history come from. '
      + 'The only service Playlisterr genuinely requires.',
    fields: [
      { k: 'plex.url', label: 'Server address', type: 'text',
        hint: 'Filled in for you by Sign in with Plex.' },
      { k: 'plex.token', label: 'Token', type: 'password',
        hint: 'Also filled in by signing in — you should never have to '
          + 'go and find this by hand.' },
      { k: 'plex.movie_libraries', type: 'libraries', libType: 'movie',
        label: 'Movie libraries to recommend from',
        hint: 'All your movie libraries, ticked by default — untick any you '
          + 'don’t want recommendations from.' },
      { k: 'plex.show_libraries', type: 'libraries', libType: 'show',
        label: 'TV libraries to recommend from',
        hint: 'All your TV libraries, ticked by default — untick any you '
          + 'don’t want recommendations from.' },
    ],
    actions: '<button class="btn ghost" onclick="plexSignIn(true)">'
      + 'Sign in with Plex</button> '
      + '<button class="btn ghost" onclick="testConn(\'plex\')">Test</button> '
      + '<button class="btn ghost danger" onclick="resetPlex()">Reset</button>'
      + '<div id="test_plex"></div>',
  },
  {
    title: 'AI model',
    blurb: 'Playlisterr hands the model a taste profile and a list of titles you '
      + 'already own; it picks from that list and writes one line per choice. '
      + 'It is never allowed to invent a title.',
    fields: [
      { k: 'llm.provider', type: 'cards', label: 'Where does the model run?',
        options: [
          { v: 'ollama', t: 'Ollama (local)',
            d: 'On your own hardware. Free, private, and no API key — '
               + 'Ollama does not use one.' },
          { v: 'openai', t: 'OpenAI',
            d: 'OpenAI’s hosted API. Needs a key, and every run costs a '
               + 'little.' },
          { v: 'custom', t: 'Custom / OpenAI-compatible',
            d: 'LM Studio, llama.cpp, vLLM, OpenRouter — or Ollama on a '
               + 'different machine. Key only if that server asks for one.' },
        ] },
      { k: 'llm.url', label: 'Address', type: 'text',
        showIf: (v) => v['llm.provider'] !== 'openai',
        hint: 'e.g. http://192.168.1.20:11434 for Ollama on another box.' },
      { k: 'llm.api_key', label: 'API key', type: 'password',
        showIf: (v) => v['llm.provider'] !== 'ollama',
        hint: (v) => v['llm.provider'] === 'openai'
          ? 'Required. Starts with sk-.'
          : 'Optional — most self-hosted servers do not want one.' },
      { k: 'llm.model', label: 'Model', type: 'model',
        hint: 'Detect lists what this endpoint actually offers, or type a '
          + 'name it has not advertised.' },
      { k: 'llm.temperature', label: 'Temperature', type: 'number',
        advanced: true, hint: 'Higher is more adventurous. 0.4 is sensible.' },
      { k: 'llm.candidates', label: 'Titles shown to the model',
        type: 'number', advanced: true,
        hint: 'More is slower — reading the prompt costs more time than '
          + 'writing the answer does.' },
      { k: 'llm.keep_alive', label: 'Keep the model loaded for', type: 'text',
        advanced: true,
        hint: 'Ollama only. Loading a 14B model costs about 110 seconds, so '
          + 'staying warm between people is the difference between a '
          + 'four-minute run and an hour.' },
      { k: 'llm.timeout', label: 'Timeout (seconds)', type: 'number',
        advanced: true },
      { k: 'llm.system_prompt', label: 'Custom AI prompt', type: 'prompt',
        advanced: true,
        hint: 'The instructions the model is given. Almost everyone should '
          + 'leave this on the default — it is what keeps the model returning '
          + 'valid, checkable picks.' },
    ],
    actions: '<button class="btn ghost" onclick="probeLLM(true)">'
      + 'Find my model</button> '
      + '<button class="btn ghost" onclick="testConn(\'llm\')">Test</button>'
      + '<div id="test_llm"></div>',
  },
  {
    title: 'What gets recommended', short: 'Recommendations',
    blurb: 'How many, how far back, and who has watched enough to be worth '
      + 'personalising for.',
    fields: [
      { k: 'recommendations.picks', label: 'Titles per row', type: 'number' },
      { k: 'recommendations.history_days',
        label: 'Look back this many days', type: 'number',
        hint: 'How much viewing counts towards someone’s taste.' },
      { k: 'recommendations.min_plays',
        label: 'Plays needed for a personal row', type: 'number',
        hint: 'Below this, that person gets the house list instead. A model '
          + 'given four plays does not find a taste — it invents one.' },
      { k: 'recommendations.min_titles', label: 'Different titles needed',
        type: 'number',
        hint: 'Thirty episodes of one show is a play count, not a taste.' },
      { k: 'recommendations.include_movies', label: 'Recommend movies',
        type: 'bool' },
      { k: 'recommendations.include_shows', label: 'Recommend TV',
        type: 'bool' },
      { k: 'recommendations.exclude_genres',
        label: 'Never recommend these genres', type: 'list',
        hint: 'Comma separated, e.g. Horror, Reality.' },
      { k: 'recommendations.exclude_titles',
        label: 'Never recommend these titles', type: 'list',
        hint: 'Comma separated. The "never recommend" button on a pick adds '
          + 'here too.' },
      { k: 'recommendations.min_rating', label: 'Minimum rating',
        type: 'number', advanced: true,
        hint: '0 disables it. Plex ratings are out of 10.' },
    ],
  },
  {
    title: 'How rows reach people', short: 'Delivery',
    blurb: 'The one choice with a real trade-off, so here is all of it.',
    fields: [
      { k: 'recommendations.delivery', type: 'cards', label: 'Row type',
        options: [
          { v: 'playlist', t: 'Playlists (recommended)',
            d: 'Plex ties a playlist to an account, so each person sees only '
               + 'their own — including you. Shows up under Playlists and '
               + 'on home screens. TV picks become the next unwatched episode, '
               + 'because a playlist cannot hold a whole series.' },
          { v: 'collection', t: 'Collections',
            d: 'Real category rows inside a library, which look better. But '
               + 'a collection is server-wide: everyone with access to the '
               + 'library sees every row, you included.' },
        ] },
      { k: 'recommendations.per_library',
        label: 'A separate row for each library', type: 'bool',
        hint: 'On: "Movies Playlist", "Kids Movies Playlist". Off: one mixed '
          + 'row per person.' },
      { k: 'recommendations.taste_scope', type: 'cards',
        label: 'Whose taste does each row follow?',
        showIf: (v) => v['recommendations.per_library'],
        options: [
          { v: 'library', t: 'The library it lives in (recommended)',
            d: 'The Kids Movies row follows what was watched in Kids Movies. '
               + 'One Plex login is often several people — if the kids watch '
               + 'on your account, their row should not be picked from your '
               + 'viewing.' },
          { v: 'account', t: 'One taste for every row',
            d: 'Every row is picked from everything that account has '
               + 'watched, then filtered to the library.' },
        ] },
      { k: 'recommendations.library_min_plays', type: 'number',
        label: 'Plays needed in a library before it has its own taste',
        advanced: true,
        showIf: (v) => v['recommendations.per_library']
          && v['recommendations.taste_scope'] === 'library',
        hint: 'Under this, that row falls back to the account-wide taste and '
          + 'says so.' },
      { k: 'recommendations.playlist_title', label: 'Row name',
        type: 'preview',
        showIf: (v) => v['recommendations.per_library'],
        hint: '{library}, {name} and {username} are available.' },
      { k: 'recommendations.collection_title', label: 'Row name',
        type: 'preview',
        showIf: (v) => !v['recommendations.per_library'],
        hint: '{name} and {username} are available.' },
      { k: 'recommendations.fallback_title', label: 'Name of the house row',
        type: 'text', hint: 'What people with too little history get.' },
      { k: 'recommendations.max_libraries',
        label: 'Most libraries per person', type: 'number', advanced: true,
        showIf: (v) => v['recommendations.per_library'] },
      { k: 'recommendations.library_row_position', type: 'select',
        label: 'Where the row sits on the library page', advanced: true,
        options: ['top', 'bottom'],
        showIf: (v) => v['recommendations.delivery'] !== 'playlist' },
      { k: 'recommendations.label_prefix', label: 'Label prefix',
        type: 'text', advanced: true,
        hint: 'Playlisterr only ever modifies things carrying this label.' },
    ],
  },
  {
    title: 'Schedule',
    blurb: 'Every run rebuilds the rows from scratch, so a missed one just '
      + 'means yesterday’s picks stay up.',
    fields: [
      { k: 'server.schedule_mode', type: 'cards', label: 'How often',
        options: [
          { v: 'daily', t: 'Once a day', d: 'The usual choice.' },
          { v: 'twice_daily', t: 'Twice a day',
            d: 'Twelve hours apart — useful on a busy server.' },
          { v: 'every_6h', t: 'Every 6 hours',
            d: 'Only worth it if people watch things all day.' },
          { v: 'manual', t: 'Manual only',
            d: 'Nothing runs unless you press Run.' },
        ] },
      { k: 'server.schedule_time', label: 'Starting at', type: 'time',
        showIf: (v) => v['server.schedule_mode'] !== 'manual',
        hint: 'Local time. Pick something quiet — a full run works the '
          + 'GPU for a few minutes.' },
    ],
    actions: '<div class="sub" id="nextRunNote"></div>'
      + '<button class="btn" onclick="show(\'home\');startRun(false)">'
      + 'Run now</button>',
  },
  {
    title: 'Notifications',
    blurb: 'Playlisterr runs unattended, so it can ping you when a run finishes. '
      + 'Works with any webhook — ntfy, Gotify, Discord, Slack, or a generic '
      + 'JSON receiver like Home Assistant.',
    fields: [
      { k: 'notifications.webhook_url', label: 'Webhook URL', type: 'text',
        hint: 'Leave blank to disable.' },
      { k: 'notifications.format', type: 'select', label: 'Format',
        options: ['json', 'discord', 'slack', 'ntfy', 'gotify'],
        showIf: (v) => !!v['notifications.webhook_url'] },
      { k: 'notifications.on_success', label: 'Notify on a successful run',
        type: 'bool', showIf: (v) => !!v['notifications.webhook_url'] },
      { k: 'notifications.on_failure', label: 'Notify when a run fails',
        type: 'bool', showIf: (v) => !!v['notifications.webhook_url'] },
    ],
    actions: '<button class="btn ghost" onclick="testNotify()">'
      + 'Send a test</button><div id="notifyMsg"></div>',
  },
  {
    title: 'Advanced',
    fields: [
      { k: 'server.port', label: 'Port', type: 'number', advanced: true,
        hint: 'Takes effect on restart.' },
      { k: 'server.api_key', label: 'API key', type: 'password',
        advanced: true, hint: 'For scripting against Playlisterr.' },
      { k: 'server.auth_required', advanced: true,
        label: 'Require the API key in the browser', type: 'bool',
        hint: 'Leave off behind a proxy that already handles logins.' },
      { k: 'general.log_level', type: 'select', label: 'Log level',
        advanced: true, options: ['debug', 'info', 'warning', 'error'] },
      { k: 'history.provider', type: 'select', label: 'Watch history from',
        advanced: true, options: ['auto', 'plex', 'tautulli'],
        hint: 'auto keeps whichever source has more history in the window.' },
      { k: 'tautulli.url', label: 'Tautulli address', type: 'text',
        advanced: true },
      { k: 'tautulli.api_key', label: 'Tautulli API key', type: 'password',
        advanced: true },
    ],
    actions: '<button class="btn ghost" onclick="testConn(\'tautulli\')">'
      + 'Test Tautulli</button><div id="test_tautulli"></div>',
  },
];

const fid = (key) => `f_${key.replace('.', '_')}`;

function val(key) {
  const [s, k] = key.split('.');
  return SETTINGS && SETTINGS[s] ? SETTINGS[s][k] : '';
}

function formValues() {
  const out = {};
  SECTIONS.forEach((sec) => sec.fields.forEach((f) => {
    const el = $(fid(f.k));
    if (!el) { out[f.k] = val(f.k); return; }
    if (f.type === 'cards') out[f.k] = el.dataset.value;
    else if (f.type === 'bool') out[f.k] = el.checked;
    else if (f.type === 'libraries') {
      out[f.k] = Array.from(el.querySelectorAll('input:checked'))
        .map((i) => i.value);
    } else out[f.k] = el.value;
  }));
  return out;
}

async function loadSettings(quiet) {
  const d = await api('/api/settings');
  SETTINGS = d.settings;
  if (quiet) { fillWizard(); return; }
  if (LIBRARIES === null) {
    try { LIBRARIES = (await api('/api/libraries')).libraries || []; }
    catch (e) { LIBRARIES = []; }
  }
  renderSettings();
}

let SETTINGS_SECTION = 0;

function renderSettings() {
  const v = {};
  SECTIONS.forEach((s) => s.fields.forEach((f) => { v[f.k] = val(f.k); }));
  if (SETTINGS_SECTION >= SECTIONS.length) SETTINGS_SECTION = 0;
  // A sub-nav so only one group shows at a time — the whole settings surface
  // on one scroll was a lot to take in. All cards are still in the DOM (just
  // hidden), so Save captures every field regardless of which tab is open.
  const nav = SECTIONS.map((sec, i) =>
    `<button class="subtab ${i === SETTINGS_SECTION ? 'active' : ''}"
       onclick="showSettingsSection(${i})">${escapeHtml(sec.short
         || sec.title)}</button>`).join('');
  const cards = SECTIONS.map((sec, i) => {
    const body = sec.fields.map((f) => renderField(f, v)).join('');
    return `<div class="card settings-sec${i === SETTINGS_SECTION
        ? '' : ' hidden'}" data-sec="${i}">
      <h2 style="margin-top:0">${sec.title}</h2>
      ${sec.blurb ? `<p class="sub">${sec.blurb}</p>` : ''}
      ${body}
      ${sec.actions || ''}
    </div>`;
  }).join('');
  $('settingsForm').innerHTML = `<div class="subnav">${nav}</div>${cards}`;
  refreshConditional();
}

function showSettingsSection(i) {
  SETTINGS_SECTION = i;
  document.querySelectorAll('#settingsForm .subtab').forEach((b, idx) =>
    b.classList.toggle('active', idx === i));
  document.querySelectorAll('#settingsForm .settings-sec').forEach((c) =>
    c.classList.toggle('hidden', Number(c.dataset.sec) !== i));
  window.scrollTo(0, 0);
}

// Library checkboxes are ticked ON by default: an empty saved selection means
// "every library", so a fresh setup starts with everything checked and the
// user unticks what they don't want. Once a subset is saved, exactly that list
// shows checked. The list always comes from a live Plex connection (a
// successful Test or Sign in with Plex) — never a hardcoded default.
function libraryBoxes(libType, chosen) {
  const libs = (LIBRARIES || []).filter((l) => l.type === libType);
  if (!libs.length) {
    return '<span class="sub">Sign in with Plex, or enter a server address '
      + 'and press Test, to list your libraries.</span>';
  }
  const picked = (chosen || []).map((x) => String(x).toLowerCase());
  const allOn = picked.length === 0;   // nothing saved yet → everything on
  return libs.map((l) => `
    <label class="chk"><input type="checkbox" value="${escapeHtml(l.title)}"
      ${allOn || picked.includes(l.title.toLowerCase()) ? 'checked' : ''}>
      ${escapeHtml(l.title)}</label>`).join('');
}

// Repopulate the library checkboxes in place after a Test hands us the list,
// without re-rendering the whole form (which would wipe a half-typed URL or
// token). Whatever is currently ticked is preserved; a field still showing the
// placeholder falls back to its saved value, i.e. all-on.
function refreshLibraryFields() {
  SECTIONS.forEach((sec) => sec.fields.forEach((f) => {
    if (f.type !== 'libraries') return;
    const el = $(fid(f.k));
    if (!el) return;
    const current = el.querySelector('input')
      ? Array.from(el.querySelectorAll('input:checked')).map((i) => i.value)
      : val(f.k);
    el.innerHTML = libraryBoxes(f.libType, current);
  }));
}

function renderField(f, v) {
  const id = fid(f.k);
  const value = v[f.k];
  const hint = typeof f.hint === 'function' ? f.hint(v) : f.hint;
  const hintHtml = hint ? `<span class="hint">${hint}</span>` : '';
  const advTag = f.advanced ? '<span class="adv">advanced</span>' : '';

  if (f.type === 'cards') {
    const cards = f.options.map((o) => `
      <div class="opt ${o.v === value ? 'sel' : ''}" data-v="${o.v}"
           onclick="pickCard('${id}','${o.v}')">
        <div class="opt-t">${o.t}</div>
        <div class="opt-d">${o.d}</div></div>`).join('');
    return `<div class="field">
      <span class="name">${f.label}${advTag}</span>${hintHtml}
      <div class="opts" id="${id}" data-value="${escapeHtml(value)}">
        ${cards}</div></div>`;
  }
  if (f.type === 'bool') {
    return `<label class="field">
      <span class="name"><input type="checkbox" id="${id}"
        ${value ? 'checked' : ''} onchange="refreshConditional()">
        ${f.label}${advTag}</span>${hintHtml}</label>`;
  }
  if (f.type === 'libraries') {
    return `<div class="field">
      <span class="name">${f.label}${advTag}</span>
      ${hintHtml}<div class="chks" id="${id}">${
        libraryBoxes(f.libType, value)}</div></div>`;
  }

  let input;
  if (f.type === 'select') {
    input = `<select id="${id}" onchange="refreshConditional()">`
      + f.options.map((o) =>
        `<option ${o === value ? 'selected' : ''}>${o}</option>`).join('')
      + '</select>';
  } else if (f.type === 'model') {
    input = `<div class="inline"><input type="text" id="${id}"
      value="${escapeHtml(value)}" list="modelList"
      placeholder="e.g. qwen2.5:14b-instruct">
      <button class="btn ghost" onclick="detectModels()">Detect</button></div>
      <datalist id="modelList"></datalist><div id="modelMsg"></div>`;
  } else if (f.type === 'prompt') {
    // Locked by default. Editing needs an explicit unlock (with a warning),
    // and there's always a one-click reset to the built-in default.
    const custom = !!(value && value.trim());
    input = `<div class="msg info" style="margin:4px 0">⚠ Changing this can
      break recommendations — if the model stops returning valid picks,
      everything falls back to a rule-based list. There's a reset below.</div>
      <textarea id="${id}" rows="12" disabled
        style="max-width:640px;width:100%;font-family:ui-monospace,
        monospace;font-size:12px;line-height:1.5"
        placeholder="Using the built-in default. Click “Unlock to edit” to
        customise it.">${escapeHtml(value)}</textarea>
      <div class="row" style="margin-top:8px">
        <button class="btn ghost" type="button" onclick="unlockPrompt('${id}')">
          Unlock to edit</button>
        <button class="btn ghost" type="button" onclick="resetPrompt('${id}')">
          Reset to default</button>
        <span class="tag" id="${id}_state">${custom ? 'custom prompt'
          : 'using default'}</span>
      </div>`;
  } else if (f.type === 'preview') {
    input = `<input type="text" id="${id}" value="${escapeHtml(value)}"
      oninput="updatePreviews()"><div class="sub" id="${id}_prev"></div>`;
  } else if (f.type === 'time') {
    input = `<input type="time" id="${id}" value="${escapeHtml(value)}">`;
  } else if (f.type === 'list') {
    input = `<input type="text" id="${id}"
      value="${escapeHtml((value || []).join(', '))}">`;
  } else if (f.type === 'password') {
    input = `<input type="password" id="${id}"
      value="${value === '__set__' ? '__set__' : escapeHtml(value)}">`;
  } else {
    input = `<input type="${f.type}" id="${id}" value="${escapeHtml(value)}">`;
  }
  return `<label class="field">
    <span class="name">${f.label}${advTag}</span>${hintHtml}${input}</label>`;
}

function pickCard(id, value) {
  const box = $(id);
  box.dataset.value = value;
  box.querySelectorAll('.opt').forEach(
    (o) => o.classList.toggle('sel', o.dataset.v === value));
  refreshConditional();
}

/* Hide what cannot apply. An API-key box under Ollama is not a harmless extra
 * field — it implies Ollama might need one, which is exactly the confusion
 * this screen exists to remove. */
function refreshConditional() {
  const v = formValues();
  SECTIONS.forEach((sec) => sec.fields.forEach((f) => {
    const el = $(fid(f.k));
    const wrap = el && el.closest('.field');
    if (!wrap) return;
    // Only relevance hides a field now — advanced ones are always shown,
    // just labelled.
    wrap.classList.toggle('hidden', f.showIf ? !f.showIf(v) : false);
    // Re-render a hint that depends on other answers.
    if (typeof f.hint === 'function') {
      const h = wrap.querySelector('.hint');
      if (h) h.textContent = f.hint(v);
    }
  }));
  updatePreviews();
}

function updatePreviews() {
  const v = formValues();
  ['recommendations.playlist_title', 'recommendations.collection_title']
    .forEach((key) => {
      const box = $(`${fid(key)}_prev`);
      if (!box) return;
      box.textContent = 'Appears as: "' + String(v[key] || '')
        .replace('{library}', 'Movies').replace('{name}', 'Alex')
        .replace('{username}', 'alexdoe') + '"';
    });
  const note = $('nextRunNote');
  if (note && LAST_STATUS) {
    note.textContent = LAST_STATUS.next_run === 'disabled'
      ? 'No automatic runs — use Run now.'
      : `Next automatic run: ${LAST_STATUS.next_run}.`;
  }
}

function collectSettings() {
  const out = {};
  const v = formValues();
  SECTIONS.forEach((sec) => sec.fields.forEach((f) => {
    if (!$(fid(f.k))) return;
    const [s, k] = f.k.split('.');
    out[s] = out[s] || {};
    let value = v[f.k];
    if (f.type === 'number') value = Number(value);
    if (f.type === 'libraries') value = (value || []).join(', ');
    out[s][k] = value;
  }));
  return out;
}

async function saveSettings() {
  try {
    const settings = collectSettings();
    // A freshly signed-in server hands us a token ref, not the token; the
    // server resolves it. Drop the masked placeholder so we never write it.
    if (PLEX_TOKEN_REF) {
      settings.plex = settings.plex || {};
      settings.plex.token_ref = PLEX_TOKEN_REF;
      delete settings.plex.token;
    }
    const r = await api('/api/settings', {
      method: 'POST', body: JSON.stringify({ settings }),
    });
    msg($('saveMsg'), r.ok && !(r.problems || []).length ? 'ok' : 'bad',
      r.ok ? ((r.problems || []).join(' · ') || 'Saved.')
        : (r.error || 'could not save'));
    PLEX_TOKEN_REF = '';
    LIBRARIES = null;
    // Re-render from the saved connection so the library checkboxes populate
    // after a Sign in with Plex → Save. saveMsg lives outside the form, so the
    // status message above survives the re-render.
    if (r.ok) await loadSettings();
    loadStatus();
  } catch (e) {
    msg($('saveMsg'), 'bad', e.message);
  }
}

async function testConn(target) {
  const box = $(`test_${target}`);
  msg(box, 'info', 'testing…');
  const v = formValues();
  const values = {};
  Object.keys(v).forEach((key) => {
    const [s, k] = key.split('.');
    if (s === target) values[k] = v[key];
  });
  // After Sign in with Plex the token lives server-side behind a ref; send it
  // so Test can authenticate (and list libraries) before anything is saved.
  if (target === 'plex' && PLEX_TOKEN_REF) values.token_ref = PLEX_TOKEN_REF;
  const r = await api('/api/test', {
    method: 'POST', body: JSON.stringify({ target, values }),
  });
  box.innerHTML = `<div class="msg ${r.ok ? 'ok' : 'bad'}">
    ${escapeHtml(r.message)}</div>`
    + (r.detail ? `<div class="detail">${r.detail.map(escapeHtml)
      .join('<br>')}</div>` : '');
  // Keep the library checkboxes in step with the connection: a successful
  // Plex test lists them; a failed one clears them, since with no connection
  // there are no libraries to choose.
  if (target === 'plex') {
    LIBRARIES = r.ok && r.libraries ? r.libraries : [];
    refreshLibraryFields();
  }
}

async function resetPlex() {
  const box = $('test_plex');
  if (!window.confirm('Clear the saved Plex server address, token and library '
    + 'selection?\n\nYou\'ll need to sign in (or enter a URL) again. Nothing '
    + 'else in your settings is affected.')) return;
  msg(box, 'info', 'clearing…');
  try {
    const r = await api('/api/plex/reset', { method: 'POST' });
    if (!r.ok) { msg(box, 'bad', r.message || 'could not reset'); return; }
    PLEX_TOKEN_REF = '';
    LIBRARIES = null;
    await loadSettings();     // re-render: address/token blank, libraries empty
    msg($('test_plex'), 'ok', 'Plex connection cleared. Sign in or enter a '
      + 'server address to reconnect.');
  } catch (e) {
    msg(box, 'bad', e.message);
  }
}

async function testNotify() {
  const box = $('notifyMsg');
  const v = formValues();
  const url = v['notifications.webhook_url'];
  if (!url) { msg(box, 'bad', 'Enter a webhook URL first.'); return; }
  msg(box, 'info', 'sending…');
  try {
    const r = await api('/api/notify/test', {
      method: 'POST',
      body: JSON.stringify({ url, format: v['notifications.format'] }),
    });
    msg(box, r.ok ? 'ok' : 'bad', r.message);
  } catch (e) {
    msg(box, 'bad', e.message);
  }
}

async function unlockPrompt(id) {
  const ok = window.confirm(
    'Editing the AI prompt is an advanced escape hatch.\n\n'
    + 'If your version drops the rules that make the model return valid JSON '
    + 'picks, every recommendation will fail and fall back to a rule-based '
    + 'list. You can always reset to the default.\n\nUnlock and edit anyway?');
  if (!ok) return;
  const ta = $(id);
  ta.disabled = false;
  // Start from the built-in default if the box is empty, so there's something
  // to edit rather than a blank page.
  if (!ta.value.trim()) {
    try {
      const r = await api('/api/llm/default-prompt');
      ta.value = r.prompt || '';
    } catch (e) { /* leave blank */ }
  }
  ta.focus();
  const state = $(`${id}_state`);
  if (state) state.textContent = 'editing — unsaved';
}

async function resetPrompt(id) {
  const ta = $(id);
  ta.value = '';            // blank = use the built-in default
  ta.disabled = true;
  const state = $(`${id}_state`);
  if (state) state.textContent = 'using default';
  msg($('saveMsg'), 'info', 'Prompt cleared — press Save to use the built-in '
    + 'default again.');
}

async function detectModels() {
  const box = $('modelMsg');
  msg(box, 'info', 'asking the endpoint what it has…');
  const v = formValues();
  const q = new URLSearchParams({
    provider: v['llm.provider'] || 'ollama',
    url: v['llm.url'] || '',
    api_key: v['llm.api_key'] || '',
  });
  const r = await api(`/api/models?${q}`);
  if (!r.ok) {
    msg(box, 'bad', (r.message || 'nothing found')
      + (v['llm.provider'] === 'ollama'
        ? ' · Is Ollama running, and reachable from this machine?'
        : ' · Check the address and the key.'));
    return;
  }
  $('modelList').innerHTML = r.models.map(
    (m) => `<option value="${escapeHtml(m)}">`).join('');
  msg(box, 'ok', `${r.models.length} available: ${r.models.join(', ')}`);
}

/* --------------------------------------------------------------- setup */
function fillWizard() {
  $('w_plex_url').value = (SETTINGS.plex && SETTINGS.plex.url) || '';
  $('w_llm_provider').value = (SETTINGS.llm && SETTINGS.llm.provider)
    || 'ollama';
  $('w_llm_url').value = (SETTINGS.llm && SETTINGS.llm.url) || '';
  $('w_llm_provider').onchange = () => {
    $('w_llm_key_wrap').classList.toggle(
      'hidden', $('w_llm_provider').value === 'ollama');
  };
  $('w_llm_provider').onchange();
}

function showManualLLM() {
  $('w_llm_manual').classList.remove('hidden');
  $('w_llm_provider').value = 'openai';
  $('w_llm_provider').onchange();
}

/* Sign in with Plex: request a PIN, send them to plex.tv, poll until it is
 * approved. Same flow Overseerr and Tautulli use, and the reason nobody has
 * to go hunting for X-Plex-Token. */
async function plexSignIn(fromSettings) {
  const box = fromSettings ? $('test_plex') : $('w_plex_msg');
  msg(box, 'info', 'opening Plex…');
  const pin = await api('/api/plex/pin', { method: 'POST' });
  if (!pin.ok) { msg(box, 'bad', pin.message); return; }

  const popup = window.open(pin.url, 'plex-auth',
    'width=680,height=760,menubar=no,toolbar=no');
  msg(box, 'info', popup
    ? 'Waiting for you to approve it in the Plex window…'
    : `Popup blocked — open this to approve: ${pin.url}`);

  const deadline = Date.now() + 300000;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 2000));
    let r;
    try {
      r = await api(`/api/plex/pin?id=${encodeURIComponent(pin.id)}`);
    } catch (e) { continue; }
    if (!r.ok) { msg(box, 'bad', r.message); return; }
    if (r.authenticated) {
      if (popup) popup.close();
      msg(box, 'ok', 'Signed in. Looking for your servers…');
      await loadPlexServers(fromSettings);
      return;
    }
  }
  msg(box, 'bad', 'Timed out waiting for approval. Try again.');
}

async function loadPlexServers(fromSettings) {
  const box = fromSettings ? $('test_plex') : $('w_plex_msg');
  const r = await api('/api/plex/servers');
  if (!r.ok) { msg(box, 'bad', r.message); return; }
  const usable = r.servers.filter((s) => s.best);
  if (!usable.length) {
    msg(box, 'bad', 'Signed in, but none of your servers answered from here. '
      + 'Enter the address by hand.');
    return;
  }
  // The server keeps the real token; we only ever carry an opaque ref.
  if (fromSettings) {
    $(fid('plex.url')).value = usable[0].best;
    PLEX_TOKEN_REF = usable[0].token_ref;
    // List the libraries straight away (the token ref is resolved server-side)
    // so they can be ticked before saving. testConn writes into this box.
    await testConn('plex');
    $('test_plex').innerHTML += '<div class="detail">Signed in as '
      + `${escapeHtml(usable[0].name)} — press Save to keep this connection.`
      + '</div>';
    return;
  }
  $('w_servers').classList.remove('hidden');
  $('w_server').innerHTML = usable.map((s) => {
    const local = s.connections.find((c) => c.reachable && c.local);
    return `<option value="${escapeHtml(s.best)}"
      data-ref="${escapeHtml(s.token_ref)}">${escapeHtml(s.name)}
      — ${escapeHtml(s.best)}${local ? ' (local)' : ''}</option>`;
  }).join('');
  $('w_plex_url').value = usable[0].best;
  PLEX_TOKEN_REF = usable[0].token_ref;
  $('w_server').onchange = () => {
    const opt = $('w_server').selectedOptions[0];
    $('w_plex_url').value = opt.value;
    PLEX_TOKEN_REF = opt.dataset.ref;
  };
  msg(box, 'ok', `Found ${usable.length} reachable server`
    + (usable.length === 1 ? '.' : 's.'));
}

/* Look for a model server on the usual hosts and ports, so nobody has to know
 * what an "endpoint" is. */
async function probeLLM(fromSettings) {
  const box = fromSettings ? $('modelMsg') : $('w_llm_msg');
  msg(box, 'info', 'looking for a model server…');
  const r = await api('/api/llm/probe');
  if (!r.ok) {
    msg(box, 'bad', r.message);
    if (!fromSettings) showManualLLM();
    return;
  }
  const found = r.found[0];
  if (fromSettings) {
    $(fid('llm.provider')).dataset.value = found.provider;
    pickCard(fid('llm.provider'), found.provider);
    $(fid('llm.url')).value = found.url;
    $(fid('llm.model')).value = found.recommended;
    msg(box, 'ok', `Found ${found.provider} at ${found.url}. `
      + `Using ${found.recommended}. Press Save.`);
    return;
  }
  $('w_llm_manual').classList.remove('hidden');
  $('w_llm_provider').value = found.provider;
  $('w_llm_provider').onchange();
  $('w_llm_url').value = found.url;
  $('w_llm_model').innerHTML = found.models.map(
    (m) => `<option ${m === found.recommended ? 'selected' : ''}>
      ${escapeHtml(m)}</option>`).join('');
  msg(box, 'ok', `Found ${found.provider} at ${found.url} with `
    + `${found.models.length} models. Using ${found.recommended}.`);
}

async function wizardTest(target) {
  const box = $(`w_${target}_msg`);
  msg(box, 'info', 'testing…');
  const values = target === 'plex'
    ? { url: $('w_plex_url').value, token: $('w_plex_token').value }
    : {};
  const r = await api('/api/test', {
    method: 'POST', body: JSON.stringify({ target, values }),
  });
  msg(box, r.ok ? 'ok' : 'bad', r.message);
}

async function wizardDetect() {
  const box = $('w_llm_msg');
  msg(box, 'info', 'looking for models…');
  const q = new URLSearchParams({
    provider: $('w_llm_provider').value,
    url: $('w_llm_url').value,
    api_key: $('w_llm_api_key').value,
  });
  const r = await api(`/api/models?${q}`);
  if (!r.ok) {
    msg(box, 'bad', (r.message || 'nothing found')
      + ' · No local model? Switch to an OpenAI-compatible API and paste a key.');
    return;
  }
  $('w_llm_model').innerHTML = r.models.map(
    (m) => `<option>${escapeHtml(m)}</option>`).join('');
  msg(box, 'ok', `found ${r.models.length} models — pick one above`);
}

async function wizardSave() {
  const plex = { url: $('w_plex_url').value };
  // Prefer the server-held token ref from Sign in with Plex; fall back to a
  // hand-typed token only if the manual box was used.
  if (PLEX_TOKEN_REF) plex.token_ref = PLEX_TOKEN_REF;
  else if ($('w_plex_token').value) plex.token = $('w_plex_token').value;
  const settings = {
    plex,
    llm: {
      provider: $('w_llm_provider').value,
      url: $('w_llm_url').value,
      api_key: $('w_llm_api_key').value,
      model: $('w_llm_model').value,
    },
  };
  try {
    const r = await api('/api/settings', {
      method: 'POST', body: JSON.stringify({ settings }),
    });
    if (r.problems && r.problems.length) {
      msg($('w_msg'), 'bad', r.problems.join(' · '));
      return;
    }
    msg($('w_msg'), 'ok', 'Saved.');
    PLEX_TOKEN_REF = '';
    SETTINGS = null;
    loadStatus();
  } catch (e) {
    msg($('w_msg'), 'bad', e.message);
  }
}

/* -------------------------------------------------------------- system */
async function loadRuns() {
  const s = await api('/api/status');
  $('sysStatus').innerHTML = `
    <div><b>Version</b> ${escapeHtml(s.version)}</div>
    <div><b>Config</b> ${escapeHtml(s.config_dir)}</div>
    <div><b>Schedule</b> ${escapeHtml(s.schedule)}
      ${s.schedule_time ? 'at ' + escapeHtml(s.schedule_time) : ''}
      · next ${escapeHtml(s.next_run)}</div>
    <div><b>Configured</b> ${s.configured ? 'yes'
      : escapeHtml((s.problems || []).join(' · '))}</div>`;
  const d = await api('/api/runs');
  const tbody = $('runsTable').querySelector('tbody');
  tbody.innerHTML = d.runs.map((r) => {
    const mode = r.failed ? '<span style="color:var(--bad)">failed</span>'
      : r.dry ? 'generated' : 'published';
    const err = r.failed && (r.errors || []).length
      ? ` — ${escapeHtml(r.errors[0])}` : '';
    return `<tr>
      <td>${new Date(r.at * 1000).toLocaleString()}</td>
      <td>${mode}${err}</td>
      <td class="num">${r.users}</td><td class="num">${r.personalized}</td>
      <td class="num">${r.written || 0}</td>
      <td class="num">${r.seconds}</td></tr>`;
  }).join('') || '<tr><td colspan="6">no runs yet</td></tr>';
}

async function loadLogs() {
  const d = await api('/api/logs?lines=400');
  $('logBox').textContent = d.lines.join('\n') || '(empty)';
  // Show newest lines — scroll the viewer to the bottom.
  const box = $('logBox');
  box.scrollTop = box.scrollHeight;
}

/* Downloads are browser navigations, which cannot set an X-Api-Key header, so
 * the key rides as a query param (which _authorized also accepts). */
function _download(path) {
  const sep = path.includes('?') ? '&' : '?';
  const url = API_KEY ? `${path}${sep}apikey=${encodeURIComponent(API_KEY)}`
    : path;
  window.location = url;
}
function downloadBackup() { _download('/api/backup'); }
function downloadLog() { _download('/api/logs/download'); }

loadStatus().then(() => { if (location.hash) showFromHash(); });
setInterval(loadStatus, 15000);
