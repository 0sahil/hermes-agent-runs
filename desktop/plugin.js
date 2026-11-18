// Agent Runs for Hermes Desktop: the coding-agent runs on this computer, live and read-only.
//
// A pane in the right sidebar lists the runs the backend (dashboard/plugin_api.py) finds, newest
// activity first, and opens one to follow its log. A status-bar chip counts the live runs and a
// ⌘K command opens the pane. Hermes chat titles come from the gateway's own `session.list`, so the
// backend never opens Hermes' database. The plugin only reads: it never stops or talks to a run.
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
const TITLES_MS = 60000;

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

function runtime(run) {
  if (typeof run.started_at !== 'number') return '';
  const end = run.status === 'finished' && typeof run.last_activity_at === 'number' ? run.last_activity_at : Date.now();
  return duration(end - run.started_at);
}

function originLabel(origin, titles) {
  if (origin.kind === 'hermes_chat' && origin.hermes_session_id && titles[origin.hermes_session_id]) {
    return titles[origin.hermes_session_id];
  }
  return origin.label || 'Other';
}

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

// Hermes chat id → title, from the gateway's session list; kept fresh by its `session.title` events.
function useChatTitles(ctx, wanted) {
  const qc = useQueryClient();
  const titles = useQuery({
    queryKey: [ID, 'titles'],
    queryFn: async () => {
      const res = await host.request('session.list', { limit: 500, include_hidden: true });
      const map = {};
      for (const s of res && Array.isArray(res.sessions) ? res.sessions : []) {
        if (!s || typeof s.id !== 'string' || typeof s.title !== 'string' || !s.title.trim()) continue;
        map[s.id] = s.title.trim();
        const root = s._lineage_root_id || s.lineage_root_id; // a compressed chat keeps its first id
        if (typeof root === 'string' && root) map[root] = s.title.trim();
      }
      return map;
    },
    staleTime: TITLES_MS,
    refetchInterval: TITLES_MS,
    retry: 1,
  });
  useEffect(() => ctx.onEvent('session.title', event => {
    const p = event && event.payload;
    if (p && typeof p.session_id === 'string' && typeof p.title === 'string' && p.title.trim()) {
      qc.setQueryData([ID, 'titles'], old => ({ ...(old || {}), [p.session_id]: p.title.trim() }));
    }
  }), []);
  // A chat we have not seen yet (just started): look its title up now rather than in a minute.
  const map = titles.data || {};
  const missing = wanted.some(id => !(id in map));
  useEffect(() => {
    if (missing && !titles.isFetching && Date.now() - titles.dataUpdatedAt > 10000) titles.refetch();
  }, [missing, titles.isFetching, titles.dataUpdatedAt]);
  return map;
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
  const wanted = data ? data.runs.map(r => r.origin.hermes_session_id).filter(Boolean) : [];
  const titles = useChatTitles(ctx, wanted);

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
    (data.warnings.length > 0 || data.hidden_embedded > 0) && jsxs('div', { className: 'space-y-0.5 px-3 py-2 text-[0.6875rem]',
      style: { ...faint, borderTop: '1px solid var(--ui-stroke-secondary)' }, children: [
        data.hidden_embedded > 0 && jsx('div', { children: `${data.hidden_embedded} Hermes model call${data.hidden_embedded === 1 ? '' : 's'} hidden (Settings → Plugins → Agent Runs).` }),
        ...data.warnings.slice(0, 5).map((w, i) => jsx('div', { key: i, className: 'truncate', title: w, children: w })),
      ] }),
  ] });
}

const KIND = {
  prompt: { tag: 'prompt', style: { color: 'var(--ui-accent)' } },
  text: { tag: 'says', style: { color: 'var(--ui-text-primary)' } },
  thinking: { tag: 'thinks', style: { color: 'var(--ui-text-quaternary)', fontStyle: 'italic' } },
  tool: { tag: 'tool', style: { color: 'var(--ui-text-secondary)' } },
  result: { tag: 'result', style: { color: 'var(--ui-text-quaternary)' } },
  error: { tag: 'error', className: 'text-destructive' },
  system: { tag: 'system', style: { color: 'var(--ui-text-tertiary)' } },
};

function EventLine({ ev }) {
  const kind = KIND[ev.kind] || KIND.system;
  let head = ev.title || '';
  let body = ev.detail || null;
  if (ev.kind === 'prompt' || ev.kind === 'result' || ev.kind === 'error' && ev.title === 'Error') {
    head = ev.detail || ev.title || '';
    body = null;
  } else if (ev.kind === 'text') {
    head = ev.detail || ev.title || '';
    body = null;
  } else if (ev.kind === 'tool') {
    head = `${ev.title}${ev.detail ? `  ${ev.detail}` : ''}`;
    body = null;
  }
  return jsxs('div', { className: 'flex gap-2 py-0.5', children: [
    jsx('span', { className: 'shrink-0 tabular-nums', style: faint, children: clock(ev.ts) }),
    jsx('span', { className: `w-12 shrink-0 ${kind.className || ''}`, style: kind.style, children: kind.tag }),
    jsxs('span', { className: `min-w-0 flex-1 ${kind.className || ''}`, style: kind.style, children: [
      head,
      body && jsx('span', { style: faint, children: `\n${body}` }),
    ] }),
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
  return jsx('div', { ref: box, onScroll, className: 'min-h-0 flex-1 overflow-y-auto px-2 py-2', children:
    jsx(LogView, { className: 'overflow-visible', children: events.map(ev => jsx(EventLine, { key: ev.key, ev })) }) });
}

function Detail({ ctx, scope, runId, onBack }) {
  const runs = useRuns(ctx, scope);
  const run = runs.data ? runs.data.runs.find(r => r.id === runId) : null;
  const titles = useChatTitles(ctx, run && run.origin.hermes_session_id ? [run.origin.hermes_session_id] : []);
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
