// Layout helpers in desktop/plugin.js, run with `node --test tests/js/*.test.mjs`: the width
// breakpoint, the large view's route path, event ids for the open rows of the compact log, the
// one-line text, and the middle cut that fits chat titles on a run row.
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

const names = ['COMPACT_BELOW', 'isCompact', 'ROUTE_PATH', 'runPath', 'parseRunHash', 'eventText', 'eventSummary', 'eventId', 'toggled',
  'middleTruncate', 'titleChars', 'afterTitle', 'SEP'];
const mod = await import(`data:text/javascript;charset=utf-8,${encodeURIComponent(`${block}\nexport { ${names.join(', ')} };`)}`);
const { COMPACT_BELOW, isCompact, ROUTE_PATH, runPath, parseRunHash, eventText, eventSummary, eventId, toggled,
  middleTruncate, titleChars, afterTitle, SEP } = mod;

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

// --- the middle cut for chat titles ------------------------------------------------------------

const codePoints = s => Array.from(s).length;
const loneSurrogate = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/;
const branch = 'View output of background claude task #2';

test('a title that fits is returned unchanged', () => {
  assert.equal(middleTruncate('Fix the build', 40), 'Fix the build');
  assert.equal(middleTruncate('', 5), '');
  assert.equal(middleTruncate(branch, Infinity), branch, 'no limit (an unmeasured pane)');
  assert.equal(middleTruncate(branch, undefined), branch, 'no number, no cut');
});

test('a title of exactly the limit is not cut; one character more is', () => {
  assert.equal(middleTruncate('abcdefghij', 10), 'abcdefghij');
  const cut = middleTruncate('abcdefghijk', 10);
  assert.equal(codePoints(cut), 10);
  assert.equal(cut, 'abcde…hijk', 'short limits split the room about evenly');
});

test('the cut keeps the start and the last eight characters', () => {
  assert.equal(middleTruncate(branch, 23), 'View output of… task #2');
  const original = middleTruncate('View output of background claude task', 23);
  assert.notEqual(original, middleTruncate(branch, 23), 'a chat and its branch stay told apart');
  assert.ok(original.endsWith('ude task'), original);
  for (const max of [12, 20, 30, 40]) assert.ok(codePoints(middleTruncate(branch, max)) <= max, `max ${max}`);
});

test('the cut counts code points and never splits an emoji', () => {
  const title = '🚀 Deploy 🧪 tests for the 日本語 build now 🎉';
  for (let max = 1; max < codePoints(title); max += 1) {
    const cut = middleTruncate(title, max);
    assert.ok(codePoints(cut) <= max, `max ${max}`);
    assert.ok(!loneSurrogate.test(cut), `no half emoji at max ${max}: ${JSON.stringify(cut)}`);
  }
  assert.equal(middleTruncate('🎉🎉🎉🎉', 4), '🎉🎉🎉🎉', 'four code points fit in four (eight UTF-16 units)');
  assert.equal(middleTruncate('ab🎉cdefg🚀', 5), 'ab…g🚀');
  assert.equal(middleTruncate('🚀🧪🎉🧪🚀', 3), '🚀…🚀');
});

test('very small limits still give a short, valid string', () => {
  assert.equal(middleTruncate(branch, 0), '');
  assert.equal(middleTruncate(branch, -3), '');
  assert.equal(middleTruncate(branch, 1), '…');
  assert.equal(middleTruncate(branch, 2), 'V…');
  assert.equal(middleTruncate(branch, 3), 'V…2');
  assert.equal(middleTruncate(branch, 4.9), 'Vi…2', 'a fractional limit rounds down');
});

test('a row title gets the width left after its runtime and activity', () => {
  const tail = '11m · active 16 min ago';
  assert.equal(titleChars(0, tail), Infinity, 'unmeasured: no limit');
  assert.equal(titleChars(undefined, tail), Infinity);
  const narrow = titleChars(300, tail);
  const wide = titleChars(900, tail);
  assert.ok(narrow >= 12 && narrow < 30, `narrow ${narrow}`);
  assert.ok(wide > 80, `a wide pane shows a long title whole (${wide})`);
  assert.equal(middleTruncate(branch, wide), branch);
  assert.ok(titleChars(300, '') > narrow, 'a shorter tail leaves more room');
  assert.equal(titleChars(60, tail), 12, 'never below a dozen characters; past that the CSS clip takes over');
});

test('the text after a row title keeps its space before the separator', () => {
  // It is a flex item of its own, where CSS drops a leading plain space ("task· 4m").
  const tail = ['4m', 'active 1 sec ago'].join(SEP);
  assert.equal(afterTitle(tail), ' · 4m · active 1 sec ago');
  assert.ok(!afterTitle(tail).startsWith(' '), 'never a plain leading space');
  // Rendered as a whitespace-collapsing renderer would, title and tail read with a space each side.
  const shown = `View output of… task${afterTitle(tail).replace(/^ +/, '')}`.replace(/ /g, ' ');
  assert.equal(shown, 'View output of… task · 4m · active 1 sec ago');
  assert.equal(afterTitle(''), '', 'no facts, no separator');
  assert.equal(Array.from(afterTitle(tail)).length, Array.from(tail).length + 3, 'titleChars budgets 3 for the separator');
});
