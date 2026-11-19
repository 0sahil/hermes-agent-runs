// Chat-title matching in desktop/plugin.js, run with `node --test tests/js/*.test.mjs`.
//
// The plugin is one file that imports the Hermes SDK, which only exists inside the app. The title
// logic sits in a marked block with no imports, so this test loads that block on its own.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('../../desktop/plugin.js', import.meta.url), 'utf8');
const start = source.indexOf('// --- chat titles >>>');
const end = source.indexOf('// <<< chat titles');
assert.ok(start >= 0 && end > start, 'the chat titles block markers are in plugin.js');
const block = source.slice(start, end);
assert.ok(!/\bimport\b|\bhost\./.test(block), 'the chat titles block stays free of imports and host calls');

const names = ['listingRows', 'rowTitle', 'indexTitles', 'chatTitle', 'titleMissReason', 'titleFromEvent', 'chatIds', 'originLabel'];
const mod = await import(`data:text/javascript;charset=utf-8,${encodeURIComponent(`${block}\nexport { ${names.join(', ')} };`)}`);
const { listingRows, indexTitles, chatTitle, titleMissReason, titleFromEvent, chatIds, originLabel } = mod;

const CHAT = `local-${'ab12'.repeat(16)}`; // a desktop chat id: `local-` + 64 hex
const LATER = `local-${'cd34'.repeat(16)}`;

test('listingRows accepts both reply shapes and rejects others', () => {
  assert.deepEqual(listingRows({ sessions: [{ id: 'a' }] }), [{ id: 'a' }]);
  assert.deepEqual(listingRows([{ id: 'a' }]), [{ id: 'a' }]);
  assert.equal(listingRows({ result: { sessions: [] } }), null);
  assert.equal(listingRows(null), null);
});

test('classic gateway rows map their id and compression root', () => {
  const { titles } = indexTitles([{ source: 'session.list', reply: { sessions: [
    { id: 'tip-2', title: 'Refactor the parser', _lineage_root_id: 'root-1' },
    { id: 'plain', title: '  Spaced title  ', _lineage_root_id: null },
  ] } }]);
  assert.equal(titles['tip-2'], 'Refactor the parser');
  assert.equal(titles['root-1'], 'Refactor the parser');
  assert.equal(titles.plain, 'Spaced title');
});

test('session-authority rows map session_id, root id and resolved id', () => {
  const { titles } = indexTitles([{ source: 'session.list stored', reply: { scope: 'stored', sessions: [
    { session_id: LATER, id: CHAT, resolved_id: LATER, title: 'View output of a task', running: false },
  ] } }]);
  assert.equal(titles[CHAT], 'View output of a task');
  assert.equal(titles[LATER], 'View output of a task');
});

test('saved-sessions rows map every id on the lineage', () => {
  const { titles } = indexTitles([{ source: 'saved sessions', reply: { sessions: [
    { id: 'c', title: 'Long chat', _lineage_root_id: 'a', _lineage_ids: ['a', 'b', 'c'] },
  ] } }]);
  assert.deepEqual([titles.a, titles.b, titles.c], ['Long chat', 'Long chat', 'Long chat']);
});

test('a row of its own keeps its title over another row naming it as a segment', () => {
  const { titles } = indexTitles([{ source: 'x', reply: { sessions: [
    { id: 'parent', title: 'Parent chat' },
    { id: 'branch', title: 'Branch chat', _lineage_ids: ['parent', 'branch'] },
  ] } }]);
  assert.equal(titles.parent, 'Parent chat');
  assert.equal(titles.branch, 'Branch chat');
});

test('untitled rows are skipped and root_title is a fallback', () => {
  const { titles, report } = indexTitles([{ source: 'x', reply: { sessions: [
    { id: 'empty', title: '' }, { id: 'blank', title: '   ' }, { id: 'none' }, null,
    { id: 'rooted', title: '', root_title: 'From the root' },
  ] } }]);
  assert.equal('empty' in titles || 'blank' in titles || 'none' in titles, false);
  assert.equal(titles.rooted, 'From the root');
  assert.deepEqual(report, [{ source: 'x', ok: true, rows: 5, titled: 1 }]);
});

test('the reported case: one source refuses, another has the chat', () => {
  // On the session-authority gateway `limit: 500` was refused; on the classic one `scope` is.
  const { titles, report } = indexTitles([
    { source: 'session.list stored', error: 'invalid_params' },
    { source: 'session.list', reply: { sessions: [{ id: CHAT, title: 'View output of a task', source: 'local' }] } },
    { source: 'saved sessions', skipped: 'not offered by this Hermes version' },
  ]);
  assert.equal(chatTitle(titles, CHAT), 'View output of a task');
  assert.deepEqual(report.map(r => r.ok), [false, true, false]);
  assert.equal(originLabel({ kind: 'hermes_chat', hermes_session_id: CHAT, label: 'Hermes chat' }, titles), 'View output of a task');
});

test('a miss explains what every source did', () => {
  const { titles, report } = indexTitles([
    { source: 'session.list stored', error: 'invalid_params' },
    { source: 'session.list', reply: { sessions: [{ id: 'other', title: 'Other chat' }] } },
    { source: 'saved sessions', reply: { detail: 'Not Found' } },
  ]);
  assert.equal(chatTitle(titles, CHAT), undefined);
  const reason = titleMissReason(CHAT, report);
  assert.match(reason, /^No title for chat local-ab12ab12ab12ab…: /);
  assert.match(reason, /session\.list stored: invalid_params/);
  assert.match(reason, /session\.list: 1 chat, none with this id/);
  assert.match(reason, /saved sessions: reply has no session list/);
  assert.equal(originLabel({ kind: 'hermes_chat', hermes_session_id: CHAT, label: 'Hermes chat' }, titles), 'Hermes chat');
  assert.match(titleMissReason('x', undefined), /not looked up yet/);
});

test('chatTitle matches a unique abbreviation only', () => {
  const titles = { [CHAT]: 'One', [LATER]: 'Two' };
  assert.equal(chatTitle(titles, CHAT.slice(0, 22)), 'One');
  assert.equal(chatTitle(titles, 'local-'), undefined); // too short to be an abbreviation
  assert.equal(chatTitle({ 'local-aaaaaaaaaaaaaaaa1': 'A', 'local-aaaaaaaaaaaaaaaa2': 'B' }, 'local-aaaaaaaaaaaaaaaa'), undefined);
  assert.equal(chatTitle(titles, undefined), undefined);
  assert.equal(chatTitle({ constructor: 'x' }, 'toString'), undefined);
});

test('titleFromEvent reads the payload or the event session id', () => {
  assert.deepEqual(titleFromEvent({ payload: { session_id: 's1', title: ' New ' } }), ['s1', 'New']);
  assert.deepEqual(titleFromEvent({ session_id: 's2', payload: { title: 'Renamed' } }), ['s2', 'Renamed']);
  assert.equal(titleFromEvent({ payload: { session_id: 's3', title: '' } }), null);
  assert.equal(titleFromEvent({ payload: { title: 'No id' } }), null);
  assert.equal(titleFromEvent(null), null);
});

test('chatIds keeps Hermes chats only, once each', () => {
  const runs = [
    { origin: { kind: 'hermes_chat', hermes_session_id: CHAT } },
    { origin: { kind: 'hermes_chat', hermes_session_id: CHAT } },
    { origin: { kind: 'hermes_cron', hermes_session_id: 'cron_job' } },
    { origin: { kind: 'terminal' } },
    { origin: { kind: 'hermes_chat', hermes_session_id: '' } },
  ];
  assert.deepEqual(chatIds(runs), [CHAT]);
});

test('non-chat origins keep their own label', () => {
  assert.equal(originLabel({ kind: 'terminal', label: 'Terminal' }, { x: 'y' }), 'Terminal');
  assert.equal(originLabel({ kind: 'unknown' }, {}), 'Other');
});
