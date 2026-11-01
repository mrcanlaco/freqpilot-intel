"use strict";

// Personal research never leaves this browser except an explicit file download.
(() => {
  const KEY = "bti_research_workspace_v1";
  // Keep one canonical bucket so already-open v1 tabs detect changed raw storage.
  // v1 allowed 2 MiB; 32 KiB covers empty-plan migration for all 100 collections.
  const MAX_WORKSPACE_BYTES = 2 * 1024 * 1024 + 32 * 1024;
  const ROLES = {support: "Xác nhận", refute: "Phản bác", context: "Bối cảnh"};
  const PURPOSES = {content: "Nội dung có nguồn", strategy: "Nghiên cứu phương pháp", market: "Góc nhìn thị trường"};
  const TOPICS = new Set(["", "risk", "structure", "macro", "strategy", "psychology", "ecosystem"]);
  const PLAN_FIELDS = {
    audience: "Độc giả", angle: "Góc tiếp cận", outline: "Dàn ý",
    hypothesis: "Giả thuyết", universe: "Phạm vi tài sản", horizon: "Khung thời gian",
    entry_rule: "Quy tắc vào", exit_rule: "Quy tắc thoát", risk_rule: "Quy tắc rủi ro",
    benchmark: "Mốc đối chiếu", costs: "Chi phí giả định", rejection_rule: "Tiêu chí bác bỏ",
    thesis: "Luận điểm", base_case: "Kịch bản cơ sở", alternative_case: "Kịch bản thay thế",
    invalidation: "Điều kiện vô hiệu", monitoring: "Kế hoạch theo dõi"
  };
  const PLAN_PURPOSES = {
    content: {title: "Kế hoạch nội dung", fields: ["audience", "angle", "outline"], required: false},
    strategy: {title: "Khung nghiên cứu phương pháp", fields: ["hypothesis", "universe", "horizon", "entry_rule", "exit_rule", "risk_rule", "benchmark", "costs", "rejection_rule"], required: true},
    market: {title: "Khung nghiên cứu thị trường", fields: ["thesis", "horizon", "base_case", "alternative_case", "invalidation", "monitoring"], required: true}
  };
  const PLAN_NOTICE = "Tất cả trường kế hoạch là đề xuất do bạn tự viết, không phải quy tắc của tác giả nguồn, hệ thống được tái dựng, chiến lược sẵn để backtest, dự báo hay hiệu suất đã xác minh. Không tự điền từ nguồn.";
  const emptyPlan = () => Object.fromEntries(Object.keys(PLAN_FIELDS).map(key => [key, ""]));
  const empty = () => ({version: 2, active_id: null, collections: [], watchlist: []});
  const copy = value => JSON.parse(JSON.stringify(value));
  const now = () => new Date().toISOString();
  const id = () => crypto.randomUUID();
  const text = (v, max, required = false) => {
    if (typeof v !== "string" || v.length > max || (required && !v.trim())) throw new Error("Dữ liệu văn bản không hợp lệ hoặc quá dài.");
    return v;
  };
  const date = v => {
    if (typeof v !== "string" || !/^\d{4}-\d\d-\d\dT.*(?:Z|[+-]\d\d:\d\d)$/.test(v) || !Number.isFinite(Date.parse(v))) throw new Error("Thời điểm không hợp lệ.");
    return new Date(v).toISOString();
  };
  const sourceURL = v => {
    const url = new URL(text(v, 2048, true));
    if (url.protocol !== "https:" || url.username || url.password || !["www.binance.com", "binance.com", "web.archive.org"].includes(url.hostname)) throw new Error("URL nguồn không thuộc Binance hoặc archive công khai.");
    return url.href;
  };
  const quote = v => text(v || "", 240).split(/\s+/).slice(0, 24).join(" ");
  function snapshot(item, evidenceId = null) {
    const ev = evidenceId === null ? null : (item.evidence || []).find(e => e.id === evidenceId);
    if (evidenceId !== null && !ev) throw new Error("Bằng chứng không còn được phép công khai trong bài này.");
    const version = ev?.version ? item.versions?.find(v => v.version_no === ev.version) : null;
    if (ev?.version && ev.version !== item.version && !version) throw new Error("Không còn phiên bản nguồn lịch sử để đối chiếu bằng chứng.");
    return {
      post_id: String(item.post_id), evidence_id: evidenceId, title: item.title,
      author: item.author, url: item.url, source_url: version?.source_url || item.citation?.source_url || item.citation?.source_snapshot_url || item.url,
      version: ev?.version || item.version, checksum: ev?.checksum || item.checksum || "", published_at: item.published_at,
      fetched_at: version?.fetched_at || item.fetched_at, observed_at: ev?.observed_at || item.engagement_observed_at || item.fetched_at,
      quote: quote(version?.excerpt || item.excerpt), verification: ev?.verification_level || item.provenance_type || "unknown",
      limitations: ev?.limitations || "Trích đoạn nguồn công khai; không phải xác minh độc lập hoặc bằng chứng lợi nhuận."
    };
  }
  function validateSnapshot(raw) {
    if (!raw || typeof raw !== "object") throw new Error("Mã nội dung không hợp lệ.");
    const postId = text(raw.post_id, 200, true);
    if (/[\\/\u0000-\u001f]/.test(postId) || postId.includes("..")) throw new Error("Mã nội dung không hợp lệ.");
    if (!Number.isSafeInteger(raw.version) || raw.version < 1) throw new Error("Phiên bản nguồn không hợp lệ.");
    if (raw.evidence_id !== null && (!Number.isSafeInteger(raw.evidence_id) || raw.evidence_id < 1)) throw new Error("Mã bằng chứng không hợp lệ.");
    const checksum = text(raw.checksum, 64);
    if (checksum && !/^[a-f\d]{64}$/i.test(checksum)) throw new Error("Checksum không hợp lệ.");
    return {
      post_id: raw.post_id, evidence_id: raw.evidence_id, title: text(raw.title, 1000, true),
      author: text(raw.author, 300, true), url: sourceURL(raw.url), source_url: sourceURL(raw.source_url),
      version: raw.version, checksum, published_at: raw.published_at ? date(raw.published_at) : null,
      fetched_at: date(raw.fetched_at), observed_at: date(raw.observed_at), quote: quote(raw.quote),
      verification: text(raw.verification, 100), limitations: text(raw.limitations, 3000)
    };
  }
  function validatePlan(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)
        || Object.keys(raw).some(key => !Object.hasOwn(PLAN_FIELDS, key))) throw new Error("Kế hoạch riêng không đúng định dạng.");
    return Object.fromEntries(Object.keys(PLAN_FIELDS).map(key => [key, text(raw[key], 4000)]));
  }
  function missingPlanFields(c) {
    const config = PLAN_PURPOSES[c.purpose];
    if (!config) throw new Error("Mục đích nghiên cứu không hợp lệ.");
    const plan = validatePlan(c.plan === undefined ? emptyPlan() : c.plan);
    return config.required ? config.fields.filter(key => !plan[key].trim()).map(key => PLAN_FIELDS[key]) : [];
  }
  function assertUnchanged(before, current, unsaved) {
    if (unsaved || JSON.stringify(current) !== before) throw new Error("Nghiên cứu hoặc ghi chú đã đổi trong lúc đối chiếu. Hãy lưu rồi thử lại.");
  }
  function validateBackup(raw) {
    if (!raw || ![1, 2].includes(raw.version) || !Array.isArray(raw.collections) || !Array.isArray(raw.watchlist) || raw.collections.length > 100 || raw.watchlist.length > 100) throw new Error("Bản sao không đúng định dạng kho nghiên cứu v1/v2 (tối đa 100 nghiên cứu/danh sách theo dõi).");
    const ids = new Set();
    const collections = raw.collections.map(c => {
      const key = text(c.id, 100, true);
      if (ids.has(key) || !Object.hasOwn(PURPOSES, c.purpose) || !Array.isArray(c.entries) || c.entries.length > 500) throw new Error("Nghiên cứu trùng hoặc không hợp lệ.");
      ids.add(key);
      const entries = new Set();
      if (raw.version === 1 && c.plan !== undefined) throw new Error("Kế hoạch riêng cần định dạng v2.");
      return {
        id: key, question: text(c.question, 300, true), purpose: c.purpose,
        created_at: date(c.created_at), updated_at: date(c.updated_at),
        conclusion: text(c.conclusion, 20000), unknowns: text(c.unknowns, 20000),
        plan: raw.version === 1 ? emptyPlan() : validatePlan(c.plan),
        entries: c.entries.map(e => {
          const s = validateSnapshot(e.snapshot);
          const ek = `${s.post_id}:${s.evidence_id}`;
          if (entries.has(ek) || !Object.hasOwn(ROLES, e.role)) throw new Error("Bằng chứng trùng hoặc vai trò không hợp lệ.");
          entries.add(ek);
          return {snapshot: s, role: e.role, note: text(e.note, 20000)};
        })
      };
    });
    const watchIds = new Set();
    const watchlist = raw.watchlist.map(w => {
      const key = text(w.id, 100, true);
      const entity = text(w.entity, 200);
      const asset = text(w.asset, 20).toUpperCase();
      if (watchIds.has(key) || !TOPICS.has(w.topic) || (!entity && !w.topic && !asset) || (entity && !/^[\w-]+$/.test(entity)) || (asset && !/^[A-Z\d]{1,20}$/.test(asset))) throw new Error("Danh sách theo dõi trùng hoặc bộ lọc không hợp lệ.");
      watchIds.add(key);
      return {id: key, label: text(w.label, 120, true), entity, topic: w.topic, asset, created_at: date(w.created_at), checked_at: w.checked_at ? date(w.checked_at) : null};
    });
    return {version: 2, active_id: ids.has(raw.active_id) ? raw.active_id : collections[0]?.id || null, collections, watchlist};
  }
  function encodeBackup(state) {
    const encoded = JSON.stringify(state);
    if (new TextEncoder().encode(encoded).byteLength > MAX_WORKSPACE_BYTES) throw new Error("Kho nghiên cứu vượt 2 MiB + 32 KiB. Hãy sao lưu và tách bớt nghiên cứu trước khi thêm.");
    return encoded;
  }
  function createStore(storage) {
    let state = empty();
    let saved = storage.getItem(KEY);
    if (saved) state = validateBackup(JSON.parse(saved));
    encodeBackup(state);
    const commit = mutate => {
      const next = copy(state);
      mutate(next);
      const valid = validateBackup(next);
      // Refuse stale-tab writes instead of overwriting work saved by another tab.
      if (storage.getItem(KEY) !== saved) throw new Error("Kho nghiên cứu đã đổi ở tab khác. Giữ lại ghi chú chưa lưu rồi tải lại trang.");
      const encoded = encodeBackup(valid);
      // Persist before acknowledging; quota/security errors never erase the old state.
      storage.setItem(KEY, encoded);
      saved = encoded; state = valid;
      return copy(state);
    };
    return {
      get: () => copy(state), commit,
      importBackup(raw) {
        const incoming = validateBackup(raw);
        return commit(s => {
          // Import merges by new local IDs so an old backup cannot overwrite newer work.
          const remap = new Map();
          for (const c of incoming.collections) { const key = id(); remap.set(c.id, key); s.collections.push({...c, id: key}); }
          s.active_id = remap.get(incoming.active_id) || s.active_id;
          for (const w of incoming.watchlist) {
            if (!s.watchlist.some(x => x.entity === w.entity && x.topic === w.topic && x.asset === w.asset)) s.watchlist.push({...w, id: id()});
          }
        });
      }
    };
  }
  function mergeBookmarks(storage, before, after) {
    const key = "bti_saved_articles_v1";
    const raw = JSON.parse(storage.getItem(key) || "[]");
    if (!Array.isArray(raw)) throw new Error("Tủ sách không đúng định dạng.");
    const latest = new Set(raw.map(String)), previous = new Set(before), desired = new Set(after);
    for (const value of desired) if (!previous.has(value)) latest.add(value);
    for (const value of previous) if (!desired.has(value)) latest.delete(value);
    storage.setItem(key, JSON.stringify([...latest]));
    return [...latest];
  }
  async function revalidate(collection, getJSON) {
    const cache = new Map();
    const failures = [];
    for (const entry of collection.entries) {
      const s = entry.snapshot;
      try {
        if (!cache.has(s.post_id)) cache.set(s.post_id, await getJSON(`/api/articles/${encodeURIComponent(s.post_id)}`));
        const live = cache.get(s.post_id);
        const refreshed = snapshot(live, s.evidence_id);
        if (refreshed.version !== s.version || (s.checksum && refreshed.checksum !== s.checksum)
            || (s.evidence_id !== null && date(refreshed.observed_at) !== date(s.observed_at))) throw new Error("Nguồn đã đổi phiên bản hoặc thời điểm quan sát; cần cập nhật thủ công.");
        // Never trust cached/imported source text/URLs even when its revision matches.
        entry.snapshot = validateSnapshot(refreshed);
      } catch (err) { failures.push(`${s.title}: ${err.message}`); }
    }
    if (failures.length) throw new Error(`Chưa thể xuất: ${failures.join("; ")}`);
    return collection;
  }
  const md = v => String(v || "").replace(/[\\`*_{}\[\]<>!#|]/g, "\\$&");
  function makeDraft(c) {
    if (!c.conclusion.trim() || !c.entries.length) throw new Error("Cần ít nhất một bằng chứng và kết luận của người nghiên cứu.");
    if (!c.entries.some(e => e.role !== "support") && !c.unknowns.trim()) throw new Error("Hãy thêm phản bác/bối cảnh hoặc ghi rõ khoảng trống kiểm chứng.");
    const missing = missingPlanFields(c);
    if (missing.length) throw new Error(`Cần điền kế hoạch trước khi xuất: ${missing.join(", ")}. Có thể lưu kế hoạch chưa hoàn tất.`);
    const config = PLAN_PURPOSES[c.purpose], plan = c.plan || emptyPlan();
    const lines = [`# ${md(c.question)}`, "", `Trạng thái: BẢN NHÁP RIÊNG · ${PURPOSES[c.purpose]}`, `Xuất lúc: ${now()}`, "", "## Kết luận của người nghiên cứu", md(c.conclusion), "", `## ${config.title} — đề xuất của người nghiên cứu`, PLAN_NOTICE];
    for (const key of config.fields) {
      if (plan[key].trim()) lines.push("", `### ${PLAN_FIELDS[key]}`, md(plan[key]));
    }
    if (!config.fields.some(key => plan[key].trim())) lines.push("", "Chưa ghi kế hoạch tùy chọn.");
    lines.push("", "## Bằng chứng nguồn và ghi chú riêng");
    c.entries.forEach((e, i) => {
      const s = e.snapshot;
      lines.push("", `### [${i + 1}] ${ROLES[e.role]} — ${md(s.title)}`, `Tác giả: ${md(s.author)}`, `Nguồn: ${s.url}`, `Bản nguồn quan sát: ${s.source_url}`, `Xuất bản gốc: ${s.published_at || "Chưa biết"} · Thu thập: ${s.fetched_at} · Quan sát bằng chứng: ${s.observed_at}`, `Phiên bản: ${s.version} · SHA-256: ${s.checksum || "Chưa biết"} · Evidence ID: ${s.evidence_id ?? "bài viết"}`, `Cấp nguồn: ${md(s.verification)}`, `Trích đoạn nguyên văn (không phải bản dịch): ${md(s.quote)}`, `Giới hạn: ${md(s.limitations)}`, `Diễn giải của người nghiên cứu: ${md(e.note) || "Chưa ghi"}`);
    });
    lines.push("", "## Điều chưa biết / cần phản biện", md(c.unknowns) || "Chưa ghi; không đồng nghĩa đã xác minh đầy đủ.", "", "Tương tác và giải nội dung không chứng minh lợi nhuận. Không phải khuyến nghị giao dịch. Không tự xuất bản.");
    return lines.join("\n");
  }
  const node = (tag, cls, value) => { const n = document.createElement(tag); if (cls) n.className = cls; if (value !== undefined) n.textContent = value; return n; };
  const button = (label, fn, cls = "inspect-btn") => { const b = node("button", cls, label); b.type = "button"; b.addEventListener("click", fn); return b; };
  function download(name, value, type) {
    const url = URL.createObjectURL(new Blob([value], {type}));
    const root = document.querySelector("#workspace-downloads");
    for (const old of root.querySelectorAll("a")) {
      if (old.download === name) { URL.revokeObjectURL(old.href); old.remove(); }
    }
    const link = node("a", "hero-btn secondary", `Tải ${name}`);
    link.href = url; link.download = name;
    navigate("workspace");
    for (let parent = root.parentElement; parent; parent = parent.parentElement) if (parent.tagName === "DETAILS") parent.open = true;
    root.append(link);
    link.focus();
  }
  let store, api, sourceNotice = "";
  function navigate(target, options = {}) {
    if (api?.navigate) api.navigate(target, options);
    else location.hash = target;
  }
  function nextAction(label, target) {
    const link = node("a", "hero-btn primary research-next-action", label);
    link.href = `#${target}`;
    link.addEventListener("click", event => { event.preventDefault(); navigate(target); });
    return link;
  }
  function renderContext() {
    const root = document.querySelector("#research-context-content");
    if (!root || !store) return;
    root.replaceChildren();
    const c = active();
    if (!c) {
      root.append(node("h3", "research-context-question", "Bạn đang tìm câu trả lời nào?"), node("p", "research-context-status", "Tạo một câu hỏi để gom nguồn và ghi nhận điều còn chưa biết. Ghi chú chỉ lưu trong trình duyệt này."));
      const create = nextAction("Đặt câu hỏi", "workspace");
      create.addEventListener("click", () => openCreate());
      root.append(create);
      return;
    }
    root.append(node("p", "eyebrow", "Nghiên cứu đang chọn"), node("h3", "research-context-question", c.question));
    const counts = node("p", "research-context-counts", `${c.entries.length} nguồn`);
    counts.append(node("span", "research-context-roles", ` · ${Object.entries(ROLES).map(([role, title]) => `${title}: ${c.entries.filter(entry => entry.role === role).length}`).join(" · ")}`));
    root.append(counts);
    const status = node("p", `research-context-status${sourceNotice ? " is-notice" : ""}`, sourceNotice || "Vai trò là phân loại riêng của bạn, không phải kết quả xác minh.");
    status.setAttribute("role", "status"); root.append(status);
    root.append(nextAction(c.entries.length ? "Ghi chú và đối chiếu nguồn" : "Tìm nguồn cho câu hỏi", c.entries.length ? "workspace" : "bai-viet"));
  }
  function renderProgress() {
    const root = document.querySelector(".workspace-progress"), c = active();
    if (!root || !c) return;
    root.replaceChildren();
    const steps = [
      [c.entries.length > 0, `1. Nguồn (${c.entries.length})`],
      [c.entries.length > 0 && c.entries.every(entry => entry.note.trim()), "2. Ghi chú"],
      [!!c.conclusion.trim() && c.entries.length > 0 && (c.entries.some(entry => entry.role !== "support") || !!c.unknowns.trim()), "3. Kết luận"]
    ];
    for (const [complete, label] of steps) root.append(node("li", complete ? "is-complete" : "", `${complete ? "Đã có" : "Còn thiếu"}: ${label}`));
  }
  function openCreate() {
    const panel = document.querySelector("#workspace-create-panel");
    if (panel) panel.open = true;
    document.querySelector("#workspace-create [name='question']")?.focus();
  }
  function message(value, bad = false) {
    const target = document.querySelector("#workspace-message");
    target.textContent = value; target.classList.toggle("error", bad);
    const notice = document.querySelector("#action-status");
    if (notice) {
      const rect = target.getBoundingClientRect();
      const inlineVisible = rect.height > 0 && rect.top >= 72 && rect.bottom <= innerHeight - 80 && document.querySelector("#drawer-backdrop")?.hidden;
      notice.hidden = !bad || inlineVisible;
      document.querySelector("#action-status-text").textContent = value;
    }
  }
  function act(fn, success, redraw = true) {
    try {
      const dirty = hasUnsaved(), result = fn();
      sourceNotice = "";
      if (success) message(success);
      if (redraw && !dirty) render(); else { renderWatches(store.get()); renderContext(); renderProgress(); document.dispatchEvent(new CustomEvent("bti:workspace-changed")); }
      return result;
    }
    catch (err) { message(`Không lưu được: ${err.message} Hãy xuất bản sao trước khi thay đổi tiếp.`, true); return null; }
  }
  const active = () => store.get().collections.find(c => c.id === store.get().active_id);
  function editActive(fn) { return store.commit(s => { const c = s.collections.find(c => c.id === s.active_id); if (!c) throw new Error("Hãy tạo/chọn nghiên cứu trước."); fn(c); c.updated_at = now(); }); }
  function field(label, value, name) {
    const wrap = node("label", "workspace-field", label);
    const input = node("textarea"); input.value = value; input.name = name; input.maxLength = 20000; input.rows = 3;
    wrap.append(input); return wrap;
  }
  function addArticle(item, evidenceId = null) {
    const s = validateSnapshot(snapshot(item, evidenceId));
    const result = act(() => editActive(c => {
      if (c.entries.some(e => e.snapshot.post_id === s.post_id && e.snapshot.evidence_id === s.evidence_id)) throw new Error("Nguồn này đã có trong nghiên cứu.");
      c.entries.push({snapshot: s, role: "context", note: ""});
    }));
    if (!result) return false;
    const c = active();
    sourceNotice = `Đã thêm “${s.title}” vào “${c.question}” · ${c.entries.length} nguồn.`;
    message(sourceNotice); renderContext();
    return true;
  }
  function addWatch(criteria) {
    act(() => store.commit(s => {
      if (s.watchlist.some(w => w.entity === criteria.entity && w.topic === criteria.topic && w.asset === criteria.asset)) throw new Error("Đã có danh sách theo dõi với bộ lọc này.");
      s.watchlist.push({id: id(), ...criteria, created_at: now(), checked_at: null});
    }), "Đã lưu danh sách theo dõi trên trình duyệt này.", false);
  }
  function render() {
    if (!store) return;
    const state = store.get(), c = active();
    const select = document.querySelector("#workspace-select"); select.replaceChildren();
    if (!state.collections.length) { const option = node("option", "", "Chưa có nghiên cứu"); option.value = ""; select.append(option); }
    for (const item of state.collections) { const option = node("option", "", item.question); option.value = item.id; select.append(option); }
    select.value = state.active_id || "";
    const root = document.querySelector("#workspace-editor"); root.replaceChildren();
    sourceNotice = "";
    if (!c) {
      root.append(node("p", "empty", "Tạo câu hỏi, tìm bài rồi thêm nguồn vào nghiên cứu. Ghi chú riêng không gửi lên máy chủ."));
      const panel = document.querySelector("#workspace-create-panel"); if (panel) panel.open = true;
    }
    else {
      const heading = node("header", "workspace-heading");
      heading.append(node("h3", "", c.question), node("p", "sample-note", `${PURPOSES[c.purpose]} · ${c.entries.length} nguồn đã chọn · lưu riêng, không đồng bộ`), nextAction("Tìm nguồn cho câu hỏi", "bai-viet"));
      const progress = node("ol", "workspace-progress"); progress.setAttribute("aria-label", "Mức độ điền nghiên cứu, không phải xác minh khoa học");
      heading.append(progress, node("p", "sample-note", "Ba bước cho biết nội dung đã lưu còn thiếu gì; không chứng nhận kết luận đúng hay nguồn đã được xác minh."));
      root.append(heading);
      const card = node("form", "panel workspace-card workspace-conclusion"); card.id = "workspace-edit";
      card.append(node("h3", "", "Kết luận và điều chưa biết"));
      const question = field("Câu hỏi", c.question, "question"); question.querySelector("textarea").maxLength = 300; question.querySelector("textarea").required = true;
      const questionEdit = node("details", "workspace-question-edit");
      questionEdit.append(node("summary", "", "Sửa câu hỏi nghiên cứu"), question);
      question.querySelector("textarea").addEventListener("invalid", () => { questionEdit.open = true; });
      card.append(questionEdit, field("Kết luận / tổng hợp của người nghiên cứu (không gán cho tác giả)", c.conclusion, "conclusion"), field("Khoảng trống, phản biện và điều cần kiểm chứng", c.unknowns, "unknowns"));
      const purposeLabel = node("label", "workspace-field", "Mục đích nghiên cứu");
      const purpose = node("select"); purpose.name = "purpose";
      for (const [value, title] of Object.entries(PURPOSES)) { const option = node("option", "", title); option.value = value; purpose.append(option); }
      purpose.value = c.purpose; purposeLabel.append(purpose); card.append(purposeLabel);
      const planPanel = node("section", "purpose-plan"), planTitle = node("h3");
      planPanel.append(planTitle, node("p", "field-hint", PLAN_NOTICE), node("p", "field-hint", "Có thể lưu khi chưa hoàn tất. Trường bắt buộc chỉ áp dụng khi xuất đúng mục đích; đổi mục đích vẫn giữ các đề xuất đã viết, nhưng chỉ xuất phần đang chọn. Tối đa 4.000 ký tự mỗi trường."));
      const planFields = node("div", "plan-fields"), planControls = new Map();
      for (const [key, label] of Object.entries(PLAN_FIELDS)) {
        const wrap = field(label, c.plan[key], `plan_${key}`);
        wrap.querySelector("textarea").maxLength = 4000;
        const hint = node("span", "field-hint"); wrap.append(hint);
        planControls.set(key, {wrap, hint}); planFields.append(wrap);
      }
      const orderPlanFields = () => {
        for (const key of PLAN_PURPOSES[purpose.value].fields) planFields.append(planControls.get(key).wrap);
      };
      orderPlanFields(); purpose.addEventListener("change", orderPlanFields);
      const readiness = node("p", "purpose-readiness"); readiness.setAttribute("role", "status"); readiness.setAttribute("aria-live", "polite");
      planPanel.append(planFields, readiness); card.append(planPanel);
      const updatePlan = () => {
        const config = PLAN_PURPOSES[purpose.value];
        planTitle.textContent = config.title;
        for (const [key, {wrap, hint}] of planControls) {
          wrap.hidden = !config.fields.includes(key);
          hint.textContent = config.required ? "Cần điền để xuất; không bắt buộc khi lưu." : "Tùy chọn; không chặn xuất nội dung.";
        }
        const draft = {...active(), purpose: purpose.value, conclusion: card.elements.conclusion.value, unknowns: card.elements.unknowns.value,
          plan: Object.fromEntries(Object.keys(PLAN_FIELDS).map(key => [key, card.elements[`plan_${key}`].value]))};
        try {
          const missing = missingPlanFields(draft);
          if (missing.length) {
            readiness.textContent = `Kế hoạch còn thiếu để xuất: ${missing.join(", ")}. Có thể lưu ngay; xuất vẫn cần nguồn, kết luận và phản bác/bối cảnh hoặc khoảng trống kiểm chứng.`;
            return;
          }
          makeDraft(draft);
          readiness.textContent = "Đủ trường để chuẩn bị bản nháp. Phải lưu thay đổi và đối chiếu quyền công khai/phiên bản nguồn trước khi tải; đây không phải xác minh kết luận.";
        } catch (err) { readiness.textContent = err.message; }
      };
      card.addEventListener("input", updatePlan); card.addEventListener("change", updatePlan);
      const save = node("button", "hero-btn primary", "Lưu câu hỏi, kết luận và kế hoạch"); save.type = "submit"; card.append(save);
      card.addEventListener("submit", event => {
        event.preventDefault(); const values = new FormData(card);
        const saved = act(() => editActive(x => {
          x.question = String(values.get("question")).trim(); x.conclusion = String(values.get("conclusion")); x.unknowns = String(values.get("unknowns"));
          x.purpose = String(values.get("purpose"));
          x.plan = Object.fromEntries(Object.keys(PLAN_FIELDS).map(key => [key, String(values.get(`plan_${key}`))]));
        }), "Đã lưu kết luận và kế hoạch riêng tư; kế hoạch chưa hoàn tất vẫn được giữ.", false);
        if (saved) {
          heading.querySelector("h3").textContent = active().question;
          card.elements.question.value = active().question;
          document.querySelector("#workspace-select").selectedOptions[0].textContent = active().question;
          heading.querySelector(".sample-note").textContent = `${PURPOSES[active().purpose]} · ${active().entries.length} nguồn đã chọn · lưu riêng, không đồng bộ`;
          updatePlan();
        }
      });
      const list = node("div", "workspace-entry-list");
      list.append(node("h3", "", "Nguồn, vai trò và ghi chú"));
      if (!c.entries.length) list.append(node("p", "empty", "Chưa chọn nguồn. Mở mục Tìm bài, đọc nguồn và chọn thêm vào nghiên cứu này."));
      c.entries.forEach((entry, index) => {
        const s = entry.snapshot, row = node("form", "panel workspace-card workspace-entry"); row.dataset.postId = s.post_id;
        row.append(node("h4", "", s.title), node("p", "sample-note", `${s.author} · v${s.version} · ${s.verification} · ${s.evidence_id === null ? "Bài viết" : `Evidence #${s.evidence_id}`}`), node("blockquote", "", s.quote));
        const label = node("label", "workspace-field", "Vai trò bằng chứng (không phải kết quả đã xác minh)");
        const role = node("select"); role.name = "role";
        for (const [value, title] of Object.entries(ROLES)) { const opt = node("option", "", title); opt.value = value; role.append(opt); }
        role.value = entry.role; label.append(role);
        row.append(label, field("Ghi chú / diễn giải riêng, tách biệt lời nguồn", entry.note, "note"));
        const actions = node("div", "workspace-toolbar");
        const saveEntry = node("button", "inspect-btn", "Lưu vai trò và ghi chú"); saveEntry.type = "submit";
        actions.append(saveEntry, button("Mở bài & kiểm tra nguồn", () => api.openArticle(s.post_id)), button("Cập nhật nguồn hiện tại", async () => {
          if (hasUnsaved()) { message("Hãy lưu ghi chú trước khi cập nhật nguồn.", true); return; }
          try {
            const before = JSON.stringify(active());
            const live = await api.getJSON(`/api/articles/${encodeURIComponent(s.post_id)}`);
            assertUnchanged(before, active(), hasUnsaved());
            act(() => editActive(x => {
              const entry = x.entries.find(e => e.snapshot.post_id === s.post_id && e.snapshot.evidence_id === s.evidence_id);
              if (!entry) throw new Error("Nguồn đã được bỏ khỏi nghiên cứu.");
              entry.snapshot = validateSnapshot(snapshot(live, s.evidence_id));
            }), "Đã cập nhật trích dẫn sang phiên bản đang công khai; ghi chú riêng được giữ nguyên.");
          }
          catch (err) { message(`Không cập nhật được nguồn: ${err.message}`, true); }
        }), button("Bỏ khỏi nghiên cứu", () => {
          if (hasUnsaved()) { message("Hãy lưu ghi chú trước khi bỏ nguồn.", true); return; }
          if (confirm("Bỏ nguồn và ghi chú này khỏi nghiên cứu?")) act(() => editActive(x => x.entries.splice(index, 1)), "Đã bỏ nguồn đã chọn.");
        }, "reset-filter-btn"));
        row.append(actions);
        row.addEventListener("submit", event => { event.preventDefault(); const values = new FormData(row); act(() => editActive(x => { x.entries[index].role = String(values.get("role")); x.entries[index].note = String(values.get("note")); }), "Đã lưu ghi chú riêng.", false); updatePlan(); });
        list.append(row);
      });
      root.append(list, card);
      const actions = node("div", "workspace-toolbar");
      updatePlan();
      actions.append(button("Kiểm tra nguồn & chuẩn bị Markdown", async event => {
        const b = event.currentTarget; b.disabled = true; message("Đang đối chiếu phiên bản và quyền công khai của từng nguồn…");
        for (const old of document.querySelectorAll("#workspace-downloads a[download$='.md']")) {
          URL.revokeObjectURL(old.href); old.remove();
        }
        try {
          if (hasUnsaved()) throw new Error("Còn nội dung chưa lưu. Hãy lưu kết luận/ghi chú trước khi xuất.");
          const current = active(); makeDraft(current);
          const before = JSON.stringify(current);
          const validated = await revalidate(current, api.getJSON);
          assertUnchanged(before, active(), hasUnsaved());
          download(`bti-research-${current.id}.md`, makeDraft(validated), "text/markdown;charset=utf-8");
          message("Đã đối chiếu nguồn. Bấm liên kết Tải .md để lưu bản nháp; không có nội dung nào được tự đăng.");
        } catch (err) { message(err.message, true); }
        finally { b.disabled = false; }
      }, "hero-btn primary"), button("Xóa nghiên cứu", () => {
        if (hasUnsaved()) { message("Hãy lưu thay đổi trước khi xóa nghiên cứu.", true); return; }
        if (confirm("Xóa nghiên cứu và các ghi chú riêng? Hãy xuất bản sao nếu cần giữ lại.")) act(() => store.commit(s => { s.collections = s.collections.filter(x => x.id !== c.id); s.active_id = s.collections[0]?.id || null; }), "Đã xóa nghiên cứu đã chọn.");
      }, "reset-filter-btn"));
      root.append(node("p", "field-hint", "Xuất chỉ dùng đề xuất bạn đã lưu, không suy ra quy tắc từ nguồn. " + PLAN_NOTICE), actions);
    }
    renderWatches(state);
    renderContext(); renderProgress();
    document.dispatchEvent(new CustomEvent("bti:workspace-changed"));
  }
  function hasUnsaved() {
    const c = active(); if (!c) return false;
    const form = document.querySelector("#workspace-edit");
    if (form && ["question", "conclusion", "unknowns"].some(k => form.elements[k].value !== c[k])) return true;
    if (form && (form.elements.purpose.value !== c.purpose || Object.keys(PLAN_FIELDS).some(key => form.elements[`plan_${key}`].value !== c.plan[key]))) return true;
    return [...document.querySelectorAll(".workspace-entry")].some((f, i) => f.elements.note.value !== c.entries[i]?.note || f.elements.role.value !== c.entries[i]?.role);
  }
  function renderWatches(state) {
    const select = document.querySelector("#new-watch"), selected = select.value;
    select.replaceChildren(); const all = node("option", "", "Toàn kho công khai"); all.value = ""; select.append(all);
    const list = document.querySelector("#watchlist-items"); list.replaceChildren();
    for (const watch of state.watchlist) {
      const option = node("option", "", watch.label); option.value = watch.id; select.append(option);
      const wrap = node("span", "watch-chip");
      wrap.append(button(watch.label, () => { select.value = watch.id; api.loadNew(); }), button("Bỏ theo dõi", () => act(() => store.commit(s => { s.watchlist = s.watchlist.filter(w => w.id !== watch.id); }), "Đã bỏ danh sách theo dõi.", false), "reset-filter-btn"));
      list.append(wrap);
    }
    select.value = state.watchlist.some(w => w.id === selected) ? selected : "";
    if (select.value !== selected) select.dispatchEvent(new Event("change", {bubbles: true}));
  }
  function start(callbacks) {
    api = callbacks;
    document.querySelector("#action-status-dismiss")?.addEventListener("click", () => { document.querySelector("#action-status").hidden = true; });
    // Private forms must never fall back to native GET, even if storage is denied/corrupt.
    for (const selector of ["#workspace-create", "#watch-create"]) {
      document.querySelector(selector).addEventListener("submit", event => event.preventDefault());
    }
    try { store = createStore(localStorage); }
    catch (err) {
      message(`Không mở được kho riêng: ${err.message} Dữ liệu cũ chưa bị ghi đè. Hãy sao lưu dữ liệu trình duyệt trước khi khôi phục.`, true);
      document.querySelectorAll("#workspace-create input, #workspace-create select, #workspace-create button, #watch-create input, #watch-create select, #watch-create button, #workspace-select, #workspace-import, #workspace-backup").forEach(control => { control.disabled = true; });
      return;
    }
    render();
    const createPanel = document.querySelector("#workspace-create-panel");
    if (createPanel) createPanel.open = !active();
    if (!active()) openCreate();
    createPanel?.addEventListener("toggle", () => {
      if (createPanel.open) document.querySelector("#workspace-create [name='question']")?.focus({preventScroll: true});
    });
    document.querySelector("#workspace-create").addEventListener("submit", event => {
      event.preventDefault(); if (hasUnsaved()) { message("Hãy lưu ghi chú và kết luận trước khi tạo nghiên cứu khác.", true); return; }
      const values = new FormData(event.currentTarget), question = String(values.get("question")).trim();
      if (act(() => store.commit(s => { const key = id(); s.collections.push({id: key, question, purpose: String(values.get("purpose")), plan: emptyPlan(), created_at: now(), updated_at: now(), conclusion: "", unknowns: "", entries: []}); s.active_id = key; }), "Đã tạo nghiên cứu. Chọn ‘Tìm nguồn cho câu hỏi’ để bắt đầu.")) {
        event.target.reset();
        if (createPanel) createPanel.open = false;
        document.querySelector("#workspace-editor .research-next-action")?.focus();
      }
    });
    document.querySelector("#workspace-select").addEventListener("change", event => {
      if (hasUnsaved()) { event.target.value = store.get().active_id; message("Hãy lưu ghi chú và kết luận trước khi chuyển nghiên cứu.", true); return; }
      if (!act(() => store.commit(s => { s.active_id = event.target.value; }))) event.target.value = store.get().active_id;
    });
    document.querySelector("#workspace-backup").addEventListener("click", () => {
      if (hasUnsaved()) { message("Hãy lưu các thay đổi trước khi xuất bản sao.", true); return; }
      download("bti-private-workspace.json", encodeBackup(store.get()), "application/json"); message("Đã chuẩn bị bản sao có ghi chú riêng. Bấm liên kết Tải JSON để lưu; không chia sẻ file nếu có thông tin riêng tư.");
    });
    document.querySelector("#workspace-import").addEventListener("change", async event => {
      const file = event.target.files[0]; if (!file) return;
      try {
        if (hasUnsaved()) throw new Error("Hãy lưu thay đổi trước khi nhập.");
        if (file.size > MAX_WORKSPACE_BYTES) throw new Error("File vượt 2 MiB + 32 KiB.");
        const beforeRead = JSON.stringify(store.get());
        const raw = JSON.parse(await file.text()); validateBackup(raw);
        assertUnchanged(beforeRead, store.get(), hasUnsaved());
        act(() => store.importBackup(raw), "Đã nhập thành nghiên cứu riêng, không ghi đè nghiên cứu đang có. Nguồn sẽ được đối chiếu lại trước khi xuất bản nháp.");
      } catch (err) { message(`Không nhập được: ${err.message} Dữ liệu hiện tại được giữ nguyên.`, true); }
      finally { event.target.value = ""; }
    });
    document.querySelector("#watch-create").addEventListener("submit", event => {
      event.preventDefault(); const values = new FormData(event.target);
      addWatch({label: String(values.get("label")).trim(), entity: "", topic: String(values.get("topic")), asset: String(values.get("asset")).trim().toUpperCase()});
    });
    window.addEventListener("beforeunload", event => { if (hasUnsaved()) { event.preventDefault(); event.returnValue = ""; } });
  }
  globalThis.BTIWorkspace = {
    start, createStore, validateBackup, makeDraft, revalidate, snapshot, mergeBookmarks, missingPlanFields, assertUnchanged,
    activeId: () => store?.get().active_id || null,
    addArticle(item, evidenceId = null) {
      if (!store || !active()) { message("Hãy tạo/chọn một câu hỏi trước khi thêm nguồn.", true); navigate("workspace"); if (store) openCreate(); return false; }
      if (hasUnsaved()) { message("Hãy lưu thay đổi trong nghiên cứu trước khi thêm nguồn mới.", true); return false; }
      try { return addArticle(item, evidenceId); } catch (err) { message(err.message, true); return false; }
    },
    watchAuthor(entity, label) { if (store) addWatch({label: label.slice(0, 120), entity, topic: "", asset: ""}); },
    watches: () => store ? store.get().watchlist : [],
    markRead(watchId, time) { if (store) act(() => store.commit(s => { const w = s.watchlist.find(x => x.id === watchId); if (w) w.checked_at = date(time); }), "Đã lưu mốc kiểm tra; không thay ngày xuất bản gốc.", false); }
  };
})();
