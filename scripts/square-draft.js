"use strict";

const fs = require("node:fs/promises");
const path = require("node:path");
const {parseArgs} = require("node:util");
require("../src/bti/static/workspace.js");
require("../src/bti/static/drafts.js");

const ORIGIN = "https://intel.freqpilot.dev";
const DAY = 86400000;
const count = n => Number.isSafeInteger(n) && n >= 0;
const num = n => n.toLocaleString("vi-VN");
const stamp = iso => new Intl.DateTimeFormat("vi-VN", {
  day: "2-digit", month: "2-digit", year: "numeric",
  hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "UTC"
}).format(new Date(iso));

async function readPublic(route) {
  const response = await fetch(new URL(route, ORIGIN), {
    redirect: "error", signal: AbortSignal.timeout(60000), headers: {Accept: "application/json"}
  });
  if (response.status === 404 && route.startsWith("/api/articles/")) return null;
  if (!response.ok) throw new Error(`Intel HTTP ${response.status}`);
  return response.json();
}

function observationDraft(lead, article, start, end, historical) {
  if (!article || !/^\d+$/.test(lead.post_id) || article.post_id !== lead.post_id ||
      article.version !== lead.version || !/^[a-f0-9]{64}$/.test(article.checksum) ||
      article.url !== lead.url || article.author !== lead.author ||
      article.published_at !== lead.published_at || article.engagement_observed_at !== lead.engagement_observed_at) return null;
  const published = Date.parse(article.published_at);
  if (!(published >= start && published < end)) return null;
  const rows = article.evidence.filter(row =>
    row.pillar === "engagement" && row.kind === "post_metrics" && row.verification_level === "platform" &&
    row.version === article.version && row.checksum === article.checksum &&
    Date.parse(row.observed_at) >= published && Date.parse(row.observed_at) < end &&
    count(row.value?.views) && count(row.value?.comments)
  ).sort((a, b) => Date.parse(b.observed_at) - Date.parse(a.observed_at));
  const last = rows[0];
  if (!last || last.observed_at !== article.engagement_observed_at ||
      ["views", "comments"].some(k => last.value[k] !== article.engagement?.[k] ||
        last.value[k] !== lead.engagement?.[k])) return null;
  const first = rows.find(row => Date.parse(row.observed_at) < Date.parse(last.observed_at));
  if (!first || (first.value.views === last.value.views && first.value.comments === last.value.comments)) return null;

  const change = last.value.views - first.value.views;
  const changeText = change === 0 ? "giữ nguyên" : `${change > 0 ? "tăng" : "giảm"} ${num(Math.abs(change))} lượt`;
  const topic = article.vi_intel?.topic_labels_vi?.find(label => lead.topic_labels_vi?.includes(label));
  const title = `${historical ? "Nhìn lại" : "Nhịp thảo luận"} · ${topic || "Cộng đồng Square"}`;
  const comments = last.value.comments - first.value.comments;
  const commentsText = comments === 0 ? "số bình luận giữ nguyên" :
    `số bình luận ${comments > 0 ? "tăng" : "giảm"} ${num(Math.abs(comments))}`;
  const shortTitle = article.title.trim();
  const citedTitle = shortTitle.length <= 110 && shortTitle.split(/\s+/).length <= 16 &&
    !/[\r\n]/.test(shortTitle) ? ` với tiêu đề “${shortTitle}”` : "";
  const body = [
    `${article.author} đăng bài${citedTitle} lúc ${stamp(article.published_at)} UTC. Giữa hai lần Intel ghi nhận, lượt xem ${changeText} và ${commentsText}.`,
    `Lúc ${stamp(first.observed_at)} UTC, bài có ${num(first.value.views)} lượt xem và ${num(first.value.comments)} bình luận. Đến ${stamp(last.observed_at)} UTC, hai con số lần lượt là ${num(last.value.views)} và ${num(last.value.comments)}.`,
    "Một người có thể xem bài nhiều lần. Muốn biết độc giả đồng tình hay phản bác quan điểm của tác giả, cần đọc nội dung bình luận.",
    `Đối chiếu hai lần ghi nhận: ${ORIGIN}/api/articles/${lead.post_id}`
  ].join("\n\n");
  const sources = [first, last].map(row => BTIWorkspace.snapshot(article, row.id));
  const draft = BTIDrafts.newDraft({title, body, sources});
  return BTIDrafts.checkDraft(draft).ok ? draft : null;
}

async function saveDraft(out, draft) {
  // The stable post ID also deduplicates changes in rank, counters and brief IDs.
  const target = path.join(out, draft.sources[0].post_id);
  await fs.mkdir(out, {recursive: true});
  const stage = await fs.mkdtemp(path.join(out, ".square-"));
  try {
    const backup = BTIDrafts.encodeBackup({version: 2, active_id: draft.id, drafts: [draft]});
    await fs.writeFile(path.join(stage, "draft.json"), backup, "utf8");
    await fs.writeFile(path.join(stage, "post.txt"), BTIDrafts.renderText(draft), "utf8");
    try {
      await fs.rename(stage, target);
    } catch (error) {
      if (!["EEXIST", "ENOTEMPTY", "EPERM", "EACCES"].includes(error.code)) throw error;
      // Only acknowledge a completed package. Propagate unrelated permission errors.
      const saved = BTIDrafts.validateBackup(JSON.parse(await fs.readFile(path.join(target, "draft.json"), "utf8")));
      if (BTIDrafts.active(saved)?.sources[0]?.post_id !== draft.sources[0].post_id) throw error;
      await fs.access(path.join(target, "post.txt"));
      return {status: "skipped", reason: "already_drafted", directory: target};
    }
    return {status: "draft", directory: target};
  } finally {
    await fs.rm(stage, {recursive: true, force: true});
  }
}

async function run({out, date, now = Date.now(), get = readPublic}) {
  if (!out) throw new Error("Specify --out DIRECTORY for the private draft archive.");
  const query = new URLSearchParams({window: "7d", lang: "vi_first", max_items: "6"});
  let start = now - 7 * DAY, end = now + 1;
  if (date !== undefined) {
    start = Date.parse(`${date}T00:00:00Z`);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !Number.isFinite(start) ||
        new Date(start).toISOString().slice(0, 10) !== date || start > now) throw new Error("Invalid UTC archive date.");
    end = Math.min(start + DAY, now + 1);
    query.set("window", "custom");
    query.set("date_from", date);
    query.set("date_to", date);
  }
  const brief = await get(`/api/brief?${query}`);
  if (brief.schema !== "bti-intelligence-brief-v1" || brief.policy_contract?.read_only_projection !== true ||
      brief.policy_contract?.no_auto_publish_in_bti !== true) throw new Error("Unsupported Intel brief policy.");
  const section = brief.sections.market_digest;
  if (!section.eligible || !section.notable_articles.length) return {status: "skipped", reason: "no_public_articles"};
  for (const lead of section.notable_articles) {
    if (typeof lead.post_id !== "string" || !/^\d+$/.test(lead.post_id)) continue;
    const article = await get(`/api/articles/${lead.post_id}`);
    const draft = observationDraft(lead, article, start, end, date !== undefined);
    if (draft) return saveDraft(path.resolve(out), draft);
  }
  return {status: "skipped", reason: "no_comparable_public_observations"};
}

module.exports = {run};
if (require.main === module) {
  const {values} = parseArgs({options: {out: {type: "string"}, date: {type: "string"}}});
  run(values).then(result => console.log(JSON.stringify(result))).catch(error => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
