// Render-loop guards in desktop/plugin.js, run with `node --test tests/js/*.test.mjs`.
//
// Each guard decides whether a view writes state, the cache or the scroll position; a write that
// changes nothing must not happen, or two mounted views (the pane and the large page) can keep
// waking each other. Like the other tests, this loads the plugin's pure blocks on their own: the
// plugin imports the Hermes SDK, which only exists inside the app.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('../../desktop/plugin.js', import.meta.url), 'utf8');
const block = (open, close) => {
  const start = source.indexOf(open);
  const end = source.indexOf(close);
  assert.ok(start >= 0 && end > start, `${open} block markers are in plugin.js`);
  return source.slice(start, end);
};
const titlesBlock = block('// --- chat titles >>>', '// <<< chat titles');
const layoutBlock = block('// --- layout >>>', '// <<< layout');

const names = ['withTitle', 'titleRetryDue', 'nextSize', 'UNMEASURED', 'WIDTH_STEP_PX', 'COMPACT_BELOW', 'scrollTarget', 'eventsKey'];
const mod = await import(`data:text/javascript;charset=utf-8,${encodeURIComponent(
  `${titlesBlock}\n${layoutBlock}\nexport { ${names.join(', ')} };`)}`);
const { withTitle, titleRetryDue, nextSize, UNMEASURED, WIDTH_STEP_PX, COMPACT_BELOW, scrollTarget, eventsKey } = mod;

// --- view size --------------------------------------------------------------------------------

test('a size that renders the same is the same object, so React skips the update', () => {
  const first = nextSize(UNMEASURED, 300);
  assert.deepEqual(first, { compact: true, width: 288 });
  assert.equal(nextSize(first, 300), first);
  assert.equal(nextSize(first, 303.5), first, 'within one width step');
  assert.equal(nextSize(first, 0), first, 'a collapsed zone keeps the last size');
  assert.equal(nextSize(first, Number.NaN), first);
  assert.equal(nextSize(first, undefined), first);
  assert.equal(nextSize(UNMEASURED, 0), UNMEASURED, 'unmeasured stays unmeasured');
  assert.notEqual(nextSize(first, 320), first, 'the next step is a new size');
});

test('the breakpoint uses the exact width, even inside one step', () => {
  // 416 and 419 share a step with 420's neighbour below; 420 is wide.
  const narrow = nextSize(UNMEASURED, COMPACT_BELOW - 1);
  const wide = nextSize(narrow, COMPACT_BELOW);
  assert.equal(narrow.compact, true);
  assert.equal(wide.compact, false);
  assert.equal(nextSize(wide, COMPACT_BELOW + 1), wide);
});

test('a resize loop between two views settles instead of re-rendering forever', () => {
  // A stand-in for ResizeObserver -> setState -> layout -> ResizeObserver: each render nudges the
  // measured width by a pixel either way (a scrollbar, a rounding change). React re-renders only
  // when the state object changes; count how many of 1,000 notifications cost a render.
  const views = [{ size: UNMEASURED, base: 300, renders: 0 }, { size: UNMEASURED, base: 1100, renders: 0 }];
  for (let i = 0; i < 1000; i += 1) {
    for (const v of views) {
      const next = nextSize(v.size, v.base + (i % 2 ? 0.6 : -0.6));
      if (next !== v.size) {
        v.size = next;
        v.renders += 1;
      }
    }
  }
  for (const v of views) assert.ok(v.renders <= 2, `settled after ${v.renders} renders`);
});

test('a width oscillating across a step boundary costs one render per real change, never more', () => {
  let size = UNMEASURED;
  let renders = 0;
  const widths = [];
  for (let i = 0; i < 100; i += 1) widths.push(i % 2 ? 10 * WIDTH_STEP_PX : 10 * WIDTH_STEP_PX - 1);
  for (const w of widths) {
    const next = nextSize(size, w);
    if (next !== size) renders += 1;
    size = next;
  }
  assert.equal(renders, widths.length, 'each crossing is a real change, and nothing more');
  // Repeating the same width afterwards costs nothing.
  for (let i = 0; i < 100; i += 1) assert.equal(nextSize(size, widths[widths.length - 1]), size);
});

// --- following the log ------------------------------------------------------------------------

test('the log only scrolls when it is not at the bottom already', () => {
  assert.equal(scrollTarget({ scrollTop: 600, scrollHeight: 1000, clientHeight: 400 }), null);
  assert.equal(scrollTarget({ scrollTop: 599.5, scrollHeight: 1000, clientHeight: 400 }), null, 'sub-pixel is at the bottom');
  assert.equal(scrollTarget({ scrollTop: 100, scrollHeight: 1000, clientHeight: 400 }), 600);
  assert.equal(scrollTarget({ scrollTop: 0, scrollHeight: 300, clientHeight: 400 }), null, 'content shorter than the box');
});

test('following settles: writing the target once leaves nothing more to write', () => {
  const el = { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 };
  let writes = 0;
  for (let i = 0; i < 50; i += 1) {
    const target = scrollTarget(el);
    if (target !== null) {
      el.scrollTop = target;
      writes += 1;
    }
  }
  assert.equal(writes, 1);
});

// --- the run's log: one cache entry per view ---------------------------------------------------

test('the pane and the large page never share a cache entry for the same run', () => {
  const pane = eventsKey('agent-runs', 'claude:r1', 'pane');
  const page = eventsKey('agent-runs', 'claude:r1', 'page');
  assert.notDeepEqual(pane, page);
  assert.deepEqual(eventsKey('agent-runs', 'claude:r1', undefined), pane, 'the pane is the default');
  assert.notDeepEqual(eventsKey('agent-runs', 'claude:r2', 'pane'), pane);
  // Neither key is a prefix of the other, so removing one view's log never matches the other's.
  assert.notDeepEqual(pane.slice(0, page.length), page);
});

// --- chat titles ------------------------------------------------------------------------------

test('a pushed title that the cache already has is not written again', () => {
  const old = { titles: { a: 'Alpha' }, report: [{ source: 's', ok: true }] };
  assert.equal(withTitle(old, 'a', 'Alpha'), undefined, 'undefined makes setQueryData skip the write');
  const next = withTitle(old, 'a', 'Renamed');
  assert.deepEqual(next, { titles: { a: 'Renamed' }, report: old.report });
  assert.deepEqual(old.titles, { a: 'Alpha' }, 'the old cache value is not mutated');
  assert.deepEqual(withTitle(undefined, 'b', 'Beta'), { report: [], titles: { b: 'Beta' } });
});

test('three mounted views hearing one title event write the cache once', () => {
  let cache = { titles: {}, report: [] };
  let writes = 0;
  const setQueryData = updater => {
    const next = updater(cache);
    if (next !== undefined) {
      cache = next;
      writes += 1;
    }
  };
  for (let view = 0; view < 3; view += 1) setQueryData(old => withTitle(old, 'chat', 'A title'));
  assert.equal(writes, 1);
  for (let view = 0; view < 3; view += 1) setQueryData(old => withTitle(old, 'chat', 'A title'));
  assert.equal(writes, 1, 'the same event again writes nothing');
});

test('views asking again for a missing title ask at most once a minute between them', () => {
  const every = 60000;
  const t0 = 10 * every;
  let lastTry = 0;
  let asks = 0;
  const state = { missing: 1, fetching: false, updatedAt: t0 - 2 * every };
  // Three views (list, pane run, page) re-run their effect on every render for two minutes.
  for (let now = t0; now < t0 + 2 * every; now += 250) {
    for (let view = 0; view < 3; view += 1) {
      if (titleRetryDue({ ...state, lastTry, now, every })) {
        lastTry = now;
        asks += 1;
      }
    }
  }
  assert.equal(asks, 2, 'once at the start, once a minute later');
});

test('no title retry while nothing is missing, while fetching, or right after an answer', () => {
  const base = { missing: 1, fetching: false, updatedAt: 0, lastTry: 0, now: 10 * 60000, every: 60000 };
  assert.equal(titleRetryDue(base), true);
  assert.equal(titleRetryDue({ ...base, missing: 0 }), false);
  assert.equal(titleRetryDue({ ...base, fetching: true }), false);
  assert.equal(titleRetryDue({ ...base, updatedAt: base.now - 1000 }), false);
  assert.equal(titleRetryDue({ ...base, lastTry: base.now - 1000 }), false);
});
