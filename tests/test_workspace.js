"use strict";
// Synthetic fixtures only. No browser, credentials, network or production storage.
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {webcrypto} = require("node:crypto");
if (!globalThis.crypto) globalThis.crypto = webcrypto;
require("../src/bti/static/workspace.js");
const {createStore, validateBackup, revalidate, makeDraft, snapshot, missingPlanFields, assertUnchanged} = BTIWorkspace;
const stamp = "2026-10-20T00:00:00.000Z";
const article = () => ({post_id: "12345678", title: "Synthetic risk evidence", author: "Synthetic author", url: "https://www.binance.com/en/square/post/12345678", version: 1, checksum: "a".repeat(64), published_at: stamp, fetched_at: stamp, excerpt: "Synthetic source states that risk must be bounded before entering a trade.", provenance_type: "archive_snapshot", evidence: [{id: 7, observed_at: stamp, verification_level: "secondary", limitations: "Synthetic archival observation."}]});
const collection = () => ({id: "synthetic-research", question: "How is risk bounded?", purpose: "content", created_at: stamp, updated_at: stamp, conclusion: "Analyst conclusion, not a source claim.", unknowns: "No verified trading outcome.", entries: [{snapshot: snapshot(article(), 7), role: "context", note: "Private analyst note."}]});
const backup = () => ({version: 1, active_id: "synthetic-research", collections: [collection()], watchlist: []});
function storage(initial = null) { let value = initial; return {getItem: () => value, setItem: (_key, next) => { value = next; }, raw: () => value}; }

test("reload preserves private conclusion and cited evidence; failed quota does not acknowledge or erase changes", () => {
  const disk = storage(JSON.stringify(backup())); const store = createStore(disk);
  store.commit(s => { s.collections[0].conclusion = "Updated private conclusion"; });
  assert.equal(createStore(disk).get().collections[0].conclusion, "Updated private conclusion");
  assert.equal(createStore(disk).get().collections[0].entries[0].snapshot.evidence_id, 7);
  const before = disk.raw(); disk.setItem = () => { throw new Error("QuotaExceededError"); };
  assert.throws(() => store.commit(s => { s.collections[0].question = "Unsaved replacement"; }), /Quota/);
  assert.equal(disk.raw(), before); assert.equal(store.get().collections[0].question, "How is risk bounded?");
});

test("invalid import and unsafe source URLs preserve existing work; valid import merges rather than overwrites", () => {
  const disk = storage(JSON.stringify(backup())); const store = createStore(disk);
  const bad = backup(); bad.collections[0].entries[0].snapshot.source_url = "javascript:alert(1)";
  assert.throws(() => store.importBackup(bad)); assert.equal(store.get().collections.length, 1);
  store.importBackup(backup()); const next = store.get();
  assert.equal(next.collections.length, 2); assert.notEqual(next.collections[0].id, next.collections[1].id);
  assert.equal(next.collections[0].conclusion, "Analyst conclusion, not a source claim.");
  assert.equal(next.collections[1].entries[0].note, "Private analyst note.");
  assert.equal(next.active_id, next.collections[1].id);
});

test("prototype values are not accepted as evidence roles or research purposes", () => {
  const badRole = backup(); badRole.collections[0].entries[0].role = "constructor";
  assert.throws(() => validateBackup(badRole));
  const badPurpose = backup(); badPurpose.collections[0].purpose = "toString";
  assert.throws(() => validateBackup(badPurpose));
});

test("draft export blocks withdrawn, revised and revoked evidence instead of republishing cached data", async () => {
  await assert.rejects(revalidate(collection(), async () => { throw new Error("HTTP 404"); }), /404/);
  await assert.rejects(revalidate(collection(), async () => ({...article(), version: 2})), /phiên bản/);
  await assert.rejects(revalidate(collection(), async () => ({...article(), evidence: []})), /công khai/);
});

test("export refreshes imported citation text from the public API and separates interpretation from source", async () => {
  const c = collection(); c.entries[0].snapshot.quote = "Tampered backup source quote";
  c.entries[0].snapshot.url = "https://www.binance.com/en/square/post/99999999";
  const routes = [];
  const validated = await revalidate(c, async route => { routes.push(route); return article(); });
  const draft = makeDraft(validated);
  assert.deepEqual(routes, ["/api/articles/12345678"]);
  assert.ok(draft.includes("https://www.binance.com/en/square/post/12345678"));
  assert.ok(draft.includes(article().excerpt)); assert.ok(!draft.includes("Tampered backup"));
  assert.ok(draft.includes("Diễn giải của người nghiên cứu: Private analyst note."));
  assert.ok(draft.includes("Phiên bản: 1")); assert.ok(draft.includes("SHA-256: " + "a".repeat(64)));
});

test("conclusion-only reports and unsupported confirmation without unknowns cannot masquerade as completed research", () => {
  const c = collection(); c.entries = []; assert.throws(() => makeDraft(c), /bằng chứng/);
  const onlySupport = collection(); onlySupport.entries[0].role = "support"; onlySupport.unknowns = "";
  assert.throws(() => makeDraft(onlySupport), /phản bác/);
});

test("a stale tab cannot overwrite a newer private conclusion", () => {
  const disk = storage(JSON.stringify(backup()));
  const first = createStore(disk), stale = createStore(disk);
  first.commit(s => { s.collections[0].conclusion = "Newer conclusion from first tab"; });
  assert.throws(() => stale.commit(s => { s.collections[0].conclusion = "Old tab replacement"; }), /tab khác/);
  assert.equal(createStore(disk).get().collections[0].conclusion, "Newer conclusion from first tab");
});

test("bookmark deltas merge another tab's saves without restoring an intentional removal", () => {
  const disk = storage("[]"), beforeA = [], beforeB = [];
  BTIWorkspace.mergeBookmarks(disk, beforeA, ["12345678"]);
  const merged = BTIWorkspace.mergeBookmarks(disk, beforeB, ["87654321"]);
  assert.deepEqual(new Set(merged), new Set(["12345678", "87654321"]));
  const removed = BTIWorkspace.mergeBookmarks(disk, ["12345678"], []);
  assert.deepEqual(removed, ["87654321"]);
});

test("a persisted workspace cannot exceed its own backup import size limit", () => {
  const disk = storage(JSON.stringify(backup())), store = createStore(disk), before = disk.raw();
  assert.throws(() => store.commit(s => {
    s.collections[0].entries = Array.from({length: 60}, (_, i) => ({
      snapshot: {...snapshot(article()), post_id: String(20000000 + i)},
      role: "context", note: "é".repeat(20000)
    }));
  }));
  assert.equal(disk.raw(), before);
  assert.equal(store.get().collections[0].entries.length, 1);
});

test("opaque portfolio IDs and historical evidence retain their exact source revision", async () => {
  const live = {...article(), post_id: "copy-trading-home-30d:1234567890123456789", version: 2, checksum: "b".repeat(64), excerpt: "New revision text must not replace the historical evidence quotation."};
  live.versions = [{version_no: 1, checksum: "a".repeat(64), fetched_at: stamp, excerpt: "Historical source quotation with an explicit observation timestamp.", source_url: "https://web.archive.org/web/20261020000000id_/https://www.binance.com/en/copy-trading"}];
  live.evidence[0].version = 1; live.evidence[0].checksum = "a".repeat(64);
  const c = collection(); c.entries[0].snapshot = snapshot(live, 7);
  const valid = validateBackup({version: 1, active_id: c.id, collections: [c], watchlist: []});
  const checked = await revalidate(valid.collections[0], async () => live);
  assert.equal(checked.entries[0].snapshot.version, 1);
  assert.equal(checked.entries[0].snapshot.checksum, "a".repeat(64));
  assert.equal(checked.entries[0].snapshot.quote, live.versions[0].excerpt);
  assert.equal(checked.entries[0].snapshot.source_url, live.versions[0].source_url);
  const unavailable = {...live, versions: []};
  await assert.rejects(revalidate(valid.collections[0], async () => unavailable), /lịch sử/);
});

test("v1 state migrates notes, citations and watches to canonical v2 without writing on read", () => {
  const original = backup();
  original.watchlist.push({id: "watch", label: "Private watch", entity: "", topic: "risk", asset: "", created_at: stamp, checked_at: stamp});
  const encoded = JSON.stringify(original), disk = storage(encoded);
  let writes = 0; const persist = disk.setItem;
  disk.setItem = (...args) => { writes++; persist(...args); };
  const store = createStore(disk), migrated = store.get();
  assert.equal(migrated.version, 2); assert.equal(writes, 0); assert.equal(disk.raw(), encoded);
  assert.equal(migrated.collections[0].question, original.collections[0].question);
  assert.equal(migrated.collections[0].conclusion, original.collections[0].conclusion);
  assert.equal(migrated.collections[0].unknowns, original.collections[0].unknowns);
  assert.deepEqual(migrated.collections[0].entries, original.collections[0].entries);
  assert.deepEqual(migrated.watchlist, original.watchlist);
  assert.equal(Object.keys(migrated.collections[0].plan).length, 17);
  assert.ok(Object.values(migrated.collections[0].plan).every(value => value === ""));
  store.commit(s => { s.collections[0].plan.angle = "Private writing angle"; });
  assert.equal(writes, 1); assert.equal(JSON.parse(disk.raw()).version, 2);
  assert.equal(createStore(disk).get().collections[0].plan.angle, "Private writing angle");
  assert.equal(original.version, 1);
});

test("near-limit v1 workspaces remain saveable and their migrated backups can restore all research", () => {
  const original = backup();
  original.collections = Array.from({length: 100}, (_, i) => ({...collection(), id: `legacy-${i}`}));
  original.active_id = original.collections[0].id;
  const legacyLimit = 2 * 1024 * 1024;
  let remaining = legacyLimit - 100 - Buffer.byteLength(JSON.stringify(original));
  for (const c of original.collections) {
    const chars = Math.min(20000, Math.floor(remaining / 2));
    c.entries[0].note = "é".repeat(chars); remaining -= 2 * chars;
    if (remaining === 1 && chars < 20000) { c.entries[0].note += "x"; remaining--; }
  }
  assert.equal(remaining, 0);
  const encoded = JSON.stringify(original), disk = storage(encoded), store = createStore(disk);
  assert.equal(disk.raw(), encoded);
  store.commit(s => { s.collections[0].plan.angle = "Saved after a full-size legacy migration"; });
  const restored = createStore(storage());
  restored.importBackup(JSON.parse(JSON.stringify(store.get())));
  assert.deepEqual(restored.get().collections.map(c => c.entries), original.collections.map(c => c.entries));
  assert.equal(restored.get().collections[0].plan.angle, "Saved after a full-size legacy migration");
});

test("v2 plans roundtrip and import all purposes without overwriting or dropping inactive fields", () => {
  const v2 = validateBackup(backup()), c = v2.collections[0];
  for (const key of Object.keys(c.plan)) c.plan[key] = `Analyst proposal: ${key}`;
  c.purpose = "strategy";
  const disk = storage(JSON.stringify(v2)), store = createStore(disk);
  assert.deepEqual(store.get(), v2);
  store.commit(s => { s.collections[0].purpose = "market"; });
  assert.deepEqual(createStore(disk).get().collections[0].plan, c.plan);
  store.importBackup(v2);
  const merged = store.get();
  assert.equal(merged.collections.length, 2);
  assert.equal(merged.collections[0].purpose, "market");
  assert.equal(merged.collections[1].purpose, "strategy");
  assert.deepEqual(merged.collections[1].plan, c.plan);
  assert.deepEqual(validateBackup(JSON.parse(JSON.stringify(merged))), merged);
  const legacy = backup(); store.importBackup(legacy);
  assert.equal(store.get().collections[2].entries[0].note, legacy.collections[0].entries[0].note);
  assert.ok(Object.values(store.get().collections[2].plan).every(value => value === ""));
});

test("content exports remain usable without optional plan values and escape analyst Markdown", () => {
  assert.doesNotThrow(() => makeDraft(collection()));
  const c = validateBackup(backup()).collections[0];
  c.plan.angle = "<script>not executable</script> [link](https://example.invalid)";
  c.plan.hypothesis = "Inactive strategy hypothesis";
  const draft = makeDraft(c);
  assert.ok(draft.includes("\\<script\\>"));
  assert.ok(!draft.includes("Inactive strategy hypothesis"));
  assert.deepEqual(missingPlanFields(c), []);
});

test("strategy saves incomplete plans but exports require each labeled proposal field", () => {
  const disk = storage(JSON.stringify(backup())), store = createStore(disk);
  store.commit(s => { s.collections[0].purpose = "strategy"; s.collections[0].plan.hypothesis = "Testable analyst idea"; });
  const c = store.get().collections[0];
  const fields = ["hypothesis", "universe", "horizon", "entry_rule", "exit_rule", "risk_rule", "benchmark", "costs", "rejection_rule"];
  assert.equal(missingPlanFields(c).length, 8);
  for (const key of fields) c.plan[key] = `Own proposal ${key}`;
  c.plan.thesis = "Inactive market proposal must stay private";
  const draft = makeDraft(c);
  assert.ok(!draft.includes("Inactive market proposal must stay private"));
  for (const key of fields) {
    const value = c.plan[key]; c.plan[key] = " \n ";
    assert.throws(() => makeDraft(c));
    c.plan[key] = value;
  }
  c.entries = []; assert.throws(() => makeDraft(c));
});

test("market frame requires its own fields without borrowing strategy proposals", () => {
  const c = validateBackup(backup()).collections[0]; c.purpose = "market";
  c.plan.hypothesis = "Not a market thesis"; c.plan.entry_rule = "Not a market scenario";
  const fields = ["thesis", "horizon", "base_case", "alternative_case", "invalidation", "monitoring"];
  for (const key of fields) c.plan[key] = `Analyst market proposal ${key}`;
  const draft = makeDraft(c);
  assert.ok(!draft.includes("Not a market thesis")); assert.ok(!draft.includes("Not a market scenario"));
  for (const key of fields) {
    const value = c.plan[key]; c.plan[key] = "";
    assert.throws(() => makeDraft(c)); c.plan[key] = value;
  }
  c.conclusion = ""; assert.throws(() => makeDraft(c));
});

test("plan validation is fixed, bounded and version-safe without silently losing proposals", () => {
  const valid = validateBackup(backup());
  for (const version of [0, 3, "2", null]) assert.throws(() => validateBackup({...valid, version}));
  for (const value of [null, [], "text", 1, {}]) {
    const bad = structuredClone(valid); bad.collections[0].plan = value;
    assert.throws(() => validateBackup(bad));
  }
  for (const value of ["x".repeat(4001), 42, null, {}, []]) {
    const bad = structuredClone(valid); bad.collections[0].plan.horizon = value;
    assert.throws(() => validateBackup(bad));
  }
  const atBound = structuredClone(valid); atBound.collections[0].plan.horizon = "x".repeat(4000);
  assert.equal(validateBackup(atBound).collections[0].plan.horizon.length, 4000);
  const unknown = structuredClone(valid); unknown.collections[0].plan.automatic_rule = "Must not disappear silently";
  assert.throws(() => validateBackup(unknown));
  const misversioned = structuredClone(valid); misversioned.version = 1;
  assert.throws(() => validateBackup(misversioned));
  const disk = storage(JSON.stringify(valid)), store = createStore(disk), before = disk.raw();
  assert.throws(() => store.importBackup(unknown)); assert.equal(disk.raw(), before);
});

test("migrated stale tabs cannot overwrite a newer v2 plan or failed-quota edits", () => {
  const disk = storage(JSON.stringify(backup())), first = createStore(disk), stale = createStore(disk);
  first.commit(s => { s.collections[0].plan.risk_rule = "New proposal protected from stale client"; });
  assert.throws(() => stale.commit(s => { s.collections[0].conclusion = "Old tab edit"; }), /tab khác/);
  const before = disk.raw(); disk.setItem = () => { throw new Error("QuotaExceededError"); };
  assert.throws(() => first.commit(s => { s.collections[0].plan.risk_rule = "Not persisted"; }), /Quota/);
  assert.equal(disk.raw(), before);
  assert.equal(first.get().collections[0].plan.risk_rule, "New proposal protected from stale client");
});

test("async source checks preserve plans and reject saved, unsaved and purpose races", async () => {
  const store = createStore(storage(JSON.stringify(backup())));
  const current = store.get().collections[0], before = JSON.stringify(current);
  let resolveSource;
  const pending = revalidate(current, () => new Promise(resolve => { resolveSource = resolve; }));
  store.commit(s => { s.collections[0].plan.angle = "Saved while source request was pending"; });
  resolveSource(article()); const checked = await pending;
  assert.equal(checked.plan.angle, "");
  assert.throws(() => assertUnchanged(before, store.get().collections[0], false), /đã đổi/);
  assert.throws(() => assertUnchanged(before, JSON.parse(before), true), /đã đổi/);
  const purposeChange = JSON.parse(before); purposeChange.purpose = "market";
  assert.throws(() => assertUnchanged(before, purposeChange, false), /đã đổi/);
  assert.doesNotThrow(() => assertUnchanged(before, JSON.parse(before), false));
  const c = store.get().collections[0], planBefore = structuredClone(c.plan);
  await revalidate(c, async () => article()); assert.deepEqual(c.plan, planBefore);
});
