"use strict";

(() => {
  const DAY = 86_400_000;
  const format = new Intl.DateTimeFormat("vi-VN", {dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Ho_Chi_Minh"});
  const byId = id => document.getElementById(id);
  const isoDay = time => new Date(time).toISOString().slice(0, 10);
  let navigate, currentKey, controller, latestRead;

  function parseRoute(id) {
    const match = /^luong-(market|analysis|trader)(?:\/(\d{4}-\d{2}-\d{2}))?$/.exec(id);
    return match ? {stream: match[1], day: match[2] || ""} : null;
  }

  function node(tag, cls, text) {
    const n = document.createElement(tag);
    n.className = cls;
    n.textContent = text;
    return n;
  }
  function button(label, fn, primary = false) {
    const b = node("button", primary ? "hero-btn primary" : "inspect-btn", label);
    b.type = "button";
    b.addEventListener("click", fn);
    return b;
  }
  function link(label, href) {
    const a = node("a", "desk-source-link", label);
    a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer";
    return a;
  }
  function latest() {
    if (!latestRead) {
      latestRead = getJSON("/api/articles?sort=latest&lang=all&limit=1")
        .then(data => data.items[0] || null)
        .catch(err => { latestRead = null; throw err; });
    }
    return latestRead;
  }
  async function freshness() {
    const summary = byId("desk-freshness");
    try {
      const item = await latest();
      summary.textContent = item ? "Bài mới nhất trong kho theo ngày đăng gốc: " + format.format(new Date(item.published_at)) + " (giờ Việt Nam)." : "Kho chưa có bài nguồn công khai.";
    } catch {
      summary.replaceChildren("Chưa đọc được ngày bài nguồn mới nhất. ", button("Thử lại", freshness));
    }
  }
  function sources(pitch) {
    const box = node("details", "desk-evidence", "");
    box.append(node("summary", "", "Căn cứ và nguồn (" + pitch.sources.length + ")"));
    const list = node("ul", "", "");
    for (const source of pitch.sources) {
      const li = node("li", "", "");
      li.append(node("strong", "", source.author), node("span", "sample-note", " · " + format.format(new Date(source.published_at)) + " (giờ Việt Nam) · v" + source.version));
      const links = node("div", "desk-links", "");
      let origin;
      try { origin = new URL(source.url); } catch { /* Only the canonical Intel record is available. */ }
      if (origin?.protocol === "https:" && ["binance.com", "www.binance.com", "web.archive.org"].includes(origin.hostname) && !origin.username && !origin.password) {
        links.append(link("Đọc bài gốc ↗", origin.href));
      }
      links.append(link("Hồ sơ Intel ↗", "/?article=" + encodeURIComponent(source.post_id)));
      li.append(links); list.append(li);
    }
    box.append(list);
    return box;
  }
  function card(pitch, replay, read, signal) {
    const article = node("article", "desk-pitch", "");
    const origin = {stream: pitch.stream, pitch_id: pitch.id, as_of: pitch.as_of, replay};
    const action = button(BTIComposer.findEditorial(origin) ? "Viết tiếp bản nháp" : "Soạn từ đề này", async () => {
      const existing = BTIComposer.findEditorial(origin);
      if (existing) { BTIComposer.openDraft(existing.id); return; }
      action.disabled = true; message.textContent = "Đang đối chiếu lại nguồn…";
      try {
        const end = Date.parse(pitch.as_of);
        if (!replay) {
          const live = await BTIStreams.ideas(pitch.stream, Date.now(), read);
          if (!live.pitches.some(row => row.id === pitch.id)) throw Error("Đề đã thay đổi. Hãy tải lại danh sách.");
        }
        const checked = await BTIStreams.ideas(pitch.stream, end, read);
        const same = checked.pitches.find(row => row.id === pitch.id && row.body === pitch.body && JSON.stringify(row.sources) === JSON.stringify(pitch.sources));
        if (!same) throw Error("Nguồn đã thay đổi. Hãy tải lại danh sách.");
        const draft = await BTIStreams.makeDraft({...same, replay}, read);
        if (!draft) throw Error("Nguồn hoặc trích dẫn đã thay đổi. Hãy chọn lại đề.");
        if (signal.aborted) return;
        BTIComposer.create({...draft, editorial: origin});
      } catch (err) {
        if (!signal.aborted) message.replaceChildren("Chưa tạo được nháp. " + err.message + " ", button("Tải lại đề", () => refresh()));
      } finally {
        if (!signal.aborted) action.disabled = false;
      }
    }, true);
    const message = node("p", "sample-note", "");
    message.setAttribute("role", "status");
    article.append(node("h2", "", pitch.title), node("p", "desk-why", pitch.why_now), sources(pitch), action, message);
    return article;
  }
  async function render(route, signal) {
    const root = byId("desk-results");
    const currentDay = isoDay(Date.now());
    const end = route.day ? Date.parse(route.day + "T23:59:59.999Z") : Date.now();
    byId("desk-stream-name").textContent = BTIStreams.STREAMS[route.stream].name;
    byId("desk-question").textContent = BTIStreams.STREAMS[route.stream].question;
    byId("desk-asof").value = route.day ? "archive" : "now";
    byId("desk-day").max = isoDay(Date.parse(currentDay) - DAY);
    byId("desk-day").value = route.day;
    toggleCalendar();
    const stamp = byId("desk-cutoff");
    if (!Number.isFinite(end) || (route.day && (route.day >= currentDay || isoDay(end) !== route.day))) {
      stamp.textContent = "";
      root.replaceChildren(node("p", "empty", "Hãy chọn một ngày UTC đã kết thúc để xem dữ liệu lịch sử."));
      return;
    }
    stamp.textContent = (route.day ? "Kỳ lịch sử" : "Kỳ hiện tại") + " · Đối chiếu tới " + format.format(new Date(end)) + " (giờ Việt Nam).";
    root.replaceChildren(node("p", "desk-loading", "Đang đọc nguồn và đối chiếu đề bài…"));
    async function read(path) {
      const response = await fetch(path, {headers: {Accept: "application/json"}, signal: AbortSignal.any([signal, AbortSignal.timeout(60000)])});
      if (response.status === 404 && path.startsWith("/api/articles/")) return null;
      if (!response.ok) throw Error("HTTP " + response.status);
      return response.json();
    }
    try {
      const result = await BTIStreams.ideas(route.stream, end, read);
      if (signal.aborted) return;
      if (result.pitches.length) {
        root.replaceChildren(...result.pitches.map(p => card(p, Boolean(route.day), read, signal)));
        return;
      }
      const empty = node("div", "desk-empty", "");
      empty.append(node("h2", "", "Chưa đủ nguồn để gợi ý"), node("p", "", result.reason));
      root.replaceChildren(empty);
      if (!route.day) {
        const newest = await latest().catch(() => null);
        if (signal.aborted || !newest) return;
        // Include the latest publication and allow time for its public claims to have been observed.
        const day = isoDay(Math.min(Date.parse(newest.published_at) + 6 * DAY, Date.parse(currentDay) - DAY));
        if (day >= isoDay(end - 7 * DAY)) return;
        empty.append(button("Xem kỳ lịch sử · " + day.split("-").reverse().join("/"), () => navigate("luong-" + route.stream + "/" + day, {focus:true})));
      }
    } catch {
      if (signal.aborted) return;
      root.replaceChildren(node("p", "empty", "Chưa tải được đầy đủ nguồn cho kỳ này."), button("Thử lại", refresh));
    }
  }
  function refresh() {
    const route = parseRoute(location.hash.slice(1));
    currentKey = null; show(route);
  }
  function show(route) {
    const key = route ? route.stream + "/" + route.day : "";
    if (key === currentKey) return;
    currentKey = key;
    controller?.abort();
    if (!route) return;
    controller = new AbortController();
    render(route, controller.signal);
  }
  function toggleCalendar() {
    const historical = byId("desk-asof").value === "archive";
    byId("desk-day-label").hidden = byId("desk-apply").hidden = !historical;
    byId("desk-day").required = historical;
  }
  function start(go) {
    navigate = go;
    const today = byId("desk-today");
    const now = new Date();
    today.dateTime = now.toISOString();
    today.textContent = new Intl.DateTimeFormat("vi-VN",{dateStyle:"full",timeZone:"Asia/Ho_Chi_Minh"}).format(now);
    byId("desk-asof").addEventListener("change", () => {
      toggleCalendar();
      const route = parseRoute(location.hash.slice(1));
      if (byId("desk-asof").value === "now" && route) navigate("luong-" + route.stream);
      else byId("desk-day").focus();
    });
    byId("desk-period").addEventListener("submit", e => {
      e.preventDefault();
      const route = parseRoute(location.hash.slice(1));
      if (route && byId("desk-day").value) navigate("luong-" + route.stream + "/" + byId("desk-day").value, {focus:true});
    });
  }
  globalThis.BTIDesk = {parseRoute, show, start, freshness};
})();
