"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const {run} = require("../scripts/square-draft.js");

function synthetic() {
  const published = "2025-05-03T10:00:00Z", observed = "2025-05-03T12:00:00Z";
  const lead = {
    post_id: "1001", url: "https://www.binance.com/en/square/post/1001", author: "Synthetic author",
    published_at: published, version: 1, engagement_observed_at: observed, engagement: {views: 15, comments: 1}
  };
  const checksum = "1".repeat(64);
  const row = {id: 5, pillar: "engagement", kind: "post_metrics", verification_level: "platform", version: 1, checksum};
  const article = {...structuredClone(lead), title: "Synthetic source", fetched_at: observed, checksum, evidence: [
    {...row, id: 6, observed_at: observed, value: {...lead.engagement}},
    {...row, observed_at: "2025-05-03T11:00:00Z", value: {views: 0, comments: 0}}
  ]};
  const section = {eligible: true, notable_articles: [lead]};
  const brief = {schema: "bti-intelligence-brief-v1", policy_contract: {
    read_only_projection: true, no_auto_publish_in_bti: true
  }, sections: {market_digest: section}};
  const get = async route => route.startsWith("/api/brief?") ? brief : article;
  return {brief, article, section, lead, get, now: Date.parse("2025-05-04T00:00:00Z")};
}

async function destination(t) {
  const out = await fs.mkdtemp(path.join(os.tmpdir(), "synthetic-draft-"));
  t.after(() => fs.rm(out, {recursive: true, force: true}));
  return out;
}

test("concurrent runs save one importable draft and an identical, citation-bearing text export", async t => {
  const out = await destination(t), fixture = synthetic();
  const invoke = () => run({...fixture, out, date: "2025-05-03"});
  const results = await Promise.all([invoke(), invoke()]);
  assert.deepEqual(results.map(r => r.status).sort(), ["draft", "skipped"]);
  const dir = path.join(out, "1001");
  const backup = await fs.readFile(path.join(dir, "draft.json"), "utf8");
  const draft = BTIDrafts.active(BTIDrafts.validateBackup(JSON.parse(backup)));
  assert.equal(draft.notes, "");
  assert.deepEqual(draft.sources.map(source => source.evidence_id), [5, 6]);
  assert.match(draft.title, /Nhìn lại/);
  assert.match(draft.body, /tăng 15 lượt và số bình luận tăng 1/);
  const text = await fs.readFile(path.join(dir, "post.txt"), "utf8");
  assert.equal(text, BTIDrafts.renderText(draft));
  assert.equal(text.split(fixture.article.url).length - 1, 1);
  assert.equal((await invoke()).reason, "already_drafted");
  assert.equal(await fs.readFile(path.join(dir, "draft.json"), "utf8"), backup);
  fixture.article.evidence.pop();
  await assert.rejects(BTIWorkspace.revalidate({entries: draft.sources.map(snapshot => ({snapshot}))}, fixture.get));
});

test("empty, stale, incomplete, changed and ineligible sources skip without writing", async t => {
  const out = await destination(t);
  for (const mutate of [
    f => { f.section.eligible = false; }, f => { f.article.evidence.pop(); },
    f => { f.lead.engagement.views++; }, f => { f.article.version++; },
    f => { f.article.evidence[1].checksum = "2".repeat(64); },
    f => { f.article.evidence[1].value.views = null; },
    f => { f.article.evidence[1].verification_level = "self_claim"; },
    f => { f.article.evidence[0].observed_at = f.article.evidence[1].observed_at; },
    f => { f.article.evidence[1].value = {...f.article.engagement}; },
    f => { f.now += 8 * 86400000; }
  ]) {
    const fixture = synthetic();
    mutate(fixture);
    assert.equal((await run({...fixture, out})).status, "skipped");
  }
  assert.deepEqual(await fs.readdir(out), []);
});

test("authorisation and HTTP failures stop a run without selecting another route", async t => {
  const out = await destination(t), fixture = synthetic();
  const calls = [];
  await assert.rejects(run({...fixture, out, get: async route => {
    calls.push(route);
    if (route.startsWith("/api/articles/")) throw new Error("Intel HTTP 429");
    return fixture.brief;
  }}), /429/);
  assert.equal(calls.length, 2);
  fixture.brief.policy_contract.read_only_projection = false;
  await assert.rejects(run({...fixture, out}), /policy/);
  await assert.rejects(run({...fixture, out, date: "2025-02-30"}), /archive date/);
});
