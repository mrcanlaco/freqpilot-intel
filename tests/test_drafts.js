"use strict";
// Synthetic fixtures only. No browser, network, credentials or production storage.
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {webcrypto} = require("node:crypto");
if (!globalThis.crypto) globalThis.crypto = webcrypto;
require("../src/bti/static/workspace.js");
require("../src/bti/static/images.js");
require("../src/bti/static/drafts.js");
const {KEY, createStore, newDraft, validateBackup, encodeBackup, active, renderText, checkDraft, assertImageSources, makePostPackage} = BTIDrafts;
const stamp = "2026-10-20T00:00:00.000Z";
const source = () => BTIWorkspace.snapshot({
  post_id: "12345678", title: "Synthetic risk evidence", author: "Synthetic author",
  url: "https://www.binance.com/en/square/post/12345678", version: 3,
  checksum: "a".repeat(64), published_at: stamp, fetched_at: stamp,
  excerpt: "Synthetic risk observation, not a verified outcome.",
  provenance_type: "archive_snapshot",
  citation: {source_url: "https://web.archive.org/web/20261020000000/https://www.binance.com/en/square/post/12345678"}
});
const draft = () => newDraft({title: "Synthetic private title", body: "My editable interpretation.", notes: "PRIVATE-NOTES-DO-NOT-EXPORT", sources: [source()]});
const backup = () => { const item = draft(); return {version: 2, active_id: item.id, drafts: [item]}; };
function storage(initial = null) {
  let value = initial, writes = 0;
  return {
    getItem(key) { assert.equal(key, KEY); return value; },
    setItem(key, next) { assert.equal(key, KEY); value = next; writes++; },
    raw: () => value, writes: () => writes
  };
}

test("save and reload preserve editable wording, private notes and exact source references without writes on load", () => {
  const disk = storage();
  const store = createStore(disk);
  assert.equal(disk.writes(), 0);
  assert.deepEqual(store.get(), {version: 2, active_id: null, drafts: []});
  const item = draft();
  store.commit(state => { state.drafts.push(item); state.active_id = item.id; });
  const reloaded = createStore(disk);
  assert.equal(disk.writes(), 1);
  assert.deepEqual(active(reloaded.get()), item);
  assert.equal(active(reloaded.get()).sources[0].source_url, source().source_url);
  assert.deepEqual(validateBackup(JSON.parse(encodeBackup(reloaded.get()))), reloaded.get());
  const detached = reloaded.get(); detached.drafts[0].notes = "Caller mutation";
  assert.equal(active(reloaded.get()).notes, item.notes);
  item.body = "Outside mutation";
  assert.equal(active(store.get()).body, "My editable interpretation.");
});

test("quota and validation failures preserve saved bytes and acknowledged memory", () => {
  const initial = backup(), disk = storage(JSON.stringify(initial)), store = createStore(disk);
  const before = disk.raw();
  disk.setItem = () => { throw new Error("QuotaExceededError"); };
  assert.throws(() => store.commit(state => { state.drafts[0].body = "Unsaved replacement"; }), /Không thể lưu/);
  assert.equal(disk.raw(), before);
  assert.deepEqual(store.get(), initial);
  assert.throws(() => store.commit(state => { state.drafts[0].content_type = "1"; }));
  assert.equal(disk.raw(), before);
  assert.deepEqual(store.get(), initial);
  assert.throws(() => store.commit(state => { state.drafts.length = 0; throw new Error("Stop mutation"); }), /Stop mutation/);
  assert.deepEqual(store.get(), initial);
});

test("stale tabs cannot overwrite another tab's saved work", () => {
  const disk = storage(JSON.stringify(backup()));
  const first = createStore(disk), stale = createStore(disk), old = stale.get();
  first.commit(state => { state.drafts[0].body = "New work in first tab"; });
  const latest = disk.raw();
  assert.throws(() => stale.commit(state => { state.drafts[0].body = "Stale overwrite"; }), /tab khác/);
  assert.equal(disk.raw(), latest);
  assert.deepEqual(stale.get(), old);
  assert.equal(active(createStore(disk).get()).body, "New work in first tab");
});

test("corrupt, empty and unknown persisted schemas are never reset or written on load", () => {
  for (const raw of ["", "{broken", "null", JSON.stringify({...backup(), version: 99}), JSON.stringify({...backup(), future_field: true})]) {
    const disk = storage(raw);
    assert.throws(() => createStore(disk));
    assert.equal(disk.raw(), raw);
    assert.equal(disk.writes(), 0);
  }
  assert.throws(() => createStore({getItem() { throw new Error("SecurityError"); }}), /Không thể đọc/);
});

test("imports merge fresh IDs and select incoming work without overwriting existing drafts", () => {
  const initial = backup(), disk = storage(JSON.stringify(initial)), store = createStore(disk);
  const incoming = structuredClone(initial);
  incoming.drafts[0].body = "Imported wording";
  store.importBackup(incoming);
  const merged = store.get();
  assert.equal(merged.drafts.length, 2);
  assert.deepEqual(merged.drafts[0], initial.drafts[0]);
  assert.notEqual(merged.drafts[1].id, incoming.drafts[0].id);
  assert.equal(active(merged).body, "Imported wording");
  assert.equal(active(merged).notes, incoming.drafts[0].notes);
  const before = disk.raw(), bad = backup();
  bad.drafts[0].sources[0].source_url = "javascript:alert(1)";
  assert.throws(() => store.importBackup(bad));
  assert.equal(disk.raw(), before);
  assert.deepEqual(store.get(), merged);
});

test("unsafe source URLs and invalid source identities use the workspace validation boundary", () => {
  for (const field of ["url", "source_url"]) {
    for (const url of ["javascript:alert(1)", "http://www.binance.com/en/square/post/1", "https://www.binance.com.evil.example/", "https://user:secret@www.binance.com/en/square/post/1"]) {
      const raw = backup(); raw.drafts[0].sources[0][field] = url;
      assert.throws(() => validateBackup(raw));
    }
  }
  for (const [field, value] of [["post_id", "../secret"], ["evidence_id", -1], ["version", 0], ["checksum", "fake"], ["fetched_at", "not-a-date"]]) {
    const raw = backup(); raw.drafts[0].sources[0][field] = value;
    assert.throws(() => validateBackup(raw));
  }
});

test("strict draft schema rejects wrong enums, IDs, times, lengths and unknown fields", () => {
  const changes = [
    raw => { raw.version = "1"; },
    raw => { raw.active_id = "missing"; },
    raw => { raw.drafts.push(structuredClone(raw.drafts[0])); },
    raw => { raw.drafts[0].id = "../unsafe"; },
    raw => { raw.drafts[0].content_type = 3; },
    raw => { raw.drafts[0].created_at = "2026-02-30T00:00:00.000Z"; },
    raw => { raw.drafts[0].updated_at = "2020-01-01T00:00:00.000Z"; },
    raw => { raw.drafts[0].title = "x".repeat(121); },
    raw => { raw.drafts[0].body = "x".repeat(15001); },
    raw => { raw.drafts[0].notes = "x".repeat(4001); },
    raw => { delete raw.drafts[0].notes; },
    raw => { raw.drafts[0].future_field = true; },
    raw => { raw.drafts[0].sources[0].private_note = "Must not silently drop unknown data"; },
    raw => { raw.drafts[0].sources = Array.from({length: 21}, source); },
    raw => { raw.drafts = Array.from({length: 101}, draft); raw.active_id = null; }
  ];
  for (const change of changes) { const raw = backup(); change(raw); assert.throws(() => validateBackup(raw)); }
  const blank = newDraft();
  assert.equal(blank.title, ""); assert.equal(blank.body, "");
  assert.deepEqual(blank.sources, []);
});

test("aggregate UTF-8 storage limit rejects commits and imports atomically", () => {
  const disk = storage(JSON.stringify(backup())), store = createStore(disk), before = disk.raw();
  const oversized = {version: 2, active_id: null, drafts: Array.from({length: 80}, () => newDraft({body: "é".repeat(15000)}))};
  assert.throws(() => validateBackup(oversized), /2 MiB/);
  assert.throws(() => encodeBackup(oversized), /2 MiB/);
  assert.throws(() => store.importBackup(oversized), /2 MiB/);
  assert.throws(() => store.commit(state => { state.drafts = oversized.drafts; state.active_id = null; }), /2 MiB/);
  assert.equal(disk.raw(), before);
  assert.equal(store.get().drafts.length, 1);
});

test("rendered posts preserve wording and complete citations and disclaimer, never private notes", () => {
  const item = draft(); item.body = "  Unmodified wording\nwith a second line.  ";
  const output = renderText(item), ref = item.sources[0];
  assert.ok(output.includes(item.body));
  for (const value of [ref.author, ref.title, ref.url, ref.source_url, ref.published_at, `Phiên bản: ${ref.version}`]) assert.ok(output.includes(value));
  assert.match(output, /không phải khuyến nghị đầu tư hay tín hiệu giao dịch/);
  assert.match(output, /không chứng minh lợi nhuận thực tế/);
  assert.match(output, /chưa xác minh độc lập/);
  assert.ok(!output.includes(item.notes));
});

test("long source headings keep an abbreviated citation and the complete private reference", () => {
  const item = draft();
  const heading = Array.from({length: 23}, (_, i) => `synthetic${i + 1}`).join(" ");
  item.sources[0].title = heading;
  const text = renderText(item);
  assert.match(text, /synthetic16…/);
  assert.ok(!text.includes("synthetic17"));
  assert.ok(text.includes(item.sources[0].url));
  assert.equal(item.sources[0].title, heading);
});

test("export readiness counts the full rendered post including title, citations and disclaimer", () => {
  const short = draft();
  assert.equal(checkDraft(short).ok, true);
  const overhead = renderText({...short, body: ""}).length;
  short.body = "x".repeat(2200 - overhead);
  assert.deepEqual(checkDraft(short), {ok: true, issues: [], characters: 2200, limit: 2200});
  short.body += "x";
  assert.equal(checkDraft(short).characters, 2201);
  assert.equal(checkDraft(short).ok, false);
  assert.equal(renderText(short).length, 2201);
  const long = draft(); long.content_type = 2;
  long.body = "x".repeat(15000 - renderText({...long, body: ""}).length);
  assert.equal(checkDraft(long).ok, true);
  assert.equal(checkDraft(long).characters, 15000);
  long.body += "x";
  assert.equal(checkDraft(long).ok, false);
  long.title = ""; long.body = "Article body";
  assert.ok(checkDraft(long).issues.some(issue => /tiêu đề/.test(issue)));
  const empty = newDraft();
  assert.equal(checkDraft(empty).ok, false);
  assert.ok(checkDraft(empty).issues.some(issue => /nội dung/.test(issue)));
  assert.ok(checkDraft(empty).issues.some(issue => /nguồn/.test(issue)));
});

test("legacy text drafts migrate on read without overwriting saved bytes or private work", () => {
  const original = backup(); original.version = 1;
  delete original.drafts[0].images;
  const bytes = JSON.stringify(original), disk = storage(bytes), store = createStore(disk);
  assert.equal(disk.raw(), bytes); assert.equal(disk.writes(), 0);
  assert.deepEqual(active(store.get()), {...original.drafts[0], images: []});
  store.commit(state => { state.drafts[0].body = "Edited after migration"; });
  const saved = JSON.parse(disk.raw());
  assert.equal(saved.version, 2);
  assert.equal(saved.drafts[0].notes, original.drafts[0].notes);
  assert.deepEqual(saved.drafts[0].sources, original.drafts[0].sources);
  assert.equal(saved.drafts[0].body, "Edited after migration");
});

const imageURL = "https://public.bnbstatic.com/image/pgc/synthetic/chart.png";
test("selected image links stay bound to cited sources and roundtrip through private backup", () => {
  const item = draft(); item.images = [{post_id: item.sources[0].post_id, url: imageURL}];
  const state = {version: 2, active_id: item.id, drafts: [item]};
  const disk = storage(), store = createStore(disk);
  store.importBackup(JSON.parse(encodeBackup(state)));
  assert.deepEqual(active(createStore(disk).get()).images, item.images);
  assert.equal(active(store.get()).notes, item.notes);
  for (const bad of [
    [{post_id: "uncited", url: imageURL}],
    [{post_id: item.sources[0].post_id, url: "https://evil.example/chart.png"}],
    [...item.images, ...item.images],
    [{...item.images[0], private_note: "Unsupported"}],
    Array.from({length: 5}, (_, i) => ({post_id: item.sources[0].post_id, url: imageURL.replace("chart", `chart-${i}`)}))
  ]) assert.throws(() => encodeBackup({...state, drafts: [{...item, images: bad}]}));
});

test("image exports require fresh URL membership and exact source revision, never private notes", () => {
  const item = draft(), ref = item.sources[0];
  item.images = [{post_id: ref.post_id, url: imageURL}];
  const article = {post_id: ref.post_id, version: ref.version, checksum: ref.checksum, images: [imageURL]};
  assertImageSources(item, new Map([[ref.post_id, article]]));
  const output = makePostPackage(item);
  assert.equal(output.schema, "bti-square-post-v1");
  assert.equal(output.text, renderText(item));
  assert.deepEqual(output.images, [{post_id: ref.post_id, url: imageURL, author: ref.author,
    content_url: ref.url, source_url: ref.source_url, version: ref.version, source_checksum: ref.checksum,
    fetched_at: ref.fetched_at, observed_at: ref.observed_at}]);
  assert.ok(!JSON.stringify(output).includes(item.notes));
  assert.ok(!renderText(item).includes(imageURL), "Text-only copy cannot pretend to embed an image");
  for (const fresh of [null, {...article, images: []}, {...article, images: ["https://public.bnbstatic.com/image/pgc/synthetic/replaced.png"]},
    {...article, version: 4}, {...article, checksum: "b".repeat(64)}, {...article, post_id: "another-post"}]) {
    assert.throws(() => assertImageSources(item, new Map(fresh ? [[ref.post_id, fresh]] : [])));
  }
});
