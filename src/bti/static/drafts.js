"use strict";

// Private drafts leave this browser only through an explicit export.
(() => {
  const KEY = "bti_square_drafts_v1";
  const MAX_BYTES = 2 * 1024 * 1024;
  const STATE_FIELDS = ["version", "active_id", "drafts"];
  const LEGACY_DRAFT_FIELDS = ["id", "title", "body", "content_type", "notes", "sources", "created_at", "updated_at"];
  const DRAFT_FIELDS = [...LEGACY_DRAFT_FIELDS, "images"];
  const SOURCE_FIELDS = ["post_id", "evidence_id", "title", "author", "url", "source_url", "version", "checksum", "published_at", "fetched_at", "observed_at", "quote", "verification", "limitations"];
  const DISCLAIMER = "Lưu ý: Tổng hợp nghiên cứu cá nhân từ nguồn công khai, không phải khuyến nghị đầu tư hay tín hiệu giao dịch. Tương tác và giải thưởng nội dung không chứng minh lợi nhuận thực tế. Không tự xuất bản.";
  const copy = value => JSON.parse(JSON.stringify(value));
  const empty = () => ({version: 2, active_id: null, drafts: []});
  function fields(value, names) {
    if (!value || typeof value !== "object" || Array.isArray(value)
        || Object.keys(value).length !== names.length
        || names.some(name => !Object.hasOwn(value, name))) throw new Error("Dữ liệu bản nháp không đúng định dạng hoặc có trường không được hỗ trợ.");
  }
  function text(value, max) {
    if (typeof value !== "string" || value.length > max) throw new Error("Văn bản bản nháp không hợp lệ hoặc quá dài.");
    return value;
  }
  function identifier(value) {
    if (typeof value !== "string" || !/^[A-Za-z0-9_-]{1,100}$/.test(value)) throw new Error("Mã bản nháp không hợp lệ.");
    return value;
  }
  function timestamp(value) {
    if (typeof value !== "string" || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/.test(value)
        || !Number.isFinite(Date.parse(value)) || new Date(value).toISOString() !== value) throw new Error("Thời điểm bản nháp không hợp lệ.");
    return value;
  }
  function validateDraft(raw, legacy = false) {
    fields(raw, legacy ? LEGACY_DRAFT_FIELDS : DRAFT_FIELDS);
    if (![1, 2].includes(raw.content_type) || !Array.isArray(raw.sources) || raw.sources.length > 20) throw new Error("Loại bài hoặc danh sách nguồn không hợp lệ (tối đa 20 nguồn).");
    const created = timestamp(raw.created_at), updated = timestamp(raw.updated_at);
    if (updated < created) throw new Error("Ngày cập nhật không được trước ngày tạo bản nháp.");
    const sources = Array.from(raw.sources, source => {
      fields(source, SOURCE_FIELDS);
      try { return globalThis.BTIWorkspace.validateSnapshot(source); }
      catch (err) { throw new Error(`Nguồn bản nháp không hợp lệ: ${err.message}`); }
    });
    const images = legacy ? [] : raw.images;
    if (!Array.isArray(images) || images.length > 4) throw new Error("Một bản nháp hỗ trợ tối đa 4 ảnh nguồn.");
    const seen = new Set();
    const selected = Array.from(images, image => {
      fields(image, ["post_id", "url"]);
      if (!sources.some(source => source.post_id === image.post_id)
          || !globalThis.BTIImages.safeURL(image.url) || seen.has(image.url)) throw new Error("Ảnh phải là URL Binance hợp lệ, không trùng và gắn với nguồn của bài.");
      seen.add(image.url);
      return {post_id: image.post_id, url: image.url};
    });
    return {
      id: identifier(raw.id), title: text(raw.title, 120), body: text(raw.body, 15000),
      content_type: raw.content_type, notes: text(raw.notes, 4000), sources, images: selected,
      created_at: created, updated_at: updated
    };
  }
  function encoded(state) {
    const raw = JSON.stringify(state);
    if (new TextEncoder().encode(raw).byteLength > MAX_BYTES) throw new Error("Kho bản nháp vượt 2 MiB. Hãy sao lưu và bớt bản nháp trước khi lưu.");
    return raw;
  }
  function validateBackup(raw) {
    fields(raw, STATE_FIELDS);
    if (![1, 2].includes(raw.version) || !Array.isArray(raw.drafts) || raw.drafts.length > 100) throw new Error("Bản sao không đúng định dạng bản nháp v1/v2 (tối đa 100 bài).");
    const ids = new Set();
    const drafts = Array.from(raw.drafts, item => {
      const draft = validateDraft(item, raw.version === 1);
      if (ids.has(draft.id)) throw new Error("Mã bản nháp bị trùng.");
      ids.add(draft.id);
      return draft;
    });
    if (raw.active_id !== null && !ids.has(raw.active_id)) throw new Error("Bản nháp đang chọn không tồn tại.");
    const state = {version: 2, active_id: raw.active_id, drafts};
    encoded(state);
    return state;
  }
  function encodeBackup(state) { return encoded(validateBackup(state)); }
  function newDraft({title = "", body = "", content_type = 1, notes = "", sources = [], images = []} = {}) {
    const stamp = new Date().toISOString();
    return validateDraft({id: crypto.randomUUID(), title, body, content_type, notes, sources, images, created_at: stamp, updated_at: stamp});
  }
  function createStore(storage) {
    let saved;
    try { saved = storage.getItem(KEY); }
    catch (_) { throw new Error("Không thể đọc kho bản nháp trong trình duyệt. Nội dung chưa được thay đổi."); }
    let state = empty();
    if (saved !== null) {
      let parsed;
      try { parsed = JSON.parse(saved); }
      catch (_) { throw new Error("Kho bản nháp bị lỗi JSON. Hãy giữ lại dữ liệu gốc để khôi phục; không tự đặt lại kho."); }
      state = validateBackup(parsed);
    }
    const commit = mutate => {
      const next = copy(state);
      mutate(next);
      let current;
      try { current = storage.getItem(KEY); }
      catch (_) { throw new Error("Không thể đọc kho bản nháp để lưu. Nội dung chưa được lưu."); }
      if (current !== saved) throw new Error("Kho bản nháp đã đổi ở tab khác. Giữ lại nội dung chưa lưu rồi nạp bản đã lưu.");
      const valid = validateBackup(next);
      const raw = encoded(valid);
      try { storage.setItem(KEY, raw); }
      catch (_) { throw new Error("Không thể lưu bản nháp (hết dung lượng hoặc trình duyệt chặn lưu trữ). Hãy tải nội dung chưa lưu; bản đã lưu vẫn được giữ nguyên."); }
      saved = raw;
      state = valid;
      return copy(state);
    };
    return {
      get: () => copy(state), commit,
      importBackup(raw) {
        const incoming = validateBackup(raw);
        return commit(next => {
          const remap = new Map();
          for (const draft of incoming.drafts) {
            const key = crypto.randomUUID();
            remap.set(draft.id, key);
            next.drafts.push({...draft, id: key});
          }
          next.active_id = remap.get(incoming.active_id) || remap.get(incoming.drafts[0]?.id) || next.active_id;
        });
      }
    };
  }
  function active(state) { return state.drafts.find(draft => draft.id === state.active_id) || null; }
  function renderText(raw) {
    const draft = validateDraft(raw);
    const lines = [];
    if (draft.title) lines.push(draft.title, "");
    lines.push(draft.body, "", "Nguồn công khai đối chiếu (chưa xác minh độc lập):");
    const citations = new Set();
    for (const source of draft.sources) {
      // Some platform titles are excerpted first sentences. Keep the full title in the private backup.
      const words = source.title.trim().split(/\s+/);
      const title = words.length > 16 ? words.slice(0, 16).join(" ") + "…" : source.title;
      const citation = [`${source.author} — ${title}`, source.url,
        `Ngày gốc: ${source.published_at || "Chưa biết"} · Phiên bản: ${source.version}`];
      if (source.source_url !== source.url) citation.push(`Bản nguồn quan sát: ${source.source_url}`);
      citations.add(citation.join("\n"));
    }
    Array.from(citations).forEach((citation, index) => lines.push(`${index + 1}. ${citation}`));
    lines.push("", DISCLAIMER);
    return lines.join("\n");
  }
  function checkDraft(raw) {
    const draft = validateDraft(raw);
    const limit = draft.content_type === 1 ? 2200 : 15000;
    const characters = renderText(draft).length;
    const issues = [];
    if (!draft.body.trim()) issues.push("Hãy viết nội dung trước khi xuất bài.");
    if (draft.content_type === 2 && !draft.title.trim()) issues.push("Bài dài cần tiêu đề trước khi xuất.");
    if (!draft.sources.length) issues.push("Cần ít nhất một nguồn để đối chiếu trước khi xuất.");
    if (characters > limit) issues.push(`Bài xuất gồm nguồn và lưu ý có ${characters} ký tự, vượt giới hạn ${limit}.`);
    return {ok: issues.length === 0, issues, characters, limit};
  }
  function assertImageSources(raw, articles) {
    const draft = validateDraft(raw);
    for (const image of draft.images) {
      const article = articles.get(image.post_id);
      const source = draft.sources.find(s => s.post_id === image.post_id);
      if (!article || article.post_id !== image.post_id || article.version !== source.version
          || (source.checksum && article.checksum !== source.checksum)
          || !globalThis.BTIImages.urls(article).includes(image.url)) {
        throw new Error("Ảnh không còn được ghi nhận trong phiên bản nguồn công khai. Cập nhật nguồn hoặc bỏ ảnh rồi chọn lại.");
      }
    }
  }
  function makePostPackage(raw) {
    const draft = validateDraft(raw);
    return {
      schema: "bti-square-post-v1", content_type: draft.content_type, text: renderText(draft),
      images: draft.images.map(image => {
        const source = draft.sources.find(s => s.post_id === image.post_id);
        return {...image, author: source.author, content_url: source.url, source_url: source.source_url,
          version: source.version, source_checksum: source.checksum,
          fetched_at: source.fetched_at, observed_at: source.observed_at};
      })
    };
  }
  globalThis.BTIDrafts = {KEY, createStore, newDraft, validateBackup, encodeBackup, active, renderText, checkDraft, assertImageSources, makePostPackage};
})();
