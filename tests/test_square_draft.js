"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const {run} = require("../scripts/square-draft.js");

const early = "2025-05-15T12:00:00Z", late = "2025-05-16T12:00:00Z";
const known = day => `2025-05-${day}T09:00:00Z`;
function article(id, day, author, topic, side) {
  const statement = `If BTC closes ${side} 135 USD, the synthetic condition applies.`;
  const claim = {id: Number(id), statement, asset:"BTC", trigger:statement, created_at:known(day)};
  return {post_id:id, entity_slug:author, author, title:`Synthetic report by ${author}`, checksum:"a".repeat(64),
    url:`https://www.binance.com/en/square/post/${id}`, version:1, published_at:known(day), first_seen_at:known(day),
    fetched_at:known(day), language:"en", excerpt:"Synthetic source excerpt.", vi_intel:{topics:[topic], topic_labels_vi:[topic], assets:["BTC"]},
    research:{statement}, claims:[claim], evidence:[]};
}
function fixture() {
  const rows = [article("101","05","Alpha","Macro","above"), article("102","13","Beta","Risk","above"),
    article("103","15","Gamma","Structure","below"), article("104","16","Gamma","Structure","above")];
  const get = async route => {
    if (route.startsWith("/api/brief")) throw new Error("Current-ranked brief cannot establish a historical shortlist.");
    if (route.startsWith("/api/articles/")) return rows.find(a => a.post_id === route.split("/").at(-1)) || null;
    const query = new URL(route, "https://example.test").searchParams;
    const subset = rows.filter(a => a.published_at.slice(0,10) >= query.get("date_from") && a.published_at.slice(0,10) <= query.get("date_to"));
    return {items:subset, pagination:{total:subset.length, offset:0, has_more:false}};
  };
  return {rows,get,now: Date.parse(late)};
}
async function directory(t) {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "synthetic-pitches-"));
  t.after(() => fs.rm(dir, {recursive:true, force:true}));
  return dir;
}

test("entry before choosing a stream lists exactly three editorial questions without reading any source or ledger", async () => {
  const menu = await run({get: () => { throw new Error("unexpected read"); }});
  assert.deepEqual(menu.streams.map(s => s.id), ["market","analysis","trader"]);
  assert.equal(menu.status,"choose_stream");
  assert.equal(Object.hasOwn(menu,"pitches"),false);
});

test("market coverage and one-asset analysis produce distinct pitch sets at the same moment", async t => {
  const out = await directory(t), f = fixture();
  const market = await run({...f, out, stream:"market", asOf:early});
  const deep = await run({...f, out, stream:"analysis", asOf:early});
  assert.equal(market.pitches.length,1);
  assert.equal(deep.pitches.length,1);
  assert.match(market.pitches[0].title,/Độ phủ/);
  assert.match(deep.pitches[0].title,/BTC.*135 USD/);
  assert.equal(deep.pitches[0].brief_section,"conditional_scenarios");
  for (const p of [...market.pitches,...deep.pitches]) {
    assert.ok(p.sources.every(s => s.url && s.published_at <= p.as_of && s.fetched_at <= p.as_of));
    assert.match(p.change,/đầu tiên/);
    assert.ok(p.why_now);
  }
  const picked = await run({...f,out,stream:"analysis",pitch:deep.pitches[0].id});
  const text = await fs.readFile(path.join(picked.directory,"post.txt"),"utf8");
  assert.match(text,/hai phía/);
  assert.ok(text.includes(`https://intel.freqpilot.dev/api/articles/${deep.pitches[0].sources[0].post_id}`));
  assert.doesNotMatch(text,/lượt xem/);
  assert.equal((await run({...f,out,stream:"market",pitch:deep.pitches[0].id})).reason,"unknown_pitch_for_stream");
});

test("later publications, later captures, later claims and future engagement cannot enter replay", async t => {
  const out = await directory(t), f = fixture();
  f.rows[2].claims[0].created_at = known("16");
  const lateCapture = article("105","14","SecretFuture","FutureTopic","above");
  lateCapture.fetched_at = known("16");
  f.rows.push(lateCapture);
  const result = await run({...f,out,stream:"trader",asOf:early});
  assert.ok(result.pitches.length);
  const serialized = JSON.stringify(result);
  assert.doesNotMatch(serialized,/Gamma|SecretFuture|FutureTopic/);
  const nowVisible = await run({...f,out,stream:"trader",asOf:late});
  assert.match(JSON.stringify(nowVisible.pitches),/Gamma/);
  const earlyMarket = await run({...f,out,stream:"market",asOf:early});
  const laterMarket = await run({...f,out,stream:"market",asOf:late});
  assert.equal(earlyMarket.pitches[0].source_count,3);
  assert.equal(laterMarket.pitches[0].source_count,5);
  assert.match(laterMarket.pitches[0].change,/2 căn cứ mới/);
  const picked = await run({...f,out,stream:"market",pitch:laterMarket.pitches[0].id});
  const manifest = JSON.parse(await fs.readFile(path.join(picked.directory,"pitch.json"),"utf8"));
  assert.ok(manifest.sources.some(s => s.post_id === "105"));
  const impossible = await run({...f,out,stream:"analysis",asOf:"2025-05-05T08:59:59Z"});
  assert.equal(impossible.pitches.length,0);
  assert.match(impossible.reason,/Không có bài/);
});

test("reoffering and simultaneous draft materialization preserve one exact package; revoked claims block export", async t => {
  const out = await directory(t), f = fixture();
  const a = f.rows[2];
  const obs = (id,at,views,comments) => ({id, observed_at:at, checksum:a.checksum, version:a.version,
    pillar:"engagement", kind:"post_metrics", verification_level:"platform", value:{views,comments}});
  a.evidence = [obs(1,"2025-05-15T09:05:00Z",0,0),obs(2,"2025-05-15T09:30:00Z",10,1),obs(3,known("16"),987654,84)];
  const options = {...f,out,stream:"trader",asOf:early};
  const offered = await run(options), p = offered.pitches.find(x => x.story === "Gamma");
  const repeat = await run(options);
  assert.equal(repeat.reason,"no_new_development");
  assert.ok(repeat.previously_offered.includes(p.id));
  const select = () => run({...options,pitch:p.id});
  const results = await Promise.all([select(),select()]);
  assert.deepEqual(results.map(r=>r.status).sort(),["draft","skipped"]);
  const dir = results[0].directory;
  const backup = JSON.parse(await fs.readFile(path.join(dir,"draft.json"),"utf8"));
  const draft = BTIDrafts.active(BTIDrafts.validateBackup(backup));
  assert.equal(draft.notes,"");
  assert.deepEqual(draft.sources.map(s => s.evidence_id),[1,2]);
  const text = await fs.readFile(path.join(dir,"post.txt"),"utf8");
  assert.equal(text,BTIDrafts.renderText(draft));
  assert.doesNotMatch(text,/987.?654/);
  const ref = JSON.parse(await fs.readFile(path.join(dir,"pitch.json"),"utf8"));
  assert.equal(ref.id,p.id);
  assert.equal(ref.stream,"trader");
  a.evidence.shift();
  assert.equal((await select()).reason,"saved_evidence_changed");
  a.claims = [];
  assert.equal((await select()).reason,"source_or_evidence_changed");
});

test("a configured job proposes only its selected stream, and transport errors stop the run", async t => {
  const out = await directory(t), f = fixture(), config = path.join(out,"job.json");
  await fs.writeFile(config,JSON.stringify({out,stream:"market"}));
  const job = await run({...f,job:true,config});
  assert.equal(job.stream,"market");
  await fs.writeFile(config,JSON.stringify({out}));
  await assert.rejects(run({...f,job:true,config}),/stream/);
  await assert.rejects(run({...f,out,stream:"market",get:async()=>{throw new Error("Intel HTTP 429");}}),/429/);
  await assert.rejects(run({...f,out,stream:"market",asOf:"2025-02-30T00:00:00Z"}),/UTC/);
});

test("replaying backwards never reopens a later edition and cannot create a second package for the same event", async t => {
  const out = await directory(t), f = fixture();
  f.rows.pop();
  const later = (await run({...f,out,stream:"trader",asOf:late})).pitches[0];
  assert.equal((await run({...f,out,stream:"trader",pitch:later.id})).status,"draft");
  const earlier = (await run({...f,out,stream:"trader",asOf:early})).pitches[0];
  assert.equal(earlier.event_id,later.event_id);
  assert.notEqual(earlier.id,later.id);
  assert.ok(Date.parse(earlier.as_of) < Date.parse(later.as_of));
  const duplicate = await run({...f,out,stream:"trader",pitch:earlier.id});
  assert.equal(duplicate.reason,"already_drafted_other_cutoff");
  assert.equal(duplicate.directory,undefined);
});
