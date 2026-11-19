// Layout helpers in desktop/plugin.js, run with `node --test tests/js/*.test.mjs`: the width
// breakpoint, the large view's route path, event ids for the open rows of the compact log, and the
// one-line text.
//
// Like titles.test.mjs, this loads the marked block on its own: plugin.js imports the Hermes SDK,
// which only exists inside the app.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('../../desktop/plugin.js', import.meta.url), 'utf8');
const start = source.indexOf('// --- layout >>>');
const end = source.indexOf('// <<< layout');
assert.ok(start >= 0 && end > start, 'the layout block markers are in plugin.js');
const block = source.slice(start, end);
assert.ok(!/\bimport\b|\bhost\.|\bwindow\b/.test(block), 'the layout block stays free of imports, host and window');

const names = ['COMPACT_BELOW', 'isCompact', 'ROUTE_PATH', 'runPath', 'parseRunHash', 'eventText', 'eventSummary', 'eventId', 'toggled'];
const mod = await import(`data:text/javascript;charset=utf-8,${encodeURIComponent(`${block}\nexport { ${names.join(', ')} };`)}`);
const { COMPACT_BELOW, isCompact, ROUTE_PATH, runPath, parseRunHash, eventText, eventSummary, eventId, toggled } = mod;

test('compact below 420 px, full layout from 420 px', () => {
  assert.equal(COMPACT_BELOW, 420);
  assert.equal(isCompact(260), true);
  assert.equal(isCompact(300), true);
  assert.equal(isCompact(419.5), true);
  assert.equal(isCompact(420), false);
  assert.equal(isCompact(1200), false);
});

test('an unmeasured or hidden view (0 or no width) is never compact', () => {
  assert.equal(isCompact(0), false);
  assert.equal(isCompact(undefined), false);
  assert.equal(isCompact(Number.NaN), false);
});

test('the run page path is one segment with the id in the query', () => {
  assert.equal(ROUTE_PATH, '/agent-runs');
  assert.equal(runPath('claude-1234-5678'), '/agent-runs?run=claude-1234-5678');
  assert.ok(!runPath('a/b').slice(1).includes('/'), 'the id never adds a path segment');
});

test('run ids survive the round trip through the hash, however odd', () => {
  for (const id of ['claude-1234-5678', 'claude:pid:42', 'a b&c=d?e#f/g', 'ünï-código', '%41']) {
    assert.deepEqual(parseRunHash(`#${runPath(id)}`), { onRoute: true, runId: id });
    assert.deepEqual(parseRunHash(runPath(id)), { onRoute: true, runId: id }, 'without the leading #');
  }
});

test('the run page without a usable id is on the route with no run', () => {
  assert.deepEqual(parseRunHash('#/agent-runs'), { onRoute: true, runId: null });
  assert.deepEqual(parseRunHash('#/agent-runs?run='), { onRoute: true, runId: null });
  assert.deepEqual(parseRunHash('#/agent-runs?run=%20'), { onRoute: true, runId: null });
  assert.deepEqual(parseRunHash('#/agent-runs?other=1'), { onRoute: true, runId: null });
  assert.deepEqual(parseRunHash('#/agent-runs?x=1&run=r1'), { onRoute: true, runId: 'r1' });
});

test('other routes are not the run page', () => {
  for (const hash of ['', '#', '#/', '#/local-abc', '#/agent-runs-x?run=r1', '#/agent-runs/run/r1', '#/settings?run=r1', null, undefined]) {
    assert.deepEqual(parseRunHash(hash), { onRoute: false, runId: null }, String(hash));
  }
});

const tool = { ts: 1700000000000, kind: 'tool', title: 'Bash', detail: 'npm test\n  -- --watch' };

test('event ids are stable for the same event and differ when anything differs', () => {
  assert.equal(eventId(tool), eventId({ ...tool }), 'a copy from a later poll has the same id');
  assert.equal(eventId(tool), eventId({ ...tool, key: 99 }), 'the pane-side sequence number is not part of the id');
  assert.notEqual(eventId(tool), eventId({ ...tool, ts: tool.ts + 1 }));
  assert.notEqual(eventId(tool), eventId({ ...tool, kind: 'result' }));
  assert.notEqual(eventId(tool), eventId({ ...tool, title: 'Read' }));
  assert.notEqual(eventId(tool), eventId({ ...tool, detail: 'npm test' }));
  assert.notEqual(eventId({ ...tool, title: 'ab', detail: 'c' }), eventId({ ...tool, title: 'a', detail: 'bc' }), 'fields do not run together');
  assert.match(eventId(tool), /^tool:1700000000000:[0-9a-z]+$/);
  assert.equal(typeof eventId({ kind: 'system' }), 'string', 'missing fields are fine');
});

test('open-row state toggles without mutating the old set', () => {
  const before = new Set(['a']);
  const opened = toggled(before, 'b');
  assert.deepEqual([...opened].sort(), ['a', 'b']);
  assert.deepEqual([...before], ['a']);
  assert.deepEqual([...toggled(opened, 'a')], ['b']);
});

test('the compact line is the head plus a tool command, on one line', () => {
  assert.equal(eventSummary(tool), 'Bash npm test -- --watch');
  assert.equal(eventSummary({ kind: 'prompt', title: 'Prompt', detail: 'Fix the\nbuild' }), 'Fix the build');
  assert.equal(eventSummary({ kind: 'result', title: 'Result', detail: '' }), 'Result');
  assert.equal(eventSummary({ kind: 'system', title: 'Compacted', detail: 'kept 12 messages' }), 'Compacted');
  assert.equal(eventSummary({ kind: 'error', title: 'Error', detail: '  boom  ' }), 'boom');
});

test('the open row text keeps what the wide layout shows', () => {
  assert.deepEqual(eventText(tool), { head: 'Bash', body: 'npm test\n  -- --watch', inline: true });
  assert.deepEqual(eventText({ kind: 'system', title: 'Compacted', detail: 'kept 12 messages' }), { head: 'Compacted', body: 'kept 12 messages' });
  assert.deepEqual(eventText({ kind: 'text', title: 'Says', detail: 'Done.' }), { head: 'Done.', body: '' });
});
