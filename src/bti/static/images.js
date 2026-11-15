"use strict";

// Original Binance image URLs only; importing this module never touches the DOM.
(() => {
  const MAX_IMAGES = 9;
  const MAX_URL_LENGTH = 2048;
  const IMAGE_PREFIX = /^(?:\/image\/|\/static\/content\/square\/images\/)/;
  const IMAGE_SUFFIX = /\.(?:jpe?g|png|webp|gif)$/i;
  const failedImages = new Set();

  function safeURL(value) {
    if (typeof value !== "string" || !value || value.length > MAX_URL_LENGTH
        || /[\s\u0000-\u001f\u007f-\u009f\\#]/u.test(value)
        || !/^https:\/\/public\.bnbstatic\.com(?::443)?\//i.test(value)) return null;
    try {
      const path = value.slice(value.indexOf("/", 8)).split("?")[0];
      if (!IMAGE_PREFIX.test(path) || !IMAGE_SUFFIX.test(path) || /%(?:2f|5c)/i.test(path)
          || path.split("/").some(part => [".", ".."].includes(part.replace(/%2e/gi, ".")))) return null;
      const url = new URL(value);
      if (url.protocol !== "https:" || url.hostname !== "public.bnbstatic.com"
          || url.port || url.username || url.password || url.hash
          || !IMAGE_PREFIX.test(url.pathname) || !IMAGE_SUFFIX.test(url.pathname)) return null;
      // Preserve the original query and spelling rather than manufacture a CDN route.
      return value;
    } catch (_) {
      return null;
    }
  }

  function urls(itemOrArray) {
    const candidates = Array.isArray(itemOrArray) ? itemOrArray : itemOrArray?.images;
    if (!Array.isArray(candidates)) return [];
    const result = [];
    const seen = new Set();
    for (const candidate of candidates) {
      const url = safeURL(candidate);
      if (!url || seen.has(url)) continue;
      seen.add(url);
      result.push(url);
      if (result.length === MAX_IMAGES) break;
    }
    return result;
  }

  function render(root, values, {alt = "", compact = false, onSelect = null, selected = [], onRemove = null} = {}) {
    root.replaceChildren();
    root.classList.add("source-image-gallery");
    root.classList.toggle("image-cover", Boolean(compact));
    root.classList.toggle("image-selectable", typeof onSelect === "function");
    const originals = urls(values);
    if (!originals.length) return;
    const visible = compact ? originals.slice(0, 1) : originals;
    const chosen = new Set(urls(selected));
    const doc = root.ownerDocument;
    const fragment = doc.createDocumentFragment();
    visible.forEach((url, index) => {
      const figure = doc.createElement("figure");
      figure.className = "source-image";
      const media = doc.createElement("div");
      media.className = "source-image-media is-loading";
      const placeholder = doc.createElement("p");
      placeholder.className = "source-image-placeholder";
      placeholder.textContent = "Đang tải ảnh nguồn…";
      const image = doc.createElement("img");
      image.alt = typeof alt === "string" && alt ? `${alt} — ảnh ${index + 1}` : `Ảnh ${index + 1} từ bài Binance Square`;
      image.loading = "lazy";
      image.decoding = "async";
      image.referrerPolicy = "no-referrer";
      image.addEventListener("load", () => {
        media.classList.remove("is-loading");
        placeholder.remove();
      }, {once: true});
      const fail = () => {
        failedImages.add(url);
        media.classList.remove("is-loading");
        media.classList.add("is-error");
        image.remove();
        placeholder.textContent = "Không tải được ảnh từ CDN nguồn. Bạn có thể mở liên kết ảnh gốc bên dưới; ứng dụng không tự thử lại.";
        placeholder.setAttribute("role", "status");
      };
      image.addEventListener("error", fail, {once: true});
      media.append(image, placeholder);
      const caption = doc.createElement("figcaption");
      const link = doc.createElement("a");
      link.className = "source-image-original";
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.referrerPolicy = "no-referrer";
      link.textContent = `Ảnh gốc ${index + 1} · Binance CDN`;
      caption.append(link);
      if (typeof onSelect === "function" || typeof onRemove === "function") {
        const actions = doc.createElement("div");
        actions.className = "source-image-actions";
        if (typeof onSelect === "function") {
          const button = doc.createElement("button");
          button.type = "button";
          button.className = "source-image-select";
          button.textContent = chosen.has(url) ? "Đã chọn ảnh" : "Chọn ảnh";
          button.setAttribute("aria-pressed", String(chosen.has(url)));
          button.setAttribute("aria-label", `${button.textContent} ${index + 1}`);
          // Selection is parent-owned: a successful parent update renders its next state.
          button.addEventListener("click", () => onSelect(url));
          actions.append(button);
        }
        if (typeof onRemove === "function") {
          const button = doc.createElement("button");
          button.type = "button";
          button.className = "source-image-remove";
          button.textContent = "Bỏ ảnh";
          button.setAttribute("aria-label", `Bỏ ảnh ${index + 1}`);
          button.addEventListener("click", () => onRemove(url));
          actions.append(button);
        }
        caption.append(actions);
      }
      figure.append(media, caption);
      fragment.append(figure);
      if (failedImages.has(url)) fail(); else image.src = url;
    });
    const attribution = doc.createElement("p");
    attribution.className = "source-image-attribution";
    attribution.textContent = "Ảnh từ Binance Square; quyền thuộc chủ sở hữu nguồn. Xem ảnh không đồng nghĩa có quyền đăng lại.";
    fragment.append(attribution);
    root.append(fragment);
  }

  globalThis.BTIImages = {safeURL, urls, render};
})();
