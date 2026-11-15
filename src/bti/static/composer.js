"use strict";

// Private draft content never leaves this browser; network reads fetch public source records only.
(() => {
  const $ = selector => document.querySelector(selector);
  const node = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const action = (label, fn, cls = "inspect-btn") => {
    const b = node("button", cls, label); b.type = "button"; b.addEventListener("click", fn); return b;
  };
  const when = value => value ? new Date(value).toLocaleString("vi-VN") : "Chưa biết";
  let store = null, api, pending = false, loadError = "", searchSeq = 0, sourceSeq = 0, busy = false;
  const current = () => store ? BTIDrafts.active(store.get()) : null;
  const saveFields = () => ({
    title: $("#composer-title").value,
    body: $("#composer-body").value,
    content_type: Number($("#composer-type").value),
    notes: $("#composer-notes").value
  });
  function message(text = "", bad = false) {
    const p = $("#composer-message"); p.textContent = text; p.classList.toggle("error", bad);
  }
  function clearDownloads() {
    for (const a of $("#composer-downloads").querySelectorAll("a")) { URL.revokeObjectURL(a.href); a.remove(); }
  }
  function download(name, content, type = "text/plain;charset=utf-8") {
    const url = URL.createObjectURL(new Blob([content], {type}));
    const a = node("a", "inspect-btn", `Tải ${name}`); a.href = url; a.download = name;
    $("#composer-downloads").append(a); a.click();
  }
  function recoveryControls(show) {
    $("#composer-recovery").hidden = !show;
    $("#composer-refresh").hidden = !show;
  }
  function recover() {
    const d = current();
    const payload = d ? {notice: "Bản khôi phục riêng tư, chưa đối chiếu nguồn; có thể chứa ghi chú cá nhân.", draft: {...d, ...(pending ? saveFields() : {})}}
      : {notice: "Dữ liệu kho nháp riêng tư chưa xác thực.", raw: localStorage.getItem(BTIDrafts.KEY)};
    download("freqpilot-private-recovery.json", JSON.stringify(payload, null, 2), "application/json;charset=utf-8");
  }
  function mutate(fn) {
    if (!store) { message(loadError || "Kho nháp chưa khả dụng; không ghi đè dữ liệu cũ.", true); return false; }
    try {
      store.commit(fn);
      loadError = "";
      return true;
    } catch (err) {
      message(`${err.message} Nội dung đang gõ vẫn giữ nguyên. Hãy tải bản khôi phục trước khi nạp lại.`, true);
      $("#composer-save-state").textContent = "Chưa lưu được trên thiết bị";
      recoveryControls(true);
      return false;
    }
  }
  function selectPane(pane) {
    $("#composer-layout").dataset.pane = pane;
    for (const b of document.querySelectorAll("[data-composer-pane]")) {
      const selected = b.dataset.composerPane === pane;
      b.setAttribute("aria-pressed", String(selected)); b.classList.toggle("active", selected);
    }
  }
  function candidate() { const d = current(); return d ? {...d, ...saveFields()} : null; }
  function renderPreview() {
    const d = candidate();
    $("#composer-preview-title").hidden = true;
    $("#composer-preview-body").textContent = d ? BTIDrafts.renderText(d) : "Tạo bài mới hoặc chọn bản nháp để bắt đầu.";
    const report = d ? BTIDrafts.checkDraft(d) : null;
    $("#composer-count").textContent = report ? `${report.characters.toLocaleString("vi-VN")} / ${report.limit.toLocaleString("vi-VN")} ký tự, gồm nguồn và lưu ý` : "";
    $("#composer-checks").textContent = report ? report.ok ? "Đủ trường để xuất. Quyền công khai và phiên bản nguồn sẽ được đối chiếu lại; không xác minh nhận định của bạn." : report.issues.join(" · ") : "";
    for (const id of ["composer-copy", "composer-export"]) $("#" + id).disabled = !report?.ok || pending || busy;
    $("#composer-save").disabled = !d || busy;
  }
  function save() {
    if (!current()) return false;
    const id = current().id, fields = saveFields();
    clearDownloads();
    if (!mutate(s => {
      const d = s.drafts.find(x => x.id === id);
      if (!d) throw new Error("Bản nháp đã bị xóa ở nơi khác.");
      Object.assign(d, fields, {updated_at: new Date().toISOString()});
    })) { pending = true; renderPreview(); return false; }
    pending = false; recoveryControls(false);
    $("#composer-save-state").textContent = "Đã lưu trên thiết bị này";
    message(); renderPreview(); renderLibrary(); renderRecent();
    return true;
  }
  function renderSources() {
    const root = $("#composer-sources"); root.replaceChildren();
    const d = current(); if (!d) return;
    if (!d.sources.length) root.append(node("p", "empty", "Chưa chọn nguồn. Tìm bài bên dưới hoặc mở Kho nguồn và chọn Tạo bài từ nguồn này."));
    for (const s of d.sources) {
      const card = node("article", "brief-source-card");
      card.append(node("h3", "", s.title), node("p", "", `${s.author} · Xuất bản gốc: ${when(s.published_at)} · v${s.version}`), node("p", "", s.quote));
      const controls = node("div", "article-actions");
      controls.append(action("Đọc nguồn", () => api.openArticle(s.post_id)), action("Cập nhật nguồn", () => addSource(s.post_id, true)), action("Bỏ nguồn", () => {
        if (pending) { message("Hãy lưu hoặc tải bản khôi phục trước khi bỏ nguồn.", true); return; }
        if (mutate(state => {
          const active = BTIDrafts.active(state);
          active.sources = active.sources.filter(x => !(x.post_id === s.post_id && x.evidence_id === s.evidence_id));
          active.updated_at = new Date().toISOString();
        })) { clearDownloads(); renderSources(); renderPreview(); }
      }));
      card.append(controls); root.append(card);
    }
  }
  function renderEditor() {
    const d = current();
    for (const [selector, value] of [["#composer-title", d?.title || ""], ["#composer-body", d?.body || ""], ["#composer-type", String(d?.content_type || 1)], ["#composer-notes", d?.notes || ""]]) {
      const control = $(selector); control.value = value; control.disabled = !d;
    }
    $("#composer-save-state").textContent = d ? pending ? "Có nội dung chưa lưu" : "Đã lưu trên thiết bị này" : "Chưa chọn bản nháp";
    renderSources(); renderPreview(); recoveryControls(pending || Boolean(loadError));
  }
  function ensureSafeSwitch() {
    if (!pending) return true;
    message("Còn nội dung chưa lưu. Hãy tải bản khôi phục trước khi đổi bài.", true);
    api.navigate("composer", {focus:true}); recoveryControls(true); return false;
  }
  function openDraft(id) {
    if (!ensureSafeSwitch()) return;
    if (!mutate(s => { if (!s.drafts.some(d => d.id === id)) throw new Error("Bản nháp không còn tồn tại."); s.active_id = id; })) return;
    message(); renderEditor(); selectPane("editor"); api.clearReader(); api.navigate("composer", {focus:true});
  }
  function create(fields = {}) {
    if (!ensureSafeSwitch()) return false;
    const d = BTIDrafts.newDraft(fields);
    if (!mutate(s => { s.drafts.unshift(d); s.active_id = d.id; })) { api.clearReader(); api.navigate("composer", {focus:true}); return false; }
    pending = false; message(); clearDownloads(); renderEditor(); renderLibrary(); renderRecent(); selectPane("editor");
    api.clearReader(); api.navigate("composer", {focus:true});
    return true;
  }
  async function fromArticle(postId) {
    if (!ensureSafeSwitch()) return;
    const seq = ++sourceSeq;
    const before = JSON.stringify(current());
    try {
      const item = await api.getJSON(`/api/articles/${encodeURIComponent(postId)}`);
      if (seq !== sourceSeq || pending) return;
      assertCurrent(before);
      if (!create({title: item.title.slice(0,120), sources: [BTIWorkspace.snapshot(item)]})) return;
      message("Đã chọn nguồn. Viết nhận định riêng của bạn; nguồn không tự trở thành kết luận.");
    } catch (err) { message(`Không mở được nguồn: ${err.message}`, true); api.navigate("composer"); }
  }
  function assertCurrent(before) {
    if (pending || JSON.stringify(current()) !== before) throw new Error("Bản nháp đã thay đổi trong lúc đối chiếu. Nội dung của bạn được giữ nguyên; thử lại với bản mới nhất.");
    // Include another tab's revision, even if its storage event has not been dispatched yet.
    const loaded = BTIDrafts.createStore(localStorage);
    if (JSON.stringify(loaded.get()) !== JSON.stringify(store.get())) throw new Error("Kho nháp đã đổi ở tab khác. Tải bản khôi phục rồi nạp lại.");
  }
  async function addSource(postId, replace = false) {
    if (!current()) { await fromArticle(postId); return; }
    if (pending) { message("Hãy lưu hoặc khôi phục nội dung đang gõ trước khi đổi nguồn.", true); return; }
    const before = JSON.stringify(current());
    try {
      const item = await api.getJSON(`/api/articles/${encodeURIComponent(postId)}`);
      assertCurrent(before);
      const fresh = BTIWorkspace.snapshot(item);
      if (!mutate(s => {
        const d = BTIDrafts.active(s), index = d.sources.findIndex(x => x.post_id === fresh.post_id && x.evidence_id === fresh.evidence_id);
        if (index < 0) d.sources.push(fresh); else if (replace) d.sources[index] = fresh;
        d.updated_at = new Date().toISOString();
      })) return;
      clearDownloads(); renderSources(); renderPreview();
      message(replace ? "Đã cập nhật phiên bản nguồn; nội dung bài của bạn không bị sửa. Hãy đối chiếu lại các nhận định." : "Đã thêm nguồn vào bài đang soạn.");
    } catch (err) { message(`Không thay đổi nguồn: ${err.message}`, true); }
  }
  async function searchSources() {
    const seq = ++searchSeq, q = $("#composer-source-search").value.trim();
    const root = $("#composer-source-results"); root.replaceChildren(node("p", "sample-note", "Đang tìm nguồn công khai…"));
    try {
      const query = new URLSearchParams({q, limit:"5", sort:"latest"});
      const data = await api.getJSON(`/api/articles?${query}`);
      if (seq !== searchSeq) return;
      root.replaceChildren();
      if (!data.items.length) root.append(node("p", "empty", "Chưa có nguồn khớp. Thử từ khóa khác hoặc mở Kho nguồn."));
      for (const item of data.items) {
        const card = node("article", "brief-source-card");
        card.append(node("h3", "", item.title), node("p", "", `${item.author} · Ngày gốc: ${when(item.published_at)}`), action("Thêm nguồn vào bài", () => addSource(item.post_id)));
        root.append(card);
      }
    } catch (err) {
      if (seq === searchSeq) root.replaceChildren(node("p", "empty", `Chưa tìm được nguồn: ${err.message}`), action("Thử lại", searchSources));
    }
  }
  async function fromSuggestion(fields, postIds, stillCurrent = () => true) {
    if (!ensureSafeSwitch()) return;
    const before = JSON.stringify(current());
    try {
      const sources = [];
      for (const id of [...new Set(postIds)].slice(0,20)) {
        const item = await api.getJSON(`/api/articles/${encodeURIComponent(id)}`);
        sources.push(BTIWorkspace.snapshot(item));
      }
      if (!stillCurrent()) throw new Error("Bộ lọc hoặc gợi ý đã đổi; chọn lại gợi ý muốn soạn.");
      assertCurrent(before);
      if (create({...fields,sources})) message("Đã tạo bản nháp từ gợi ý. Có thể sửa trực tiếp; đổi bộ lọc sau đó không ghi đè bài này.");
    } catch (err) { message(`Chưa tạo được bài từ gợi ý: ${err.message}`, true); api.navigate("composer"); }
  }
  async function output(mode) {
    if (!current() || pending || busy) return;
    const before = JSON.stringify(current()), d = current();
    busy = true; renderPreview(); message("Đang đối chiếu phiên bản và quyền công khai của các nguồn…"); clearDownloads();
    try {
      const report = BTIDrafts.checkDraft(d);
      if (!report.ok) throw new Error(report.issues.join(" · "));
      const checked = await BTIWorkspace.revalidate({entries:d.sources.map(snapshot => ({snapshot}))}, api.getJSON);
      assertCurrent(before);
      d.sources = checked.entries.map(e => e.snapshot);
      const refreshed = BTIDrafts.checkDraft(d);
      if (!refreshed.ok) throw new Error(refreshed.issues.join(" · "));
      const text = BTIDrafts.renderText(d);
      if (mode === "copy") {
        try { await navigator.clipboard.writeText(text); message("Đã sao chép bài và dẫn nguồn; ghi chú riêng không được sao chép. Không tự đăng bài."); }
        catch (_err) { assertCurrent(before); download(`square-${d.id}.txt`, text); message("Clipboard không khả dụng. Đã chuẩn bị file bài viết để tải."); }
      } else {
        download(`square-${d.id}.txt`, text); message("Đã xuất bài và dẫn nguồn sau khi đối chiếu. Ghi chú riêng không có trong file bài viết.");
      }
    } catch (err) { message(err.message.startsWith("Chưa thể xuất:") ? err.message : `Chưa thể xuất: ${err.message}`, true); }
    finally { busy = false; renderPreview(); }
  }
  function emptyStorageMessage(root) {
    root.append(node("p", "empty", `${loadError} Kho cũ không bị xóa hoặc ghi đè.`), action("Tải dữ liệu để khôi phục", () => { api.navigate("composer"); recover(); }), action("Nạp lại kho", reload));
  }
  function renderLibrary() {
    const root = $("#draft-library"); root.replaceChildren();
    if (!store) { emptyStorageMessage(root); return; }
    const q = ($("#draft-search").value || "").trim().toLocaleLowerCase("vi");
    const drafts = store.get().drafts.filter(d => !q || `${d.title} ${d.body}`.toLocaleLowerCase("vi").includes(q));
    if (!drafts.length) {
      root.append(node("p", "empty", q ? "Chưa có bài khớp từ khóa. Xóa tìm kiếm để xem lại các bản nháp." : "Bạn chưa có bản nháp. Tạo bài mới hoặc chọn nguồn để bắt đầu."));
      return;
    }
    for (const d of drafts) {
      const card = node("article", "panel draft-card");
      card.append(node("h2", "", d.title.trim() || "Bài chưa đặt tiêu đề"), node("p", "", `${d.content_type === 2 ? "Bài dài" : "Bài ngắn"} · ${d.sources.length} nguồn · Sửa lần cuối: ${when(d.updated_at)}`), node("p", "", d.body.slice(0,160) || "Chưa viết nội dung."));
      const controls = node("div", "article-actions");
      controls.append(action("Viết tiếp", () => openDraft(d.id), "hero-btn primary"), action("Xóa bản nháp", () => {
        if (!ensureSafeSwitch() || !confirm("Xóa riêng bản nháp này trên thiết bị? Hãy sao lưu nếu cần giữ lại.")) return;
        if (mutate(s => { s.drafts = s.drafts.filter(x => x.id !== d.id); if (s.active_id === d.id) s.active_id = s.drafts[0]?.id || null; })) { clearDownloads(); renderLibrary(); renderRecent(); renderEditor(); }
      }, "reset-filter-btn"));
      card.append(controls); root.append(card);
    }
  }
  function renderRecent() {
    const root = $("#today-drafts"); root.replaceChildren();
    if (!store) { emptyStorageMessage(root); return; }
    const recent = store.get().drafts.slice().sort((a,b)=>b.updated_at.localeCompare(a.updated_at)).slice(0,3);
    if (!recent.length) { root.append(node("p", "empty", "Chưa có bài đang viết. Bắt đầu từ một nguồn hoặc tạo bài trống.")); return; }
    for (const d of recent) {
      const card = node("article", "panel draft-card");
      card.append(node("h3", "", d.title.trim() || "Bài chưa đặt tiêu đề"), node("p", "sample-note", `Lưu trên thiết bị · ${when(d.updated_at)}`), action("Viết tiếp", () => openDraft(d.id)));
      root.append(card);
    }
  }
  function reload() {
    if (pending && !confirm("Nạp bản đã lưu sẽ thay nội dung đang gõ. Bạn đã tải bản khôi phục chưa?")) return;
    try { const next = BTIDrafts.createStore(localStorage); store = next; pending = false; loadError = ""; clearDownloads(); message(); renderLibrary(); renderRecent(); renderEditor(); }
    catch (err) { loadError = err.message; message(loadError, true); recoveryControls(true); }
  }
  function show(page) {
    if (!api) return;
    if (page === "home") renderRecent();
    if (page === "drafts") renderLibrary();
    if (page === "writing" && !pending) renderEditor();
  }
  function start(callbacks) {
    api = callbacks;
    try { store = BTIDrafts.createStore(localStorage); }
    catch (err) { loadError = `Không đọc được kho bản nháp: ${err.message}`; }
    document.querySelectorAll("[data-create-draft]").forEach(b => b.addEventListener("click", () => create()));
    for (const selector of ["#composer-title", "#composer-body", "#composer-notes"]) $(selector).addEventListener("input", () => { pending = true; save(); });
    $("#composer-type").addEventListener("change", () => { pending = true; save(); });
    $("#composer-save").addEventListener("click", save);
    $("#composer-copy").addEventListener("click", () => output("copy"));
    $("#composer-export").addEventListener("click", () => output("file"));
    $("#composer-recovery").addEventListener("click", recover);
    $("#composer-refresh").addEventListener("click", reload);
    $("#composer-source-find").addEventListener("click", searchSources);
    $("#composer-source-search").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); searchSources(); } });
    $("#draft-search").addEventListener("input", renderLibrary);
    document.querySelectorAll("[data-composer-pane]").forEach(b => b.addEventListener("click", () => selectPane(b.dataset.composerPane)));
    $("#draft-backup").addEventListener("click", () => {
      if (!store || !ensureSafeSwitch()) return;
      api.navigate("composer"); download("freqpilot-private-drafts.json", BTIDrafts.encodeBackup(store.get()), "application/json;charset=utf-8");
      message("Đây là bản sao riêng tư có cả ghi chú; không dùng file này để đăng bài hoặc chia sẻ công khai.");
    });
    $("#draft-import").addEventListener("change", async e => {
      const file = e.target.files[0]; if (!file || !ensureSafeSwitch()) { e.target.value = ""; return; }
      const before = store ? JSON.stringify(store.get()) : null;
      try {
        if (!store) throw new Error(loadError);
        if (file.size > 2 * 1024 * 1024) throw new Error("Bản sao vượt 2 MiB.");
        const raw = JSON.parse(await file.text());
        if (pending || before !== JSON.stringify(store.get())) throw new Error("Kho nháp đã đổi trong lúc đọc file.");
        store.importBackup(raw); clearDownloads(); renderLibrary(); renderRecent(); renderEditor();
        selectPane("editor"); api.clearReader(); api.navigate("composer", {focus:true});
        message("Đã nhập thành các bản nháp mới, không ghi đè bài đang có.");
      } catch (err) { api.navigate("composer"); message(`Không nhập được: ${err.message}. Dữ liệu hiện tại được giữ nguyên.`, true); }
      finally { e.target.value = ""; }
    });
    window.addEventListener("storage", e => {
      if (e.key !== BTIDrafts.KEY && e.key !== null) return;
      if (pending) { message("Kho đã đổi ở tab khác; nội dung đang gõ được giữ nguyên. Tải bản khôi phục trước khi nạp lại.", true); recoveryControls(true); return; }
      reload();
    });
    window.addEventListener("beforeunload", e => { if (pending) { e.preventDefault(); e.returnValue = ""; } });
    renderLibrary(); renderRecent(); renderEditor();
  }
  globalThis.BTIComposer = {start, show, create, fromArticle, fromSuggestion, addSource};
})();
