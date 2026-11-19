// Agent Runs for Hermes Desktop: the coding-agent runs on this computer, live and read-only.
//
// A pane in the right sidebar lists the runs the backend (dashboard/plugin_api.py) finds, newest
// activity first, and opens one to follow its log. A status-bar chip counts the live runs and a
// ⌘K command opens the pane. Hermes chat titles come from Hermes' own session listings (gateway
// `session.list` and the desktop's saved-sessions read), so the backend never opens Hermes'
// database. The plugin only reads: it never stops or talks to a run.
import { useQuery, useQueryClient, host, PALETTE_AREA, StatusDot, LogView, EmptyState, ErrorState,
  SegmentedControl, Button, Skeleton, Tip } from '@hermes/plugin-sdk';
import { useEffect, useRef, useState } from 'react';
import { jsx, jsxs } from 'react/jsx-runtime';

const ID = 'agent-runs';
const PANE = 'runs';
const PANE_ID = `${ID}:${PANE}`; // contribution ids are namespaced by the host
const LIST_MS = 5000;
const EVENTS_MS = 2000;
const MAX_EVENTS = 2000;
const TITLES_MS = 5 * 60000; // renames also arrive as events; this only catches missed ones
const TITLES_RETRY_MS = 60000; // a run whose chat has no title yet asks again at most this often

const muted = { color: 'var(--ui-text-tertiary)' };
const faint = { color: 'var(--ui-text-quaternary)' };
const strong = { color: 'var(--ui-text-primary)' };
const hairline = { borderBottom: '1px solid var(--ui-stroke-secondary)' };

const TONE = { running: 'good', waiting: 'warn', quiet: 'muted', finished: 'muted' };
const STATUS_LABEL = { running: 'Running', waiting: 'Waiting for you', quiet: 'Quiet', finished: 'Finished' };

// --- helpers -----------------------------------------------------------------------------------

const errorText = e => String((e && e.message) || e || 'Something went wrong').replace(/^\d{3}:\s*/, '')
  .replace(/^\{"detail":"(.*)"\}$/, '$1');

class ShapeError extends Error {
  constructor(payload) {
    super('The Agent Runs backend sent a reply this page does not understand.');
    this.payload = payload;
  }
}

const isRun = r => r && typeof r === 'object' && typeof r.id === 'string' && typeof r.status === 'string'
  && r.origin && typeof r.origin === 'object';
const isRunsPayload = d => d && typeof d === 'object' && Array.isArray(d.runs) && d.runs.every(isRun)
  && Array.isArray(d.warnings);
const isEventsPayload = d => d && typeof d === 'object' && Array.isArray(d.events)
  && d.events.every(e => e && typeof e === 'object' && typeof e.kind === 'string');

function duration(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${String(m % 60).padStart(2, '0')}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

const rtf = typeof Intl !== 'undefined' && Intl.RelativeTimeFormat
  ? new Intl.RelativeTimeFormat(undefined, { numeric: 'auto', style: 'short' }) : null;

function ago(ts) {
  if (typeof ts !== 'number') return '';
  const s = Math.round((ts - Date.now()) / 1000);
  const abs = Math.abs(s);
  if (!rtf) return `${duration(abs * 1000)} ago`;
  if (abs < 60) return rtf.format(s, 'second');
  if (abs < 3600) return rtf.format(Math.round(s / 60), 'minute');
  if (abs < 86400) return rtf.format(Math.round(s / 3600), 'hour');
  return rtf.format(Math.round(s / 86400), 'day');
}

const clock = ts => (typeof ts === 'number'
  ? new Date(ts).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '');
// Always five characters (14:05), so the log's left column stays narrow in every locale.
const shortClock = ts => (typeof ts === 'number'
  ? new Date(ts).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }) : '');

function runtime(run) {
  if (typeof run.started_at !== 'number') return '';
  const end = run.status === 'finished' && typeof run.last_activity_at === 'number' ? run.last_activity_at : Date.now();
  return duration(end - run.started_at);
}

// --- chat titles >>> (pure: no imports, no host; tests/js/titles.test.mjs loads this block alone)

// The two gateway wires answer `session.list` differently. The classic gateway
// (tui_gateway/methods_session.py) lists stored chats, rows keyed `id` (+ `_lineage_root_id`), and
// refuses unknown params. The session-authority gateway (gateway/session_controls.py) takes
// `limit` 1..200 only, lists LIVE chats unless `scope: 'stored'`, and keys rows `session_id`, `id`
// (lineage root) and `resolved_id` (current segment). The desktop's saved-sessions read (the SDK's
// listPersistedSessions) has `id`, `_lineage_root_id` and `_lineage_ids`. A chat is known
// under any of those ids, so each one maps to the title.
const OWN_ID_FIELDS = ['id', 'session_id', 'resolved_id'];
const isId = v => typeof v === 'string' && v.length > 0;

/** The rows of a session listing reply (`{ sessions: [...] }` or a bare array); null if none. */
function listingRows(reply) {
  if (Array.isArray(reply)) return reply;
  if (reply && typeof reply === 'object' && Array.isArray(reply.sessions)) return reply.sessions;
  return null;
}

/** A listing row's title with the ids that are the row itself (`own`) and the ids of the earlier
 *  segments it continues (`linked`); null for a row without a usable title or id. */
function rowTitle(row) {
  if (!row || typeof row !== 'object') return null;
  const raw = [row.title, row.root_title].find(t => typeof t === 'string' && t.trim());
  if (!raw) return null;
  const own = [...new Set(OWN_ID_FIELDS.map(f => row[f]).filter(isId))];
  const linked = [row._lineage_root_id].concat(Array.isArray(row._lineage_ids) ? row._lineage_ids : [])
    .filter(id => isId(id) && !own.includes(id));
  return own.length || linked.length ? { title: raw.trim(), own, linked: [...new Set(linked)] } : null;
}

/**
 * Merge listing replies into one id → title map, keeping what each source did for the debug line.
 * `results`: `[{ source, reply }]`, `[{ source, error }]` or `[{ source, skipped }]`. An id that is
 * a row of its own keeps that row's title over a later row that only names it as an earlier segment.
 */
function indexTitles(results) {
  const titles = {};
  const own = new Set();
  const report = [];
  for (const r of results) {
    if (r.error !== undefined || r.skipped !== undefined) {
      report.push({ source: r.source, ok: false, error: String(r.error !== undefined ? r.error : r.skipped) });
      continue;
    }
    const rows = listingRows(r.reply);
    if (!rows) {
      report.push({ source: r.source, ok: false, error: 'reply has no session list' });
      continue;
    }
    let titled = 0;
    for (const row of rows) {
      const found = rowTitle(row);
      if (!found) continue;
      titled += 1;
      for (const id of found.own) {
        titles[id] = found.title;
        own.add(id);
      }
      for (const id of found.linked) if (!own.has(id)) titles[id] = found.title;
    }
    report.push({ source: r.source, ok: true, rows: rows.length, titled });
  }
  return { titles, report };
}

/** The title for a Hermes chat id: exact, else the one id it uniquely abbreviates (`local-` + 16 hex). */
function chatTitle(titles, id) {
  if (typeof id !== 'string' || !id || !titles) return undefined;
  if (Object.prototype.hasOwnProperty.call(titles, id)) return titles[id];
  if (id.length < 16) return undefined;
  const keys = Object.keys(titles).filter(k => k.startsWith(id));
  return keys.length === 1 ? titles[keys[0]] : undefined;
}

/** One line saying why no title was found for `id`, from `indexTitles`' report. */
function titleMissReason(id, report) {
  const short = typeof id === 'string' && id.length > 20 ? `${id.slice(0, 20)}…` : String(id);
  const parts = (report || []).map(r => (r.ok
    ? `${r.source}: ${r.rows} chat${r.rows === 1 ? '' : 's'}, none with this id`
    : `${r.source}: ${r.error}`));
  return `No title for chat ${short}: ${parts.length ? parts.join('; ') : 'not looked up yet'}.`;
}

/** A title change pushed by the gateway, as `[id, title]`, or null. */
function titleFromEvent(event) {
  const p = event && event.payload;
  if (!p || typeof p.title !== 'string' || !p.title.trim()) return null;
  const id = typeof p.session_id === 'string' ? p.session_id : event.session_id;
  return typeof id === 'string' && id ? [id, p.title.trim()] : null;
}

/** The Hermes chat ids whose titles label these runs (each once). */
function chatIds(runs) {
  const ids = runs.filter(r => r && r.origin && r.origin.kind === 'hermes_chat').map(r => r.origin.hermes_session_id);
  return [...new Set(ids.filter(isId))];
}

function originLabel(origin, titles) {
  if (origin.kind === 'hermes_chat' && origin.hermes_session_id) {
    const title = chatTitle(titles, origin.hermes_session_id);
    if (title) return title;
  }
  return origin.label || 'Other';
}

// <<< chat titles

function openPane() {
  if (typeof host.revealPane === 'function') host.revealPane(PANE_ID);
  else host.notify({ kind: 'info', message: 'Agent Runs is in the right sidebar.' });
}

function openChat(sessionId) {
  if (typeof host.openSession === 'function') {
    Promise.resolve(host.openSession(sessionId)).catch(e => host.notify({ kind: 'error', message: errorText(e) }));
  } else {
    host.navigate(`/${encodeURIComponent(sessionId)}`); // the app's own route for a stored chat
  }
}

// --- data --------------------------------------------------------------------------------------

const runsKey = scope => [ID, 'runs', scope || 'default'];

function useRuns(ctx, scope) {
  return useQuery({
    queryKey: runsKey(scope),
    queryFn: async () => {
      const res = await ctx.rest(scope ? `/runs?scope=${scope}` : '/runs', { timeoutMs: 20000 });
      if (!isRunsPayload(res)) throw new ShapeError(res);
      return res;
    },
    refetchInterval: LIST_MS,
    retry: 1,
  });
}

function activeProfile() {
  try {
    const p = host.state && host.state.profile && typeof host.state.profile.get === 'function' ? host.state.profile.get() : '';
    return typeof p === 'string' && p.trim() ? p.trim() : 'default';
  } catch {
    return 'default';
  }
}

// Every source is asked each time, because which one answers depends on the gateway wire (see the
// chat titles block): `scope: 'stored'` is refused by the classic gateway as an unknown param, and
// the plain call lists only open chats on the session-authority gateway. Limits stay within what
// both accept (200 for session.list; the saved-sessions read caps itself at 500).
const TITLE_SOURCES = [
  { source: 'session.list stored', run: () => host.request('session.list', { limit: 200, include_hidden: true, scope: 'stored' }) },
  { source: 'session.list', run: () => host.request('session.list', { limit: 200, include_hidden: true }) },
  { source: 'saved sessions', run: () => (typeof host.listPersistedSessions === 'function'
    ? host.listPersistedSessions(null, { profile: activeProfile(), limit: 500 }) : undefined) },
];

async function lookUpTitles() {
  const results = await Promise.all(TITLE_SOURCES.map(async ({ source, run }) => {
    try {
      const reply = await run();
      return reply === undefined ? { source, skipped: 'not offered by this Hermes version' } : { source, reply };
    } catch (e) {
      return { source, error: errorText(e) };
    }
  }));
  return indexTitles(results);
}

const TITLE_EVENTS = ['session.title', 'session.updated'];

// Hermes chat id → title, from Hermes' own listings; kept fresh by the gateway's title events.
// Returns the map and, for wanted chats with no title, why (shown folded in the list footer).
function useChatTitles(ctx, wanted) {
  const qc = useQueryClient();
  const key = [ID, 'titles'];
  const query = useQuery({ queryKey: key, queryFn: lookUpTitles, staleTime: TITLES_MS, refetchInterval: TITLES_MS, retry: 0 });
  useEffect(() => {
    const offs = TITLE_EVENTS.map(type => ctx.onEvent(type, event => {
      const found = titleFromEvent(event);
      if (found) {
        qc.setQueryData(key, old => ({ report: [], ...(old || {}), titles: { ...((old && old.titles) || {}), [found[0]]: found[1] } }));
      }
    }));
    return () => offs.forEach(off => typeof off === 'function' && off());
  }, []);
  const titles = (query.data && query.data.titles) || {};
  const missing = query.data ? wanted.filter(id => !chatTitle(titles, id)) : [];
  // A chat we have not seen yet (just started, or titled after its first turn): ask again soon
  // rather than at the next full refresh, but not more often than once a minute.
  // The pane re-renders with every runs poll; the minute bucket lets this effect re-check then.
  const minute = Math.floor(Date.now() / TITLES_RETRY_MS);
  useEffect(() => {
    if (missing.length && !query.isFetching && Date.now() - query.dataUpdatedAt > TITLES_RETRY_MS) query.refetch();
  }, [missing.length, query.isFetching, query.dataUpdatedAt, minute]);
  const misses = missing.map(id => titleMissReason(id, query.data && query.data.report));
  return { titles, misses };
}

function useEvents(ctx, runId, enabled) {
  const qc = useQueryClient();
  const key = [ID, 'events', runId];
  // Drop the log when the view closes, so opening it again starts from the end of the file.
  useEffect(() => () => qc.removeQueries({ queryKey: key }), [runId]);
  return useQuery({
    queryKey: key,
    enabled,
    queryFn: async () => {
      const prev = qc.getQueryData(key);
      const qs = new URLSearchParams({ limit: '300' });
      if (prev && prev.cursor != null) qs.set('cursor', prev.cursor);
      const res = await ctx.rest(`/runs/${encodeURIComponent(runId)}/events?${qs}`, { timeoutMs: 20000 });
      if (!isEventsPayload(res)) throw new ShapeError(res);
      let seq = prev ? prev.seq : 0;
      const fresh = res.events.map(e => ({ ...e, key: ++seq }));
      const base = !prev || res.reset ? [] : prev.events;
      return { events: base.concat(fresh).slice(-MAX_EVENTS), cursor: res.cursor, done: !!res.done,
        hasLog: res.has_log !== false, note: res.note || null, seq };
    },
    refetchInterval: q => (q.state.data && q.state.data.done ? false : EVENTS_MS),
    refetchOnWindowFocus: false,
    retry: 1,
  });
}

// --- pieces ------------------------------------------------------------------------------------

function Problem({ title, error, onRetry }) {
  const payload = error instanceof ShapeError ? error.payload : undefined;
  return jsx('div', { className: 'p-4', children: jsx(ErrorState, {
    title,
    description: payload !== undefined
      ? 'The backend may be an older version: restart the Hermes gateway once (hermes gateway restart).'
      : `${errorText(error)}. If you just installed or updated the plugin, restart the Hermes gateway (hermes gateway restart).`,
    children: [
      payload !== undefined && jsx(LogView, { key: 'payload', className: 'max-h-48 text-left',
        children: (() => { try { return JSON.stringify(payload, null, 2).slice(0, 4000); } catch { return String(payload); } })() }),
      onRetry && jsx(Button, { key: 'retry', variant: 'outline', size: 'sm', onClick: onRetry, children: 'Try again' }),
    ],
  }) });
}

function Row({ run, titles, onOpen }) {
  const [hover, setHover] = useState(false);
  const finished = run.status === 'finished';
  const bits = [originLabel(run.origin, titles), runtime(run), run.last_activity_at ? `active ${ago(run.last_activity_at)}` : '']
    .filter(Boolean);
  return jsxs('button', {
    type: 'button',
    onClick: () => onOpen(run.id),
    onMouseEnter: () => setHover(true),
    onMouseLeave: () => setHover(false),
    className: 'flex w-full flex-col gap-0.5 rounded-md px-2 py-1.5 text-left',
    style: { opacity: finished ? 0.6 : 1, background: hover ? 'var(--ui-row-hover-background)' : 'transparent' },
    children: [
      jsxs('div', { className: 'flex min-w-0 items-center gap-2', children: [
        jsx(StatusDot, { tone: TONE[run.status] || 'muted', className: 'shrink-0' }),
        jsx('span', { className: 'min-w-0 truncate text-sm font-medium', style: strong, children: run.folder || run.cwd || `pid ${run.pid}` }),
        run.embedded && jsx('span', { className: 'shrink-0 text-[0.6875rem]', style: faint, children: 'model call' }),
      ] }),
      jsx('div', { className: 'truncate pl-3.5 text-xs', style: muted, children: bits.join(' · ') }),
      run.latest && jsx('div', { className: 'truncate pl-3.5 font-mono text-[0.6875rem]', style: faint, children: run.latest }),
    ],
  });
}

function RunList({ ctx, scope, setScope, onOpen }) {
  const runs = useRuns(ctx, scope);
  const data = runs.data;
  const wanted = data ? chatIds(data.runs) : [];
  const { titles, misses } = useChatTitles(ctx, wanted);

  if (runs.isPending) return jsx('div', { className: 'p-3', children: jsx(Skeleton, { className: 'h-24 w-full' }) });
  if (runs.isError) return jsx(Problem, { title: 'Agent Runs is not ready', error: runs.error, onRetry: () => runs.refetch() });

  const current = data.scope === 'all' ? 'all' : 'hermes';
  const live = data.runs.filter(r => r.status !== 'finished');
  const finished = data.runs.filter(r => r.status === 'finished');
  return jsxs('div', { className: 'flex h-full min-h-0 flex-col', children: [
    jsxs('div', { className: 'flex items-center gap-2 px-3 py-2', style: hairline, children: [
      jsx(SegmentedControl, { value: current, onChange: setScope,
        options: [{ id: 'hermes', label: 'Hermes' }, { id: 'all', label: 'All' }] }),
      jsx('span', { className: 'ml-auto text-xs', style: faint, children: live.length ? `${live.length} live` : '' }),
    ] }),
    jsxs('div', { className: 'min-h-0 flex-1 overflow-y-auto px-1 py-1', children: [
      data.runs.length === 0 && jsx(EmptyState, {
        title: current === 'hermes' ? 'No agent runs started by Hermes right now' : 'No agent runs on this computer right now',
        description: current === 'hermes'
          ? 'Coding agents that a Hermes chat or cron job starts show up here. Switch to All for every run on this computer.'
          : 'Claude Code runs show up here as soon as they start, however they were started.',
      }),
      ...live.map(run => jsx(Row, { key: run.id, run, titles, onOpen })),
      finished.length > 0 && jsx('div', { key: 'finished', className: 'px-2 pb-1 pt-3 text-[0.6875rem] uppercase tracking-wide', style: faint, children: 'Finished' }),
      ...finished.map(run => jsx(Row, { key: run.id, run, titles, onOpen })),
    ] }),
    (data.warnings.length > 0 || data.hidden_embedded > 0 || misses.length > 0) && jsxs('div', { className: 'space-y-0.5 px-3 py-2 text-[0.6875rem]',
      style: { ...faint, borderTop: '1px solid var(--ui-stroke-secondary)' }, children: [
        data.hidden_embedded > 0 && jsx('div', { key: 'embedded', children: `${data.hidden_embedded} Hermes model call${data.hidden_embedded === 1 ? '' : 's'} hidden (Settings → Plugins → Agent Runs).` }),
        ...data.warnings.slice(0, 5).map((w, i) => jsx('div', { key: `w${i}`, className: 'truncate', title: w, children: w })),
        misses.length > 0 && jsxs('details', { key: 'titles', children: [
          jsx('summary', { style: { cursor: 'pointer' }, children: `Chat title not found for ${misses.length} run${misses.length === 1 ? '' : 's'}` }),
          ...misses.map((m, i) => jsx('div', { key: i, style: { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', paddingTop: '0.125rem' }, children: m })),
        ] }),
      ] }),
  ] });
}

// One mono character per kind (every glyph is ASCII so it is one column wide in any mono font); the
// kind's name is in the line's tooltip.
const KIND = {
  prompt: { mark: '>', name: 'prompt', style: { color: 'var(--ui-accent)' } },
  text: { mark: '*', name: 'says', style: { color: 'var(--ui-text-primary)' } },
  thinking: { mark: '~', name: 'thinks', style: { color: 'var(--ui-text-quaternary)', fontStyle: 'italic' } },
  tool: { mark: '$', name: 'tool', style: { color: 'var(--ui-text-secondary)' } },
  result: { mark: '=', name: 'result', style: { color: 'var(--ui-text-quaternary)' } },
  error: { mark: '!', name: 'error', style: { color: 'var(--color-destructive)' } },
  system: { mark: '#', name: 'system', style: { color: 'var(--ui-text-tertiary)' } },
};

// Layout of one log line. LogView is a plain block (whitespace-pre-wrap, break-words); each line
// is its own two-column grid: a fixed 7ch gutter (kind mark + HH:MM) and a text column that takes
// every remaining pixel (`minmax(0, 1fr)`, so long words cannot widen it) and breaks long paths
// and commands anywhere instead of overflowing. Inline styles only: the app's Tailwind build does
// not scan plugins, so a class works only if core happens to use it too.
const lineStyle = { display: 'grid', gridTemplateColumns: '7ch minmax(0, 1fr)', columnGap: '0.5ch', alignItems: 'baseline',
  padding: '0.125rem 0' };
const gutterStyle = { whiteSpace: 'nowrap', overflow: 'hidden', fontVariantNumeric: 'tabular-nums' };
const textStyle = { minWidth: 0, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', wordBreak: 'break-word' };
const detailStyle = { ...textStyle, gridColumn: '2', color: 'var(--ui-text-quaternary)', fontStyle: 'normal' };

/** What a log line shows: `head` on the first line, optional `body` under it. */
function eventText(ev) {
  const title = ev.title || '';
  const detail = ev.detail || '';
  // These kinds carry their text in `detail`; their title is only a label.
  if (ev.kind === 'prompt' || ev.kind === 'result' || ev.kind === 'text' || (ev.kind === 'error' && title === 'Error')) {
    return { head: detail || title, body: '' };
  }
  if (ev.kind === 'tool') return { head: title, body: detail, inline: true };
  return { head: title || detail, body: title ? detail : '' };
}

function EventLine({ ev }) {
  const kind = KIND[ev.kind] || KIND.system;
  const { head, body, inline } = eventText(ev);
  return jsxs('div', { style: lineStyle, title: `${kind.name} · ${clock(ev.ts)}`, children: [
    jsxs('span', { style: { ...gutterStyle, ...faint }, children: [
      jsx('span', { style: { ...kind.style, fontStyle: 'normal' }, children: kind.mark }),
      ` ${shortClock(ev.ts)}`,
    ] }),
    jsxs('div', { style: { ...textStyle, ...kind.style }, children: [
      head,
      // A tool's command follows its name on the same line, wrapping under it.
      inline && body && jsx('span', { style: { color: 'var(--ui-text-tertiary)' }, children: `  ${body}` }),
    ] }),
    !inline && body && jsx('div', { style: detailStyle, children: body }),
  ] });
}

function EventLog({ events }) {
  const box = useRef(null);
  const follow = useRef(true);
  // Follow new events while the view is at the bottom; stop when the user scrolls up, and resume
  // once they scroll back down.
  const onScroll = () => {
    const el = box.current;
    if (el) follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  };
  useEffect(() => {
    const el = box.current;
    if (el && follow.current) el.scrollTop = el.scrollHeight;
  }, [events]);
  // LogView supplies the mono font, size and selectable text; its own border and padding would
  // take another ~22px from a 300px pane, so they are cleared here.
  return jsx('div', { ref: box, onScroll, style: { flex: '1 1 0%', minHeight: 0, minWidth: 0, overflowY: 'auto', overflowX: 'hidden',
    padding: '0.5rem 0.625rem' }, children:
    jsx(LogView, { style: { overflow: 'visible', border: 0, borderRadius: 0, padding: 0 },
      children: events.map(ev => jsx(EventLine, { key: ev.key, ev })) }) });
}

function Detail({ ctx, scope, runId, onBack }) {
  const runs = useRuns(ctx, scope);
  const run = runs.data ? runs.data.runs.find(r => r.id === runId) : null;
  const { titles } = useChatTitles(ctx, run ? chatIds([run]) : []);
  const events = useEvents(ctx, runId, !!(run && run.has_log));

  const back = jsx(Button, { variant: 'ghost', size: 'sm', onClick: onBack, children: '← Runs' });
  if (!run) {
    return jsxs('div', { className: 'flex h-full flex-col', children: [
      jsx('div', { className: 'px-2 py-1.5', style: hairline, children: back }),
      runs.isPending
        ? jsx('div', { className: 'p-3', children: jsx(Skeleton, { className: 'h-24 w-full' }) })
        : jsx(EmptyState, { title: 'This run is no longer listed', description: 'It finished longer ago than the plugin keeps runs, or it is outside the current view.' }),
    ] });
  }
  const chat = run.origin.kind === 'hermes_chat' && run.origin.hermes_session_id;
  const facts = [STATUS_LABEL[run.status] || run.status, runtime(run), run.model, run.agent_label].filter(Boolean);
  let body;
  if (!run.has_log) {
    body = jsx(EmptyState, { title: 'No live log', description: run.log_note || 'This run has no log to show.' });
  } else if (events.isError) {
    body = jsx(Problem, { title: 'Could not read this run\'s log', error: events.error, onRetry: () => events.refetch() });
  } else if (events.isPending) {
    body = jsx('div', { className: 'p-3', children: jsx(Skeleton, { className: 'h-40 w-full' }) });
  } else if (!events.data.hasLog) {
    body = jsx(EmptyState, { title: 'No live log', description: events.data.note || 'This run has no log to show.' });
  } else if (events.data.events.length === 0) {
    body = jsx(EmptyState, { title: 'Nothing in the log yet' });
  } else {
    body = jsx(EventLog, { events: events.data.events });
  }
  return jsxs('div', { className: 'flex h-full min-h-0 flex-col', children: [
    jsxs('div', { className: 'space-y-1 px-2 py-1.5', style: hairline, children: [
      back,
      jsxs('div', { className: 'flex min-w-0 items-center gap-2 px-1', children: [
        jsx(StatusDot, { tone: TONE[run.status] || 'muted', className: 'shrink-0' }),
        jsx(Tip, { label: run.cwd || '', children: jsx('span', { className: 'min-w-0 truncate text-sm font-medium', style: strong, children: run.folder || run.cwd || `pid ${run.pid}` }) }),
      ] }),
      jsxs('div', { className: 'flex flex-wrap items-center gap-x-2 px-1 text-xs', style: muted, children: [
        jsx('span', { children: originLabel(run.origin, titles) }),
        chat && jsx('button', { type: 'button', className: 'underline', style: { color: 'var(--ui-accent)' },
          onClick: () => openChat(run.origin.hermes_session_id), children: 'Open chat' }),
        jsx('span', { children: facts.join(' · ') }),
      ] }),
      run.args_summary && jsx('div', { className: 'truncate px-1 font-mono text-[0.6875rem]', style: faint, title: run.args_summary, children: run.args_summary }),
    ] }),
    body,
  ] });
}

function Pane({ ctx }) {
  const [scope, setScope] = useState(null); // null: the default scope from the plugin settings
  const [open, setOpen] = useState(null);
  const qc = useQueryClient();
  useEffect(() => ctx.onEvent(`plugin.${ID}.settings.changed`, () => qc.invalidateQueries({ queryKey: [ID, 'runs'] })), []);
  return jsx('div', { style: { height: '100%', width: '100%' }, children: open
    ? jsx(Detail, { ctx, scope, runId: open, onBack: () => setOpen(null) })
    : jsx(RunList, { ctx, scope, setScope, onOpen: setOpen }) });
}

function Chip({ ctx }) {
  const runs = useRuns(ctx, null);
  if (!runs.data) return null;
  const live = runs.data.runs.filter(r => r.status !== 'finished').length;
  if (!live) return null;
  return jsx(Tip, { label: 'Coding-agent runs in progress. Click to show them.', children: jsxs('button', {
    type: 'button',
    onClick: openPane,
    className: 'inline-flex h-full items-center gap-1.5 px-1.5 text-[0.6875rem]',
    style: muted,
    children: [jsx(StatusDot, { tone: 'good' }), `Agent runs · ${live}`],
  }) });
}

export default {
  id: ID,
  name: 'Agent Runs',
  description: 'See the coding-agent runs on this computer and what each one is doing.',
  register(ctx) {
    ctx.registerMany([
      { id: PANE, area: 'panes', title: 'Agent Runs', data: { placement: 'right' }, render: () => jsx(Pane, { ctx }) },
      { id: 'chip', area: 'statusBar.right', order: 70, render: () => jsx(Chip, { ctx }) },
      { id: 'show', area: PALETTE_AREA, data: { id: `${ID}.show`, label: 'Show agent runs',
        keywords: ['agent', 'runs', 'claude', 'coding', 'background'], run: openPane } },
    ]);
  },
};
