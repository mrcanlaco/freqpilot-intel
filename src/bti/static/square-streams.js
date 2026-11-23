"use strict";

// Use the same time windows, source checks and proposal identities on the web and in Square Studio.
(() => {
  const DAY = 86_400_000;
  const STREAMS = {
    market: {name: "Toàn cảnh thị trường", question: "Chủ đề nào đổi độ phủ giữa hai kỳ bảy ngày?", section: "market_digest", days: 14},
    analysis: {name: "Phân tích chuyên sâu", question: "Nguồn mới củng cố hay làm yếu điều kiện nào của một luận điểm?", section: "conditional_scenarios", days: 14},
    trader: {name: "Góc nhìn trader", question: "Tác giả vừa nêu quan điểm gì và đã nói gì trước đó về cùng tài sản?", section: "creator_spotlights", days: 14}
  };
  const NO_DATA = {
    market: "Cần bài trong cả hai kỳ bảy ngày và ít nhất hai chủ đề hoặc tài sản trong kỳ mới.",
    analysis: "Cần hai phát biểu của hai tác giả về cùng tài sản, cùng đơn vị và một điều kiện giá có thể đối chiếu.",
    trader: "Chưa có phát biểu cụ thể của tác giả được Intel ghi nhận trong kỳ và được phép công khai ở mốc chọn."
  };
  async function digest(value) {
    const bytes = new TextEncoder().encode(JSON.stringify(value));
    const hash = await crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, "0")).join("");
  }
  const time = value => typeof value === "string" ? Date.parse(value) : NaN;
  const ymd = millis => new Date(millis).toISOString().slice(0, 10);
  const vnDate = value => value.slice(0, 10).split("-").reverse().join("/");
  const byNewest = (a, b) => time(b.published_at) - time(a.published_at) || a.post_id.localeCompare(b.post_id);
  const ORIGIN = "https://intel.freqpilot.dev";

  function inWindow(a, start, end) {
    return typeof a.post_id === "string" && /^\d+$/.test(a.post_id) && a.author && a.entity_slug &&
      Number.isInteger(a.version) && time(a.published_at) > start && time(a.published_at) <= end &&
      time(a.first_seen_at) <= time(a.fetched_at) && time(a.published_at) <= time(a.fetched_at) && time(a.fetched_at) <= end;
  }

  async function articlesAt(get, start, end) {
    const query = new URLSearchParams({date_from: ymd(start), date_to: ymd(end), sort: "latest", lang: "all", limit: "100"});
    const rows = [], seen = new Set();
    let offset = 0, total;
    for (;;) {
      query.set("offset", String(offset));
      const page = await get(`/api/articles?${query}`);
      const p = page.pagination;
      if (!p || !Number.isSafeInteger(p.total) || p.total < 0 || p.offset !== offset || !Array.isArray(page.items) ||
          (total !== undefined && total !== p.total) || p.total > 2000) throw new Error("Incomplete or changing public article window.");
      total = p.total;
      for (const item of page.items) {
        if (seen.has(item.post_id)) throw new Error("Public article pages changed during selection.");
        seen.add(item.post_id);
        if (inWindow(item, start, end)) rows.push(item);
      }
      if (!p.has_more) {
        if (seen.size !== total) throw new Error("Incomplete public article window.");
        return rows.sort(byNewest);
      }
      if (p.next_offset !== offset + page.items.length || !page.items.length) throw new Error("Invalid public cursor.");
      offset = p.next_offset;
    }
  }

  function source(a, claim) {
    const ref = {post_id: a.post_id, url: a.url, author: a.author, version: a.version, published_at: a.published_at,
      first_seen_at: a.first_seen_at, fetched_at: a.fetched_at};
    if (a.checksum) ref.checksum = a.checksum;
    if (claim) Object.assign(ref, {claim_id: claim.id, claim_known_at: claim.created_at, statement: claim.statement});
    return ref;
  }

  async function proposal(stream, story, title, why, body, rows, developments, asOf) {
    const refs = rows.map(({article, claim}) => source(article, claim));
    const id = (await digest([stream, story, developments])).slice(0, 24);
    return {id, stream, story, brief_section: STREAMS[stream].section, title: title.slice(0, 105),
      why_now: why, as_of: asOf, sources: refs, developments, body};
  }

  function counts(rows) {
    const result = new Map();
    for (const row of rows) {
      const labels = [...new Set([...(row.vi_intel?.assets || []), ...(row.vi_intel?.topic_labels_vi || [])])];
      for (const label of labels) result.set(label, (result.get(label) || 0) + 1);
    }
    return result;
  }

  async function market(rows, end, asOf) {
    const split = end - 7 * DAY, recent = rows.filter(a => time(a.published_at) > split);
    const prior = rows.filter(a => time(a.published_at) <= split);
    const current = counts(recent), previous = counts(prior);
    const categories = key => new Set(recent.flatMap(a => a.vi_intel?.[key] || [])).size;
    if (!prior.length || new Set(recent.map(a => a.entity_slug)).size < 2 ||
        (categories("assets") < 2 && categories("topics") < 2)) return [];
    const changes = [...new Set([...current.keys(), ...previous.keys()])].map(label => ({
      label, before: previous.get(label) || 0, after: current.get(label) || 0
    })).filter(v => v.before !== v.after).sort((a, b) =>
      Math.abs(b.after - b.before) - Math.abs(a.after - a.before) || a.label.localeCompare(b.label, "vi"));
    if (!changes.length) return [];
    const focus = changes.slice(0, 3);
    const title = `Độ phủ trên Square thay đổi ra sao ở nhóm ${focus[0].label}?`;
    const why = `Intel ghi nhận ${recent.length} bài trong bảy ngày kết thúc lúc ${asOf}, so với ${prior.length} bài trong bảy ngày liền trước.`;
    const body = [why, focus.map(c => `• ${c.label}: từ ${c.before} ${c.after < c.before ? "còn" : "lên"} ${c.after} bài.`).join("\n"),
      "Mỗi bài có thể thuộc nhiều nhóm. Các phép đếm mô tả độ phủ trong tập nguồn Intel. Để kết luận giá hoặc thanh khoản thay đổi cần thêm chuỗi dữ liệu thị trường."].join("\n\n");
    // The pitch identity changes when the covered posts change, never just because the clock advances.
    const developments = (await Promise.all(rows.map(a => digest([a.post_id, a.vi_intel?.assets || [], a.vi_intel?.topics || []])))).sort();
    return [await proposal("market", "coverage", title, why, body, [...recent, ...prior].map(article => ({article})),
      developments, asOf)];
  }

  async function knownClaims(rows, get, start, end) {
    const result = [];
    // Bound detail reads by original publication order, independent of future engagement/claim rankings.
    for (const row of rows.slice(0, 24)) {
      if (!row.research?.statement) continue;
      const a = await get(`/api/articles/${row.post_id}`);
      if (!a || a.post_id !== row.post_id || a.version !== row.version || a.fetched_at !== row.fetched_at ||
          a.url !== row.url || a.author !== row.author || !inWindow(a, start, end) || !/^[a-f0-9]{64}$/.test(a.checksum)) continue;
      for (const c of a.claims || []) {
        if (!Number.isSafeInteger(c.id) || !(time(c.created_at) <= end) || c.statement !== a.research?.statement ||
            !c.statement || c.statement.length > 240 || c.statement.split(/\s+/).length > 24) continue;
        result.push({article: a, claim: c});
        break;
      }
    }
    return result;
  }

  function comparativeReturn(statement) {
    const subjects = {equities: "cổ phiếu", stocks: "cổ phiếu", bitcoin: "BTC", ethereum: "ETH",
      btc: "BTC", eth: "ETH", bnb: "BNB", sol: "SOL", xrp: "XRP"};
    const leg = "(\\w+) (?:is|are|was|were) (up|down) (?:only )?(\\d+(?:\\.\\d+)?)%";
    const match = new RegExp(`^${leg} (?:while|whereas|and) ${leg}[.!]?$`, "i").exec(statement);
    if (!match || !Object.hasOwn(subjects, match[1].toLowerCase()) || !Object.hasOwn(subjects, match[4].toLowerCase())) return null;
    const label = i => `${subjects[match[i].toLowerCase()]} ${match[i+1].toLowerCase() === "up" ? "tăng" : "giảm"} ${match[i+2]}%`;
    return `${label(1)} còn ${label(4)}`;
  }

  function describe(claim) {
    return comparativeReturn(claim.statement) || `“${claim.statement}”`;
  }

  async function trader(claims, end, asOf) {
    const result = [], authors = new Set();
    const candidates = [...claims].sort((a, b) => Number(!!b.claim.asset) - Number(!!a.claim.asset) || byNewest(a.article, b.article));
    for (const entry of candidates) {
      const {article: a, claim: c} = entry;
      if (time(a.published_at) <= end - 7 * DAY || authors.has(a.entity_slug) || (!c.asset && !c.trigger && !/\d/.test(c.statement))) continue;
      const earlier = c.asset && claims.find(x => x.article.entity_slug === a.entity_slug && x.claim.asset === c.asset &&
        time(x.article.published_at) < time(a.published_at) && x.claim.statement !== c.statement);
      const translated = comparativeReturn(c.statement);
      const title = translated ? `Góc nhìn ${a.author}: ${translated}` :
        `${a.author} nhận định${c.asset ? " về " + c.asset : ""}: “${c.statement.split(/\s+/).slice(0, 8).join(" ")}…”`;
      const why = `Bài đăng ngày ${vnDate(a.published_at)}. Đến ${c.created_at}, Intel đã có phát biểu để đối chiếu: ${describe(c)}.`;
      const context = earlier ? `Trong bài ngày ${vnDate(earlier.article.published_at)}, cùng tác giả viết: ${describe(earlier.claim)}. Hai phát biểu cho phép so sánh cách lập luận; riêng việc nhắc lại tài sản chưa chứng minh tác giả đã đổi quan điểm.` :
        "Trong cửa sổ đã kiểm tra, Intel chưa có phát biểu trước đủ điều kiện để kết luận tác giả đổi quan điểm.";
      const note = translated ? "Để kiểm tra phép so sánh, cần biết chuỗi giá dùng cho từng tài sản, giá đầu kỳ và cuối kỳ. Trích đoạn chưa cung cấp đủ các mốc đó." :
        "Nhận định trên thuộc về tác giả. Cần đối chiếu dữ liệu gốc cùng thời điểm trước khi coi đây là một diễn biến đã được xác nhận.";
      const opening = translated ? `Trong bài ngày ${vnDate(a.published_at)}, ${a.author} dùng hai tỷ lệ trên để so sánh diễn biến của hai thị trường. Cả hai tỷ lệ đều do tác giả nêu.` :
        `Ngày ${vnDate(a.published_at)}, ${a.author} viết: ${describe(c)}.`;
      const body = [opening, context, note].join("\n\n");
      result.push(await proposal("trader", a.entity_slug, title, why, body, earlier ? [entry, earlier] : [entry],
        [await digest([a.entity_slug, c.asset, c.statement])], asOf));
      authors.add(a.entity_slug);
      if (result.length === 3) break;
    }
    return result;
  }

  function priceCondition(c) {
    // Require an explicit currency and role. A comma must never truncate a level.
    for (const role of ["trigger", "invalidation"]) {
      const text = c[role];
      if (!text || !c.asset || !text.toUpperCase().split(/[^A-Z0-9]+/).includes(c.asset.toUpperCase())) continue;
      const match = /\b(above|below)\s+(?:USD\s+(\d+(?:,\d{3})*(?:\.\d+)?)|(\d+(?:,\d{3})*(?:\.\d+)?)\s+USD)(?=\s|$|[.,;!?](?:\s|$))/i.exec(text);
      if (match) {
        const raw = match[2] || match[3];
        if (!/^(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?$/.test(raw)) continue;
        return {role, side: match[1].toLowerCase(), price: raw.replaceAll(",", "")};
      }
    }
    return null;
  }

  async function analysis(claims, asOf) {
    const result = [], assets = new Set();
    for (const fresh of claims) {
      const {article: a, claim: c} = fresh, condition = priceCondition(c);
      if (!c.asset || !condition || assets.has(c.asset)) continue;
      const older = claims.find(x => x.claim.asset === c.asset && x.article.entity_slug !== a.entity_slug &&
        time(x.article.published_at) < time(a.published_at) && priceCondition(x.claim)?.price === condition.price &&
        priceCondition(x.claim).role === condition.role);
      if (!older) continue;
      const agrees = priceCondition(older.claim).side === condition.side;
      const title = `${c.asset}: hai nguồn đọc mốc ${condition.price} USD như thế nào?`;
      const why = `Sau bài của ${older.article.author}, ${a.author} cũng đặt điều kiện quanh ${condition.price} USD trong bài ngày ${vnDate(a.published_at)}.`;
      const body = [why, `${older.article.author} viết: ${describe(older.claim)}. ${a.author} viết: ${describe(c)}.`,
        agrees ? "Nguồn thứ hai củng cố nhận xét rằng hai tác giả cùng theo dõi một phía của ngưỡng giá này." :
          "Hai tác giả theo dõi hai phía khác nhau của ngưỡng này. Phát biểu mới làm yếu cách hiểu rằng hai nguồn đồng thuận.",
        "Muốn kiểm tra điều kiện đã xảy ra, cần đối chiếu giá USD theo đúng sàn, khung nến và thời điểm mà từng tác giả sử dụng. Hai phát biểu chưa cung cấp phép xác nhận đó."].join("\n\n");
      result.push(await proposal("analysis", c.asset, title, why, body, [fresh, older],
        [await digest([a.entity_slug, c.statement]), await digest([older.article.entity_slug, older.claim.statement])], asOf));
      assets.add(c.asset);
      if (result.length === 3) break;
    }
    return result;
  }

  async function ideas(stream, end, get) {
    const start = end - STREAMS[stream].days * DAY, asOf = new Date(end).toISOString();
    const rows = await articlesAt(get, start, end);
    let pitches = [];
    if (stream === "market") pitches = await market(rows, end, asOf);
    else if (rows.length) {
      const claims = await knownClaims(rows, get, start, end);
      pitches = stream === "trader" ? await trader(claims, end, asOf) : await analysis(claims, asOf);
    }
    return {pitches, reason: rows.length ? NO_DATA[stream] : "Không có bài công khai đã được Intel thu thập trước mốc chọn trong cửa sổ này."};
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
    const links = refs.map(ref => `${ORIGIN}/api/articles/${ref.post_id}`).join("\n");
    const draft = BTIDrafts.newDraft({title: heading, body: `${p.body}\n\nHồ sơ Intel:\n${links}`, sources});
    if (p.stream === "trader") {
      const context = comparison(articles[0], end);
      if (context) {
        const extended = BTIDrafts.newDraft({...draft, body: draft.body + "\n\n" + context.text, sources: [...context.sources, ...sources.slice(1)]});
        if (BTIDrafts.checkDraft(extended).ok) return extended;
      }
    }
    return BTIDrafts.checkDraft(draft).ok ? draft : null;
  }

  const editorial = {STREAMS, ideas, makeDraft, ORIGIN};
  if (typeof module === "object" && module.exports) module.exports = editorial;
  else globalThis.BTIStreams = editorial;
})();
