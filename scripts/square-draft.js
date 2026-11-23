"use strict";

const fs = require("node:fs/promises");
const path = require("node:path");
const {parseArgs} = require("node:util");
const {STREAMS, ideas, readPublic, digest} = require("./square-streams.js");
require("../src/bti/static/workspace.js");
require("../src/bti/static/drafts.js");

function cutoff(asOf, date, now) {
  if (date && asOf) throw new Error("Choose --as-of or --date.");
  if (date) {
    const day = Date.parse(`${date}T00:00:00Z`);
    if (!/^\d{4}-\d\d-\d\d$/.test(date) || !Number.isFinite(day) || new Date(day).toISOString().slice(0, 10) !== date || day > now)
      throw new Error("Invalid UTC archive date.");
    return Math.min(day + 86400000 - 1, now);
  }
  if (!asOf) return now;
  const end = Date.parse(asOf);
  if (!/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{3})?Z$/.test(asOf) || !Number.isFinite(end) || end > now ||
      new Date(end).toISOString() !== asOf.replace(/(?<=\d\d:\d\d:\d\d)Z$/, ".000Z")) throw new Error("Invalid UTC --as-of.");
  return end;
}

async function openLedger(out) {
  await fs.mkdir(out, {recursive: true});
  const {DatabaseSync} = require("node:sqlite");
  const db = new DatabaseSync(path.join(out, "editorial.sqlite3"));
  db.exec(`PRAGMA busy_timeout=5000;
    CREATE TABLE IF NOT EXISTS pitches (
      id TEXT PRIMARY KEY, event_id TEXT NOT NULL, stream TEXT NOT NULL, story TEXT NOT NULL,
      as_of TEXT NOT NULL, payload TEXT NOT NULL, drafted INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS pitches_event_time ON pitches(event_id, as_of)`);
  return db;
}

function summary(p, full = false) {
  const {body, developments, sources, ...fields} = p;
  const links = full || sources.length <= 3 ? sources : [sources[0], sources[1], sources.at(-1)];
  return {...fields, source_count: sources.length, sources: links};
}

function recordOffers(db, candidates, replay) {
  const pitches = [], previousIds = [];
  db.exec("BEGIN IMMEDIATE");
  try {
    for (const p of candidates) {
      const known = db.prepare("SELECT id FROM pitches WHERE event_id=? AND as_of<=? ORDER BY as_of DESC LIMIT 1").get(p.id, p.as_of);
      if (known) { previousIds.push(known.id); continue; }
      const previous = db.prepare("SELECT payload FROM pitches WHERE stream=? AND story=? AND as_of<=? ORDER BY as_of DESC, rowid DESC LIMIT 1")
        .get(p.stream, p.story, p.as_of);
      const prior = previous && JSON.parse(previous.payload);
      const additions = p.developments.filter(key => !prior?.developments.includes(key));
      if (prior && !additions.length) continue;
      p.change = prior ? `Có ${additions.length} căn cứ mới so với đề bài đã lưu tại ${prior.as_of}. ${p.why_now}` :
        "Đề bài đầu tiên của chủ đề này trong sổ biên tập. Chưa có mốc bài đã xuất bản để so sánh.";
      p.replay = replay;
      p.event_id = p.id;
      p.id = digest([p.event_id, p.as_of]).slice(0, 24);
      db.prepare("INSERT INTO pitches(id,event_id,stream,story,as_of,payload) VALUES (?,?,?,?,?,?)")
        .run(p.id, p.event_id, p.stream, p.story, p.as_of, JSON.stringify(p));
      pitches.push(summary(p));
    }
    db.exec("COMMIT");
  } catch (err) { db.exec("ROLLBACK"); throw err; }
  return {pitches, previously_offered: previousIds};
}

function comparison(a, end) {
  const valid = (a.evidence || []).filter(row => row.pillar === "engagement" && row.kind === "post_metrics" &&
    row.verification_level === "platform" && row.checksum === a.checksum && row.version === a.version &&
    Date.parse(row.observed_at) >= Date.parse(a.published_at) && Date.parse(row.observed_at) <= end &&
    ["views", "comments"].every(k => Number.isSafeInteger(row.value?.[k]) && row.value[k] >= 0)
  ).sort((x,y) => Date.parse(y.observed_at) - Date.parse(x.observed_at));
  const last = valid[0], first = last && valid.find(v => Date.parse(v.observed_at) < Date.parse(last.observed_at));
  if (!first || (first.value.views === last.value.views && first.value.comments === last.value.comments)) return null;
  const fmt = n => n.toLocaleString("vi-VN");
  return {sources: [first,last].map(v => BTIWorkspace.snapshot(a, v.id)),
    text: `Về độ phủ, lúc ${first.observed_at} Intel ghi nhận ${fmt(first.value.views)} lượt xem và ${fmt(first.value.comments)} bình luận. Đến ${last.observed_at}, bài có ${fmt(last.value.views)} lượt xem và ${fmt(last.value.comments)} bình luận.`};
}

async function makeDraft(p, get) {
  const end = Date.parse(p.as_of);
  const refs = p.stream === "market" ? [p.sources[0], p.sources.at(-1)] : p.sources;
  const sources = [], articles = [];
  for (const ref of refs) {
    const a = await get(`/api/articles/${ref.post_id}`);
    if (!a || a.post_id !== ref.post_id || a.version !== ref.version || a.fetched_at !== ref.fetched_at ||
        a.author !== ref.author || a.url !== ref.url || a.published_at !== ref.published_at ||
        !/^[a-f0-9]{64}$/.test(a.checksum) || (ref.checksum && a.checksum !== ref.checksum) ||
        (ref.claim_id && !a.claims?.some(c => c.id === ref.claim_id && c.statement === ref.statement &&
          c.created_at === ref.claim_known_at && Date.parse(c.created_at) <= end))) return null;
    sources.push(BTIWorkspace.snapshot({...a, engagement_observed_at: a.fetched_at}));
    articles.push(a);
  }
  const heading = (p.replay ? "Nhìn lại · " : "") + p.title;
  const draft = BTIDrafts.newDraft({title: heading, body: p.body, sources});
  if (p.stream === "trader") {
    const context = comparison(articles[0], end);
    if (context) {
      const extended = BTIDrafts.newDraft({...draft, body: draft.body + "\n\n" + context.text, sources: [...context.sources, ...sources.slice(1)]});
      if (BTIDrafts.checkDraft(extended).ok) return extended;
    }
  }
  return BTIDrafts.checkDraft(draft).ok ? draft : null;
}

async function saveDraft(out, p, draft) {
  const target = path.join(out, p.event_id);
  const stage = await fs.mkdtemp(path.join(out, ".square-"));
  try {
    const backup = BTIDrafts.encodeBackup({version: 2, active_id: draft.id, drafts: [draft]});
    await fs.writeFile(path.join(stage, "draft.json"), backup);
    await fs.writeFile(path.join(stage, "post.txt"), BTIDrafts.renderText(draft));
    await fs.writeFile(path.join(stage, "pitch.json"), JSON.stringify(summary(p, true), null, 2));
    try { await fs.rename(stage, target); }
    catch (err) {
      if (!["EEXIST", "ENOTEMPTY", "EPERM", "EACCES"].includes(err.code)) throw err;
      const saved = JSON.parse(await fs.readFile(path.join(target, "pitch.json"), "utf8"));
      if (saved.event_id !== p.event_id) throw err;
      if (saved.id !== p.id) return {status:"skipped", reason:"already_drafted_other_cutoff", id:p.id, stream:p.stream};
      const backup = JSON.parse(await fs.readFile(path.join(target, "draft.json"), "utf8"));
      const active = BTIDrafts.active(BTIDrafts.validateBackup(backup));
      const text = await fs.readFile(path.join(target, "post.txt"), "utf8");
      if (!active || BTIDrafts.renderText(active) !== text) throw err;
      if (text !== BTIDrafts.renderText(draft) || digest(active.sources) !== digest(draft.sources)) {
        return {status:"skipped", reason:"saved_evidence_changed", id:p.id, stream:p.stream};
      }
      return {status: "skipped", reason: "already_drafted", id:p.id, stream:p.stream, directory:target};
    }
    return {status: "draft", id:p.id, stream:p.stream, directory:target};
  } finally { await fs.rm(stage, {recursive:true, force:true}); }
}

async function run({out, stream, pitch, date, asOf, now = Date.now(), get = readPublic, job, config}) {
  if (job) {
    if (!config || stream || pitch || asOf || date) throw new Error("A job requires --config with an explicit stream and output directory.");
    const options = JSON.parse(await fs.readFile(config, "utf8"));
    stream = options.stream;
    out = out || options.out;
    if (!stream) throw new Error("Configure the operator's stream before running the job.");
  }
  if (!stream) return {status: "choose_stream", streams: Object.entries(STREAMS).map(([id, {name,question}]) => ({id,name,question}))};
  if (!Object.hasOwn(STREAMS, stream) || !out) throw new Error("Specify a valid --stream and private --out directory.");
  const end = cutoff(asOf, date, now), dir = path.resolve(out);
  const db = await openLedger(dir);
  try {
    if (pitch) {
      const saved = db.prepare("SELECT payload FROM pitches WHERE id=? AND stream=?").get(pitch, stream);
      if (!saved) return {status:"skipped", reason:"unknown_pitch_for_stream"};
      const p = JSON.parse(saved.payload);
      if ((date || asOf) && end !== Date.parse(p.as_of)) throw new Error("Select the original pitch cutoff.");
      if (!p.replay && now > Date.parse(p.as_of)) {
        const current = await ideas(stream, now, get);
        if (!current.pitches.some(candidate => candidate.id === p.event_id)) return {status:"skipped", reason:"refresh_expired_pitch"};
      }
      const fresh = await ideas(stream, Date.parse(p.as_of), get);
      const same = fresh.pitches.find(v => v.id === p.event_id && v.body === p.body && digest(v.sources) === digest(p.sources));
      if (!same) return {status:"skipped", reason:"source_or_evidence_changed"};
      const draft = await makeDraft(p, get);
      if (!draft) return {status:"skipped", reason:"source_or_export_unavailable"};
      const result = await saveDraft(dir, p, draft);
      if (result.directory) db.prepare("UPDATE pitches SET drafted=1 WHERE id=?").run(p.id);
      return result;
    }
    const current = await ideas(stream, end, get);
    const offered = recordOffers(db, current.pitches, Boolean(asOf || date));
    return {status: offered.pitches.length ? "choose_pitch" : "skipped", stream, as_of: new Date(end).toISOString(),
      reason: offered.pitches.length ? null : current.pitches.length ? "no_new_development" : current.reason, ...offered};
  } finally { db.close(); }
}

module.exports = {run};
if (require.main === module) {
  const options = {job:{type:"boolean"}};
  for (const name of ["out", "date", "as-of", "stream", "pitch", "config"]) options[name] = {type:"string"};
  const {values} = parseArgs({options});
  run({...values, asOf: values["as-of"]}).then(result => console.log(JSON.stringify(result,null,2))).catch(error => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
