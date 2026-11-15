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
  let mediaSeq = 0, mediaOwner = "", mediaSeed = [], mediaArticles = new Map(), mediaPending = new Set(), mediaErrors = new Map();
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
    const imageProblem = imageIssue(d);
    $("#composer-count").textContent = report ? `${report.characters.toLocaleString("vi-VN")} / ${report.limit.toLocaleString("vi-VN")} ký tự, gồm nguồn và lưu ý` : "";
    $("#composer-checks").textContent = report ? [...report.issues, imageProblem].filter(Boolean).join(" · ") || "Đủ trường để xuất. Nguồn và các link ảnh sẽ được đối chiếu lại; không xác minh nhận định hoặc quyền đăng lại ảnh." : "";
    for (const id of ["composer-copy", "composer-export"]) $("#" + id).disabled = !report?.ok || Boolean(imageProblem) || pending || busy;
    $("#composer-export").textContent = d?.images.length ? "Tải bài + link ảnh" : "Tải bài .txt";
    $("#composer-copy").textContent = d?.images.length ? "Sao chép chữ" : "Sao chép bài";
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
          if (!active.sources.some(source => source.post_id === s.post_id)) active.images = active.images.filter(image => image.post_id !== s.post_id);
          active.updated_at = new Date().toISOString();
        })) { clearDownloads(); renderSources(); loadImageSources(); renderPreview(); }
      }));
      card.append(controls); root.append(card);
    }
  }
  function matchesSource(article, source) {
    return article?.post_id === source.post_id && article.version === source.version
      && (!source.checksum || article.checksum === source.checksum);
  }
  function imageIssue(draft) {
    if (!draft?.images.length) return "";
    if (mediaOwner !== draft.id || draft.images.some(image => mediaPending.has(image.post_id))) return "Đang đối chiếu các ảnh với bài nguồn…";
    try { BTIDrafts.assertImageSources(draft, mediaArticles); return ""; }
    catch (err) { return err.message; }
  }
  function toggleImage(postId, url) {
    const d = current(); if (!d) return;
    if (pending || busy) { message("Hãy lưu nội dung hoặc chờ đối chiếu xong trước khi đổi ảnh.", true); return; }
    const removing = d.images.some(image => image.url === url);
    if (!removing && (mediaOwner !== d.id || !BTIImages.urls(mediaArticles.get(postId)).includes(url))) {
      message("Ảnh chưa được đối chiếu với nguồn công khai hiện tại.", true); return;
    }
    if (mutate(state => {
      const active = BTIDrafts.active(state);
      active.images = removing ? active.images.filter(image => image.url !== url) : [...active.images, {post_id: postId, url}];
      active.updated_at = new Date().toISOString();
    })) {
      clearDownloads(); message(); $("#composer-save-state").textContent = "Đã lưu trên thiết bị này";
      renderMedia(); renderLibrary(); renderRecent();
    }
  }
  function renderMedia() {
    const d = current(), selected = $("#composer-selected-images"), preview = $("#composer-preview-images");
    const approved = d && mediaOwner === d.id ? d.images.filter(image => {
      const article = mediaArticles.get(image.post_id), source = d.sources.find(s => s.post_id === image.post_id);
      return source && matchesSource(article, source) && BTIImages.urls(article).includes(image.url);
    }) : [];
    BTIImages.render(selected, approved.map(image => image.url), {alt: d?.title,
      onRemove: url => toggleImage(d.images.find(image => image.url === url).post_id, url)});
    BTIImages.render(preview, approved.map(image => image.url), {alt: d?.title});
    $("#composer-image-count").textContent = `${d?.images.length || 0} / 4`;
    for (const image of d?.images || []) {
      if (approved.some(allowed => allowed.url === image.url)) continue;
      const box = node("div", "image-unavailable");
      box.append(node("p", "", mediaPending.has(image.post_id) ? "Đang kiểm tra quyền công khai và phiên bản ảnh…" : "Ảnh đã chọn chưa khả dụng trong nguồn hiện tại. Không tải ảnh cũ từ bộ nhớ nháp."),
        action("Bỏ ảnh này", () => toggleImage(image.post_id, image.url)));
      selected.append(box);
      preview.append(node("p", "image-unavailable", "Một ảnh đã chọn chưa đối chiếu được. Cập nhật nguồn hoặc bỏ ảnh trong phần Soạn bài."));
    }
    if (!d?.images.length) selected.append(node("p", "sample-note", "Chưa chọn ảnh. Chọn từ các bài nguồn bên dưới."));
    const options = $("#composer-image-options"); options.replaceChildren();
    for (const source of d?.sources || []) {
      const group = node("div", "composer-image-source");
      group.append(node("p", "sample-note", `Ảnh nguồn: ${source.author}`));
      const article = mediaArticles.get(source.post_id), urls = BTIImages.urls(article);
      if (urls.length && matchesSource(article, source)) {
        const gallery = node("div");
        BTIImages.render(gallery, urls, {alt: source.title, selected: d.images.map(image => image.url),
          onSelect: url => toggleImage(source.post_id, url)});
        group.append(gallery);
      } else group.append(node("p", "sample-note", mediaPending.has(source.post_id) ? "Đang đọc link ảnh đã thu thập…" : mediaErrors.get(source.post_id) || "Bản nguồn này chưa ghi nhận link ảnh phù hợp."));
      options.append(group);
    }
    if (!d?.sources.length) options.append(node("p", "sample-note", "Thêm nguồn ở tab Nguồn để chọn ảnh của bài đó."));
    renderPreview();
  }
  async function loadImageSources(seeds = []) {
    const seq = ++mediaSeq, d = current();
    mediaOwner = d?.id || ""; mediaArticles = new Map(); mediaErrors = new Map(); mediaPending = new Set();
    if (!d || $("#composer").hidden) { renderMedia(); return; }
    const signature = JSON.stringify(d.sources);
    for (const source of d.sources) {
      const seeded = seeds.find(article => matchesSource(article, source));
      if (seeded) mediaArticles.set(source.post_id, seeded); else mediaPending.add(source.post_id);
    }
    renderMedia();
    await Promise.all(d.sources.filter(source => !mediaArticles.has(source.post_id)).map(async source => {
      let item, error;
      try {
        item = await api.getJSON(`/api/articles/${encodeURIComponent(source.post_id)}`);
        if (!matchesSource(item, source)) throw new Error("Nguồn đã đổi phiên bản. Bấm Cập nhật nguồn trước khi chọn ảnh.");
      } catch (err) { error = `Chưa dùng được ảnh nguồn: ${err.message}`; }
      if (seq !== mediaSeq || current()?.id !== d.id || JSON.stringify(current()?.sources) !== signature) return;
      mediaPending.delete(source.post_id);
      if (error) mediaErrors.set(source.post_id, error); else mediaArticles.set(source.post_id, item);
      renderMedia();
    }));
  }
  async function renderCovers(root, drafts) {
    const illustrated = drafts.filter(d => d.images.length);
    if (!illustrated.length) return;
    const ids = [...new Set(illustrated.map(d => d.images[0].post_id))];
    try {
      const data = await api.getJSON(`/api/articles?${new URLSearchParams({post_ids: ids.join(","), limit:"100", sort:"latest"})}`);
      if (!root.isConnected) return;
      const articles = new Map(data.items.map(item => [item.post_id, item]));
      for (const d of illustrated) {
        const cover = root.querySelector(`[data-draft-cover="${d.id}"]`);
        if (!cover) continue;
        const latest = store.get().drafts.find(item => item.id === d.id);
        if (!latest || JSON.stringify(latest.images) !== JSON.stringify(d.images) || JSON.stringify(latest.sources) !== JSON.stringify(d.sources)) continue;
        const image = d.images[0], item = articles.get(image.post_id), source = d.sources.find(s => s.post_id === image.post_id);
        if (item && source && item.version === source.version && BTIImages.urls(item).includes(image.url)) {
          BTIImages.render(cover, [image.url], {alt: d.title, compact:true});
        } else cover.append(node("p", "sample-note", "Mở bản nháp để đối chiếu lại ảnh nguồn."));
      }
    } catch (_err) {
      for (const cover of root.querySelectorAll("[data-draft-cover]")) if (!cover.children.length) cover.append(node("p", "sample-note", "Chưa đối chiếu được ảnh nguồn; nội dung nháp vẫn được giữ."));
    }
  }
  function renderEditor() {
    const d = current();
    for (const [selector, value] of [["#composer-title", d?.title || ""], ["#composer-body", d?.body || ""], ["#composer-type", String(d?.content_type || 1)], ["#composer-notes", d?.notes || ""]]) {
      const control = $(selector); control.value = value; control.disabled = !d;
    }
    $("#composer-save-state").textContent = d ? pending ? "Có nội dung chưa lưu" : "Đã lưu trên thiết bị này" : "Chưa chọn bản nháp";
    renderSources(); renderPreview(); recoveryControls(pending || Boolean(loadError));
    const seeds = mediaSeed; mediaSeed = []; loadImageSources(seeds);
  }
  function ensureSafeSwitch() {
    if (!pending) return true;
    message("Còn nội dung chưa lưu. Hãy tải bản khôi phục trước khi đổi bài.", true);
    api.navigate("composer", {focus:true}); recoveryControls(true); return false;
  }
  function openDraft(id) {
    if (!ensureSafeSwitch()) return;
    if (!mutate(s => { if (!s.drafts.some(d => d.id === id)) throw new Error("Bản nháp không còn tồn tại."); s.active_id = id; })) return;
    message(); selectPane("editor"); api.clearReader(); api.navigate("composer", {focus:true});
  }
  function create(fields = {}, imageSeeds = []) {
    if (!ensureSafeSwitch()) return false;
    const d = BTIDrafts.newDraft(fields);
    if (!mutate(s => { s.drafts.unshift(d); s.active_id = d.id; })) { api.clearReader(); api.navigate("composer", {focus:true}); return false; }
    pending = false; mediaSeed = imageSeeds; message(); clearDownloads(); renderLibrary(); renderRecent(); selectPane("editor");
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
      if (!create({title: item.title.slice(0,120), sources: [BTIWorkspace.snapshot(item)]}, [item])) return;
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
      clearDownloads(); renderSources(); loadImageSources([item]); renderPreview();
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
      const sources = [], articles = [];
      for (const id of [...new Set(postIds)].slice(0,20)) {
        const item = await api.getJSON(`/api/articles/${encodeURIComponent(id)}`);
        sources.push(BTIWorkspace.snapshot(item));
        articles.push(item);
      }
      if (!stillCurrent()) throw new Error("Bộ lọc hoặc gợi ý đã đổi; chọn lại gợi ý muốn soạn.");
      assertCurrent(before);
      if (create({...fields,sources}, articles)) message("Đã tạo bản nháp từ gợi ý. Có thể sửa trực tiếp; đổi bộ lọc sau đó không ghi đè bài này.");
    } catch (err) { message(`Chưa tạo được bài từ gợi ý: ${err.message}`, true); api.navigate("composer"); }
  }
  async function output(mode) {
    if (!current() || pending || busy) return;
    const before = JSON.stringify(current()), d = current();
    busy = true; renderPreview(); message("Đang đối chiếu phiên bản và quyền công khai của các nguồn…"); clearDownloads();
    try {
      const report = BTIDrafts.checkDraft(d);
      if (!report.ok) throw new Error(report.issues.join(" · "));
      const articles = new Map();
      const checked = await BTIWorkspace.revalidate({entries:d.sources.map(snapshot => ({snapshot}))}, async url => {
        const article = await api.getJSON(url); articles.set(article.post_id, article); return article;
      });
      assertCurrent(before);
      d.sources = checked.entries.map(e => e.snapshot);
      BTIDrafts.assertImageSources(d, articles);
      const refreshed = BTIDrafts.checkDraft(d);
      if (!refreshed.ok) throw new Error(refreshed.issues.join(" · "));
      const text = BTIDrafts.renderText(d);
      if (mode === "copy") {
        const notice = d.images.length ? " Ảnh không có trong clipboard; tải gói JSON để giữ link ảnh." : "";
        try { await navigator.clipboard.writeText(text); message(`Đã sao chép chữ và dẫn nguồn; không chứa ghi chú riêng.${notice} Không tự đăng bài.`); }
        catch (_err) { assertCurrent(before); download(`square-${d.id}.txt`, text); message(`Clipboard không khả dụng. Đã chuẩn bị file chữ.${notice}`); }
      } else {
        if (d.images.length) {
          download(`square-${d.id}.json`, JSON.stringify(BTIDrafts.makePostPackage(d), null, 2), "application/json;charset=utf-8");
          message("Đã xuất chữ, dẫn nguồn và link ảnh gốc trong JSON. Không chứa file ảnh hay ghi chú riêng; chưa đăng lên Square.");
        } else {
          download(`square-${d.id}.txt`, text); message("Đã xuất bài và dẫn nguồn sau khi đối chiếu. Ghi chú riêng không có trong file bài viết.");
        }
      }
    } catch (err) {
      if (d.images.length) { ++mediaSeq; mediaArticles.clear(); mediaPending.clear(); renderMedia(); }
      message(err.message.startsWith("Chưa thể xuất:") ? err.message : `Chưa thể xuất: ${err.message}`, true);
    }
    finally { busy = false; renderPreview(); }
  }
  function emptyStorageMessage(root) {
    root.append(node("p", "empty", `${loadError} Kho cũ không bị xóa hoặc ghi đè.`), action("Tải dữ liệu để khôi phục", () => { api.navigate("composer"); recover(); }), action("Nạp lại kho", reload));
  }
  function renderLibrary() {
    const root = $("#draft-library"); if ($("#my-posts").hidden) return; root.replaceChildren();
    if (!store) { emptyStorageMessage(root); return; }
    const q = ($("#draft-search").value || "").trim().toLocaleLowerCase("vi");
    const drafts = store.get().drafts.filter(d => !q || `${d.title} ${d.body}`.toLocaleLowerCase("vi").includes(q));
    if (!drafts.length) {
      root.append(node("p", "empty", q ? "Chưa có bài khớp từ khóa. Xóa tìm kiếm để xem lại các bản nháp." : "Bạn chưa có bản nháp. Tạo bài mới hoặc chọn nguồn để bắt đầu."));
      return;
    }
    for (const d of drafts) {
      const card = node("article", "panel draft-card");
      if (d.images.length) { const cover = node("div", "draft-cover"); cover.dataset.draftCover = d.id; card.append(cover); }
      card.append(node("h2", "", d.title.trim() || "Bài chưa đặt tiêu đề"), node("p", "", `${d.content_type === 2 ? "Bài dài" : "Bài ngắn"} · ${d.sources.length} nguồn · Sửa lần cuối: ${when(d.updated_at)}`), node("p", "", d.body.slice(0,160) || "Chưa viết nội dung."));
      const controls = node("div", "article-actions");
      controls.append(action("Viết tiếp", () => openDraft(d.id), "hero-btn primary"), action("Xóa bản nháp", () => {
        if (!ensureSafeSwitch() || !confirm("Xóa riêng bản nháp này trên thiết bị? Hãy sao lưu nếu cần giữ lại.")) return;
        if (mutate(s => { s.drafts = s.drafts.filter(x => x.id !== d.id); if (s.active_id === d.id) s.active_id = s.drafts[0]?.id || null; })) { clearDownloads(); renderLibrary(); renderRecent(); renderEditor(); }
      }, "reset-filter-btn"));
      card.append(controls); root.append(card);
    }
    renderCovers(root, drafts);
  }
  function renderRecent() {
    const root = $("#today-drafts"); if ($("#today").hidden) return; root.replaceChildren();
    if (!store) { emptyStorageMessage(root); return; }
    const recent = store.get().drafts.slice().sort((a,b)=>b.updated_at.localeCompare(a.updated_at)).slice(0,3);
    if (!recent.length) { root.append(node("p", "empty", "Chưa có bài đang viết. Bắt đầu từ một nguồn hoặc tạo bài trống.")); return; }
    for (const d of recent) {
      const card = node("article", "panel draft-card");
      if (d.images.length) { const cover = node("div", "draft-cover"); cover.dataset.draftCover = d.id; card.append(cover); }
      card.append(node("h3", "", d.title.trim() || "Bài chưa đặt tiêu đề"), node("p", "sample-note", `Lưu trên thiết bị · ${when(d.updated_at)}`), action("Viết tiếp", () => openDraft(d.id)));
      root.append(card);
    }
    renderCovers(root, recent);
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
