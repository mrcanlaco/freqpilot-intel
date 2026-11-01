"use strict";

const $ = (selector) => document.querySelector(selector);
const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
};
const fmt = (value) => value === null || value === undefined ? "Chưa biết" : new Intl.NumberFormat("vi-VN", {notation: value > 9999 ? "compact" : "standard", maximumFractionDigits: 1}).format(value);
const when = (value) => value ? new Intl.DateTimeFormat("vi-VN", {dateStyle:"medium", timeStyle:"short"}).format(new Date(value)) : "Chưa biết";
const labels = {engagement:"Tương tác nội dung", performance:"Hiệu suất công khai", recognition:"Công nhận chính thức"};
const icons = {engagement:"↗", performance:"≈", recognition:"✦"};
const draftLabels = {source_excerpt: "TRÍCH XUẤT NGUỒN", research_backlog: "BACKLOG · CHƯA ĐỦ ĐIỀU KIỆN", content:"NỘI DUNG CÓ NGUỒN", strategy_hypothesis:"GIẢ THUYẾT CÓ ĐIỀU KIỆN NGUỒN", market_scenario:"KỊCH BẢN CÓ ĐIỀU KIỆN NGUỒN"};
let researchCoverage = null;
let currentArticleData = {items: []};
let currentDossierData = {items: []};
let currentEvidenceData = {items: []};
let newArticleData = {items: []};
let newSnapshotAt = null;
let newReqSeq = 0;
let newFeedKey = "";
let newLoadedWatchId = "";
let newLoadedMode = "";
let newVisitedPages = new Set();
let drawerReqSeq = 0;
let drawerReturnFocus = null;
const routeIdentities = new Map();
let routeReader = "";
const lazySections = new Map();
const feedStates = new Map();
const sectionPages = {
  "bai-viet": "discover", "tong-quan": "discover", "uu-tien-viet": "discover", "luong-trai-nghiem": "discover",
  "ho-so": "sources", "bang-chung": "sources", workspace: "research", "ban-nhap": "research",
  moi: "following", nguon: "more", metrics: "more", "bo-quet": "more", "cam-nang": "more"
};
let savedBookmarks = new Set();
let comparedDossiers = new Map();
let savedFilterActive = false;

try {
  const rawSaved = JSON.parse(localStorage.getItem("bti_saved_articles_v1") || "[]");
  if (Array.isArray(rawSaved)) savedBookmarks = new Set(rawSaved.map(String));
} catch (_e) {}

function persistBookmarks(before) {
  try {
    savedBookmarks = new Set(BTIWorkspace.mergeBookmarks(localStorage, [...before], [...savedBookmarks]));
  } catch (_e) {
    alert("Không lưu được tủ sách trên trình duyệt. Dung lượng hoặc quyền lưu trữ không khả dụng.");
    return false;
  }
  const badge = $("#saved-count-badge");
  if (badge) badge.textContent = String(savedBookmarks.size);
  return true;
}
async function getJSON(url) {
  const response = await fetch(url, {headers: {"Accept": "application/json"}, signal: AbortSignal.timeout(20000)});
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function sectionError(selector, retry, message = "Không tải được dữ liệu. Các phần khác vẫn hoạt động.") {
  const root = $(selector);
  root.replaceChildren();
  const box = el(root.tagName === "TBODY" ? "tr" : "div", "empty");
  const content = root.tagName === "TBODY" ? el("td") : box;
  if (root.tagName === "TBODY") { content.colSpan = 5; box.append(content); }
  const button = el("button", "inspect-btn", "Thử lại phần này");
  button.type = "button"; button.addEventListener("click", retry);
  content.append(el("p", "", message), button); root.append(box);
}

function routeIdentity(prefix, params = new URL(location.href).searchParams) {
  const entries = [...params].filter(([key]) => key.startsWith(`${prefix}.`) || (prefix === "a" && key === "saved"));
  entries.sort(([a, av], [b, bv]) => a.localeCompare(b) || av.localeCompare(bv));
  return JSON.stringify(entries);
}

function syncFilterURL(prefix, form, offset = 0) {
  const url = new URL(location.href);
  for (const key of [...url.searchParams.keys()]) if (key.startsWith(`${prefix}.`)) url.searchParams.delete(key);
  for (const [key, value] of new FormData(form)) {
    if (String(value).trim() || key === "date_from" || key === "date_to") url.searchParams.set(`${prefix}.${key}`, String(value).trim());
  }
  if (offset) url.searchParams.set(`${prefix}.offset`, String(offset));
  if (prefix === "a") {
    if (savedFilterActive) url.searchParams.set("saved", "1"); else url.searchParams.delete("saved");
  }
  routeIdentities.set(prefix, routeIdentity(prefix, url.searchParams));
  if (url.href !== location.href) history.pushState(null, "", url);
}

function readerURL(kind, key) {
  const url = new URL(location.href);
  url.searchParams.delete("article"); url.searchParams.delete("dossier");
  if (kind) url.searchParams.set(kind, key);
  if (kind) url.hash = kind === "dossier" ? "ho-so" : ($("#moi").closest("[data-page]")?.hidden ? "bai-viet" : "moi");
  routeReader = kind ? `${kind}:${key}` : "";
  if (url.href !== location.href) history.pushState(null, "", url);
}

function navigateToSection(id, options = {}) {
  id = String(id || "bai-viet").replace(/^#/, "");
  const target = document.getElementById(id) || document.getElementById("bai-viet");
  const page = sectionPages[id] || target.closest("[data-page]")?.dataset.page || "discover";
  document.querySelectorAll("[data-page]").forEach(node => { node.hidden = node.dataset.page !== page; });
  const title = {discover: "Khám phá", sources: "Nguồn", research: "Nghiên cứu của tôi", following: "Theo dõi", more: "Nguồn dữ liệu & trợ giúp"}[page];
  document.title = `${title} · Future Intelligence`;
  for (let node = target; node; node = node.parentElement) {
    if (node.tagName === "DETAILS") node.open = true;
  }
  document.querySelectorAll(".nav-item").forEach(link => {
    const active = sectionPages[link.dataset.view] === page;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  });
  if (options.history !== false) {
    const url = new URL(location.href); url.hash = target.id;
    if (url.href !== location.href) history.pushState(null, "", url);
  }
  if (options.load !== false) {
    lazySections.get(target.id)?.();
    const primary = {discover: "bai-viet", sources: "ho-so", following: "moi"}[page];
    if (primary && primary !== target.id) lazySections.get(primary)?.();
    if (page === "more") lazySections.get("metrics")?.();
  }
  if (options.scroll !== false) target.scrollIntoView({behavior: options.behavior || "auto", block: "start"});
  if (options.focus) {
    if (!target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
    target.focus({preventScroll: true});
  }
  return page;
}

function lazySection(id, loader) {
  let loading = false, loaded = false;
  const run = async () => {
    const target = document.getElementById(id);
    if ((loading || loaded) && !["bai-viet", "ho-so"].includes(id)) return;
    if (target?.closest("[data-page]")?.hidden) return;
    for (let node = target; node; node = node.parentElement) {
      if (node.tagName === "DETAILS" && !node.open) return;
    }
    loading = true;
    try { await loader(); loaded = true; }
    finally { loading = false; }
  };
  lazySections.set(id, run);
  const target = document.getElementById(id);
  if (!target) return;
  for (let node = target; node; node = node.parentElement) {
    if (node.tagName === "DETAILS") node.addEventListener("toggle", () => { if (node.open) run().catch(() => {}); });
  }
  const observer = new IntersectionObserver(entries => {
    if (entries.some(entry => entry.isIntersecting) && !target.closest("[data-page]")?.hidden) {
      run().catch(() => {});
    }
  }, {rootMargin: "120px"});
  observer.observe(target);
}

function headerState(message, state = "neutral") {
  const node = $("#header-status");
  node.textContent = message;
  node.parentElement.dataset.state = state;
}

function listState(name, status) {
  const body = name === "evidence" ? "#evidence-body" : `#${name}-grid`;
  const root = $(body);
  feedStates.set(name, status);
  if (name === "article") currentArticleData = {items: []};
  if (name === "dossier") currentDossierData = {items: []};
  if (name === "evidence") currentEvidenceData = {items: []};
  if (name === "new") { newArticleData = {items: []}; newLoadedWatchId = ""; newLoadedMode = ""; }
  const sample = $(`#${name}-sample`);
  if (sample) sample.textContent = status === "loading" ? "Đang tải dữ liệu…" : status === "changed" ? "Bộ lọc đã đổi. Bấm Xem cập nhật để tải lại." : "Chưa tải được dữ liệu.";
  renderPaginationBar(`#${name}-pagination`, null);
  if (name === "article" && $("#article-coverage-note")) $("#article-coverage-note").textContent = "";
  if (name === "new") $("#watch-mark-read").hidden = true;
  root.setAttribute("aria-busy", String(status === "loading"));
  if (status === "loading" || status === "changed") {
    root.replaceChildren();
    const row = el(name === "evidence" ? "tr" : "div", "empty");
    if (name === "evidence") { const td = el("td", "", "Đang tải bằng chứng…"); td.colSpan = 5; row.append(td); }
    else row.textContent = status === "changed" ? "Chọn Xem cập nhật để tải đúng bộ lọc mới." : "Đang tải dữ liệu…";
    root.append(row);
  }
  if (name === "article") headerState(status === "loading" ? "Đang tải bài viết…" : "Bài viết chưa tải được · Có thể thử lại", status === "loading" ? "neutral" : "warn");
}

function metric(label, value, note) {
  const card = el("article", "metric");
  card.append(el("div", "metric-label", label), el("div", "metric-value", value), el("div", "metric-note", note));
  return card;
}

function renderAssetFilters(entries) {
  const root = $("#vi-asset-pills");
  if (!root) return;
  const selected = $("#article-controls").elements.asset.value;
  root.replaceChildren();
  for (const entry of [{asset: "", count: null}, ...entries.slice(0, 8)]) {
    const chip = el("button", `filter-chip asset-chip${entry.asset === selected ? " active" : ""}`, entry.asset ? `$${entry.asset} (${entry.count})` : "Tất cả tài sản");
    chip.type = "button"; chip.dataset.asset = entry.asset;
    root.append(chip);
  }
}

function renderVietnamesePortal(data) {
  const portal = data.vietnamese_portal || {};
  const goal = portal.goal_and_backlog_vi || {};
  const viNative = portal.vi_native_articles ?? 0;
  const tagged = portal.keyword_tagged_articles ?? data.research_coverage?.sample_count;
  const badge = $("#vi-native-badge");
  if (badge) {
    badge.textContent = `${fmt(viNative)} bài gốc Tiếng Việt · ${fmt(tagged)} bài hỗ trợ lọc từ khóa (không phải bản dịch)`;
  }
  renderAssetFilters(currentArticleData.vietnamese_summary?.top_assets || portal.top_assets || []);
  const audited = goal.quality_audited_bodies ?? (data.content_extraction?.quality_audited_bodies || 0);
  const target = goal.target_quality_bodies || 1000;
  const qualifying = goal.total_unique_qualifying_bodies ?? (data.content_extraction?.total_unique_qualifying_bodies || 0);
  const pct = Math.min(100, Math.round((audited / target) * 100));
  const fill = $("#goal-progress-fill");
  if (fill) fill.style.width = `${pct}%`;
  const goalPill = $("#goal-status-pill");
  if (goalPill) {
    const reached = audited >= target;
    goalPill.textContent = reached ? "ĐÃ ĐẠT 1.000+" : `CHƯA ĐẠT (${audited}/${target})`;
    goalPill.className = `status-pill ${reached ? "" : "warn"}`;
  }
  const summaryText = $("#goal-summary-text");
  if (summaryText) {
    const currentState = goal.current_extraction_status || "unknown";
    const cooldownUntil = goal.archive_cooldown?.active ? ` (Tạm dừng cooldown nguồn đến ${when(goal.archive_cooldown.until)})` : "";
    summaryText.textContent = `Đã kiểm định chất lượng v2: ${fmt(audited)}/${fmt(target)} bài (${fmt(qualifying)} bài đủ chuẩn cấu trúc). Trạng thái trích xuất hiện tại: ${currentState.toUpperCase()}${cooldownUntil} · Lần trích xuất thực gần nhất: ${when(goal.last_real_extraction_at)}.`;
  }
  const breakdown = $("#goal-breakdown-grid");
  if (breakdown) {
    breakdown.replaceChildren(
      (() => { const d = el("div", "goal-stat"); d.append(el("span", "", "7 ngày gần nhất"), el("b", "", `${fmt(goal.recent_7d_bodies ?? 0)} bài`)); return d; })(),
      (() => { const d = el("div", "goal-stat"); d.append(el("span", "", "30 ngày gần nhất"), el("b", "", `${fmt(goal.recent_30d_bodies ?? 0)} bài`)); return d; })(),
      (() => { const d = el("div", "goal-stat"); d.append(el("span", "", "Lưu trữ lịch sử (2 năm)"), el("b", "", `${fmt(goal.historical_archive_bodies ?? 0)} bài`)); return d; })()
    );
  }
  const notices = $("#goal-notices");
  if (notices) {
    notices.replaceChildren();
    for (const note of (goal.notices_vi || [])) {
      notices.append(el("li", "", note));
    }
  }
}

function renderOverview(data) {
  const root = $("#metrics"); root.replaceChildren(
    metric("Hồ sơ nghiên cứu", fmt(data.dossier_count), "Awardee, creator và portfolio tách biệt"),
    metric("Bằng chứng", fmt(data.counts.evidence), "Snapshot có provenance"),
    metric("Phiên bản nội dung", fmt(data.counts.content_versions), "Checksum chống trùng"),
    metric("Bản nháp", fmt(data.draft_count), "0 tự động xuất bản")
  );
  root.append(el("p", "metric-note", `Ingest ${data.last_ingest?.status || "chưa chạy"} · ${when(data.last_ingest?.finished_at)}`));
  const gate = data.pipeline_smoke;
  const panel = $("#pipeline-gate");
  const pill = panel.querySelector(".status-pill");
  pill.textContent = gate.status === "passed" ? "ĐẠT" : "CHƯA ĐẠT";
  pill.className = `status-pill ${gate.status === "passed" ? "" : "bad"}`;
  panel.querySelector("p").textContent = `${gate.substantive_non_official_contents} nội dung creator · ${gate.candidates_with_engagement} hồ sơ có snapshot. ${gate.definition}`;
  const quality = data.trader_quality;
  const qualityPanel = $("#quality-gate");
  const qualityPill = qualityPanel.querySelector(".status-pill");
  qualityPill.textContent = quality.status === "not_established" ? "CHƯA THIẾT LẬP" : "QUAN SÁT MỘT PHẦN";
  qualityPill.className = "status-pill warn";
  qualityPanel.querySelector("p").textContent = `${quality.official_trader_awardees} awardee chính thức · ${quality.awardee_substantive_contents} bài sâu · ${quality.awardees_with_platform_performance} awardee có performance platform · ${quality.unlinked_platform_performance_portfolios} portfolio direct-platform · ${quality.unlinked_captured_performance_portfolios} portfolio từ operator capture. ${quality.definition}`;
  renderVietnamesePortal(data);
}

function pillarSummary(group) {
  if (group.status !== "available") return "Chưa có bằng chứng";
  const value = group.items[0]?.value || {};
  if (value.views !== undefined) return `${fmt(value.views)} lượt xem · ${fmt(value.likes)} lượt thích`;
  if (value.program && value.rank !== null && value.rank !== undefined) return `Hạng ${value.rank} · ${fmt(value.award_vote_count)} phiếu giải`;
  if (value.program) return value.program;
  if (value.leader_pnl !== undefined) return `ROI ${fmt(value.roi_percent)}% · PnL leader ${fmt(value.leader_pnl)}`;
  if (value.reported_text) return "Chỉ có tự công bố";
  return group.label;
}

function renderEngagementSummary(summary) {
  const box = el("section", "engagement-summary");
  const head = el("div", "engagement-summary-head");
  head.append(
    el("strong", "", `Bài xuất bản · ${summary.window.label.replace(/^Trailing (\d+) days$/, "$1 ngày gần nhất")}`),
    el("span", "", `${summary.sample_n} bài trong kỳ`)
  );
  box.append(head);
  box.append(el("p", "engagement-caveat", summary.sample_n
    ? `Tổng tương tác: ${fmt(summary.interactions.total)} · chỉ ${summary.interactions.complete_n}/${summary.sample_n} bài đủ lượt thích, bình luận và chia sẻ.`
    : "Chưa có mẫu bài đủ điều kiện trong kỳ. Tương tác chưa biết, không phải bằng 0."));
  const details = el("details", "engagement-details");
  details.append(el("summary", "", "Số liệu & mốc quan sát"));
  const grid = el("div", "engagement-stat-grid");
  const names = {likes:"Lượt thích", comments:"Bình luận", shares:"Chia sẻ", views:"Lượt xem"};
  for (const key of ["likes", "comments", "shares", "views"]) {
    const value = summary.metrics[key];
    const stat = el("div", "engagement-stat");
    stat.append(
      el("span", "", names[key]),
      el("b", "", `Σ ${fmt(value.total)}`),
      el("small", "", `TB ${fmt(value.average)} · biết ${value.known_n}/${summary.sample_n} bài`)
    );
    grid.append(stat);
  }
  const combined = el("div", "engagement-stat combined");
  combined.append(
    el("span", "", "Tổng tương tác"),
    el("b", "", `Σ ${fmt(summary.interactions.total)}`),
    el("small", "", `TB ${fmt(summary.interactions.average)} · đủ ${summary.interactions.complete_n}/${summary.sample_n} bài · không gồm lượt xem`)
  );
  grid.append(combined); details.append(grid);
  details.append(el("p", "engagement-caveat", `Quan sát đến ${when(summary.as_of)} · Đây là snapshot của bài xuất bản trong kỳ, không phải mức tăng trong kỳ. Tăng trưởng chưa tính: ${summary.growth.reason}`));
  box.append(details);
  return box;
}

function renderPaginationBar(containerId, page, onLoad) {
  const bar = $(containerId);
  if (!bar) return;
  bar.replaceChildren(); bar.hidden = !page || page.total <= page.limit;
  if (bar.hidden) return;
  bar.append(el("span", "", `Mục ${page.total ? page.offset + 1 : 0}–${Math.min(page.offset + page.limit, page.total)} / ${fmt(page.total)}`));
  const actions = el("div", "pagination-actions");
  for (const [label, offset, disabled] of [
    ["← Trang trước", Math.max(0, page.offset - page.limit), page.offset === 0],
    ["Trang tiếp →", page.next_offset, !page.has_more]
  ]) {
    const btn = el("button", "inspect-btn", label); btn.type = "button"; btn.disabled = disabled;
    btn.addEventListener("click", async () => { btn.disabled = true; await onLoad(offset); });
    actions.append(btn);
  }
  bar.append(actions);
}

function updateCompareTray() {
  const tray = $("#compare-tray");
  const countNode = $("#compare-count");
  const chips = $("#compare-chips");
  if (!tray || !countNode || !chips) return;
  countNode.textContent = String(comparedDossiers.size);
  chips.replaceChildren();
  if (!comparedDossiers.size) {
    tray.hidden = true;
    return;
  }
  tray.hidden = false;
  for (const [slug, item] of comparedDossiers.entries()) {
    const chip = el("span", "badge-vi", `${item.display_name} ✕`);
    chip.style.cursor = "pointer";
    chip.addEventListener("click", () => {
      comparedDossiers.delete(slug);
      updateCompareTray();
      renderDossiers(currentDossierData, false);
    });
    chips.append(chip);
  }
}

function openComparisonDrawer() {
  const backdrop = $("#drawer-backdrop");
  const body = $("#drawer-body");
  $("#drawer-eyebrow").textContent = "ĐỐI CHIẾU NGANG · 3 TRỤ CỘT BẰNG CHỨNG";
  $("#drawer-title").textContent = `So sánh ${comparedDossiers.size} Hồ sơ Chủ thể`;
  body.replaceChildren();
  backdrop.hidden = false;
  const grid = el("div", "compare-grid");
  for (const item of comparedDossiers.values()) {
    const col = el("div", "compare-col");
    const known = Object.values(item.pillars).filter(x => x.status === "available").length;
    col.append(
      el("strong", "", item.display_name),
      el("small", "", `@${item.username || item.slug} · ${item.category_label_vi || item.category}`),
      el("span", `status-pill ${known ? "" : "muted"}`, `${known}/3 NHÓM CÓ DỮ LIỆU`),
      el("div", "vi-verdict-banner", item.vi_profile?.audit_verdict_vi || item.selection_note)
    );
    for (const key of ["engagement", "performance", "recognition"]) {
      const grp = item.pillars[key];
      const pBox = el("div", "pillar");
      pBox.append(
        el("strong", "", `${icons[key]} ${labels[key]}`),
        el("div", "", pillarSummary(grp)),
        el("small", "", `Cấp: ${grp.verification_level.toUpperCase()}`)
      );
      col.append(pBox);
    }
    if (item.engagement_window) {
      col.append(el("small", "", `Tương tác kỳ chọn (N=${item.engagement_window.sample_n}): Σ 3 metric = ${fmt(item.engagement_window.interactions?.total)}`));
    }
    const fullBtn = el("button", "inspect-btn", "Mở hồ sơ đầy đủ →");
    fullBtn.type = "button";
    fullBtn.addEventListener("click", () => openDossierDrawer(item.slug));
    col.append(fullBtn);
    grid.append(col);
  }
  body.append(grid);
}

function renderDossiers(data, resetLimit = true) {
  if (feedStates.get("dossier") !== "ready") return;
  currentDossierData = data || {items: []};
  // A page replaces the visible list; full evidence is loaded only in the reader.
  const items = currentDossierData.items || [];
  const root = $("#dossier-grid"); root.replaceChildren();
  const window = currentDossierData.window?.label || "cửa sổ đã chọn";
  syncDossierCriteria();
  $("#dossier-sample").textContent = `${currentDossierData.sample_count ?? items.length} hồ sơ khớp · ${currentDossierData.eligible_article_count ?? 0} bài đúng kỳ.`;
  $("#dossier-window-note").textContent = `Kỳ bài gốc: ${window} · quan sát tương tác đến ${when(currentDossierData.engagement_as_of)}. ${currentDossierData.scope_note || ""}`;
  if (!items.length) {
    root.append(el("div", "empty", "Không có hồ sơ công khai khớp tiêu chí. Hãy mở rộng nhóm nguồn hoặc đặt lại bộ lọc."));
    renderPaginationBar("#dossier-pagination", currentDossierData.pagination, offset => refreshDossiersFromControls(false, offset));
    return;
  }
  const visibleItems = items;
  for (const item of visibleItems) {
    const card = el("article", "panel dossier-card");
    const head = el("div", "dossier-head");
    const identity = el("div", "identity");
    identity.append(el("div", "avatar", item.display_name.slice(0,2).toUpperCase()));
    const text = el("div");
    text.append(
      el("h3", "", item.display_name),
      el("p", "", `@${item.username || "không rõ"} · ${item.content_count} nội dung · ${item.category_label_vi || item.category}`)
    );
    identity.append(text);
    head.append(identity);
    card.append(head);
    const notes = el("details", "source-notes");
    notes.append(el("summary", "", "Định danh & giới hạn đọc hồ sơ"));
    if (item.bio) notes.append(el("p", "", `Tiểu sử: ${item.bio}`));
    if (item.vi_profile?.audit_verdict_vi) notes.append(el("p", "", item.vi_profile.audit_verdict_vi));
    const list = el("div", "pillar-list");
    for (const key of ["engagement","performance","recognition"]) {
      const group = item.pillars[key]; const box = el("div", "pillar"); const top = el("div", "pillar-top");
      const name = el("div", "pillar-name"); name.append(el("span", "pillar-icon", icons[key]), document.createTextNode(labels[key]));
      top.append(name, el("div", "pillar-value", pillarSummary(group))); box.append(top);
      if (key === "engagement") {
        box.append(el("p", "", "Snapshot trong kho, độc lập với kỳ lọc bài phía dưới."));
        notes.append(el("p", "", `${labels[key]}: ${group.limitations}`));
      } else box.append(el("p", "", group.limitations));
      list.append(box);
    }
    card.append(list);
    if (item.engagement_window) card.append(renderEngagementSummary(item.engagement_window));
    card.append(notes);
    if (item.contents?.[0]) {
      const link = el("a", "content-link", item.contents[0].title); link.href=item.contents[0].canonical_url; link.target="_blank"; link.rel="noopener";
      link.append(el("span", "", `Phiên bản ${item.contents[0].version_no} · lấy ${when(item.contents[0].fetched_at)} ↗`)); card.append(link);
    }
    const actions = el("div", "article-actions");
    const detailBtn = el("button", "inspect-btn", "Hồ sơ 3 nhóm bằng chứng");
    detailBtn.type = "button";
    detailBtn.addEventListener("click", () => openDossierDrawer(item.slug));
    const postsBtn = el("button", "hero-btn primary", "Đọc bài của tác giả");
    postsBtn.type = "button";
    postsBtn.addEventListener("click", () => showAuthorArticles(item.slug));
    const isCompared = comparedDossiers.has(item.slug);
    const compareBtn = el("button", `compare-btn ${isCompared ? "selected" : ""}`, isCompared ? "⚖️ Đã chọn so sánh" : "⚖️ So sánh");
    compareBtn.type = "button";
    compareBtn.addEventListener("click", () => {
      if (comparedDossiers.has(item.slug)) {
        comparedDossiers.delete(item.slug);
      } else {
        if (comparedDossiers.size >= 3) {
          const oldestKey = comparedDossiers.keys().next().value;
          comparedDossiers.delete(oldestKey);
        }
        comparedDossiers.set(item.slug, item);
      }
      updateCompareTray();
      renderDossiers(currentDossierData, false);
    });
    actions.append(postsBtn, detailBtn, compareBtn);
    card.append(actions);
    root.append(card);
  }
  renderPaginationBar("#dossier-pagination", currentDossierData.pagination, offset => refreshDossiersFromControls(false, offset));
}

function renderEvidence(data) {
  if (data) currentEvidenceData = data;
  const filtered = currentEvidenceData.items || [];
  const sampleNode = $("#evidence-sample");
  if (sampleNode) sampleNode.textContent = `${fmt(currentEvidenceData.sample_count ?? currentEvidenceData.pagination?.total ?? filtered.length)} bằng chứng khớp bộ lọc · mỗi trang tối đa 20.`;
  const body = $("#evidence-body"); body.replaceChildren();
  if (!filtered.length) {
    const tr=el("tr"); const td=el("td","","Chưa có bằng chứng public-shareable khớp bộ lọc."); td.colSpan=5; tr.append(td); body.append(tr);
    renderPaginationBar("#evidence-pagination", currentEvidenceData.pagination, refreshEvidence);
    return;
  }
  const visible = filtered;
  for (const item of visible) {
    const tr=el("tr");
    const entity=el("td"); entity.dataset.label = "Chủ thể";
    const entityBtn = el("button", "author-chip-btn", item.entity);
    entityBtn.type = "button";
    entityBtn.addEventListener("click", () => openDossierDrawer(item.entity_slug));
    entity.append(entityBtn, el("small","",`@${item.entity_slug}`));
    const pillar=el("td","",labels[item.pillar] || item.pillar); pillar.dataset.label = "Trụ cột"; pillar.append(el("small","",item.kind));
    const level=el("td"); level.dataset.label = "Cấp xác minh";
    level.append(el("span",`status-pill ${["self_claim","operator_capture","unknown"].includes(item.verification_level) ? "warn" : ""}`,item.verification_level.toUpperCase()));
    if (item.verification_label_vi) level.append(el("small", "", item.verification_label_vi));
    const summaryText = item.value_summary_vi || JSON.stringify(item.value, null, 1);
    const value=el("td","",summaryText); value.dataset.label = "Dữ liệu quan sát"; value.append(el("small","",item.limitations));
    const source=el("td"); source.dataset.label = "Nguồn";
    const link=el("a","","Mở nguồn ↗"); link.href=item.source_url; link.target="_blank"; link.rel="noopener";
    source.append(link,el("small","",`v${item.version || "–"} · ${when(item.observed_at)}`));
    if (item.post_id) {
      const add = el("button", "inspect-btn", "Thêm vào nghiên cứu"); add.type = "button";
      add.addEventListener("click", async () => {
        const destination = BTIWorkspace.activeId();
        if (!destination) { alert("Hãy tạo hoặc chọn nghiên cứu trước khi thêm bằng chứng."); navigateToSection("workspace", {focus: true}); return; }
        try {
          const article = await getJSON(`/api/articles/${encodeURIComponent(item.post_id)}`);
          if (BTIWorkspace.activeId() !== destination) throw new Error("Nghiên cứu đã thay đổi trong lúc tải.");
          if (BTIWorkspace.addArticle(article, item.id)) {
            add.textContent = "Đã thêm vào nghiên cứu";
            add.setAttribute("aria-live", "polite");
          }
        }
        catch (_e) { alert("Không thể đối chiếu bài nguồn; bằng chứng chưa được thêm."); }
      });
      source.append(add);
    }
    tr.append(entity,pillar,level,value,source); body.append(tr);
  }
  renderPaginationBar("#evidence-pagination", currentEvidenceData.pagination, refreshEvidence);
}

function renderDrafts(items) {
  const root=$("#draft-grid"); root.replaceChildren();
  if(!items.length){root.append(el("div","empty","Chưa có bản nháp qua cổng nguồn hiện tại. Bạn vẫn có thể tạo nghiên cứu riêng."));return;}
  for(const item of items){
    const displayKind = item.body.output_kind || item.artifact_type;
    const card=el("article","panel draft-card");
    card.append(el("div","draft-kind",draftLabels[displayKind]||displayKind),el("h3","",item.title),el("p","",item.summary));
    const detail = el("div", "draft-detail-box");
    if (item.body.source_claim) detail.append(el("div", "", `Luận điểm trích xuất: "${item.body.source_claim}"`));
    if (item.body.analyst_read) detail.append(el("div", "", `Nhận định kiểm toán: ${item.body.analyst_read}`));
    if (item.body.sections?.nguon_noi_gi) detail.append(el("div", "", `Nguồn nêu: "${item.body.sections.nguon_noi_gi}"`));
    if (item.body.hypothesis) detail.append(el("div", "", `Giả thuyết: "${item.body.hypothesis}" · Cổng: ${item.body.gate || "Chỉ nghiên cứu"}`));
    if (item.body.base_scenario) detail.append(el("div", "", `Kịch bản cơ sở: ${item.body.base_scenario}`));
    if (item.body.research_brief) {
      const brief = item.body.research_brief;
      detail.append(el("div", "", `Trích xuất xác định, không phải bản dịch: ${brief.method}`));
      for (const [key, label] of [["asset", "Tài sản nguồn nêu"], ["horizon", "Khung thời gian"], ["trigger", "Điều kiện kích hoạt"], ["invalidation", "Điều kiện vô hiệu"]]) {
        detail.append(el("div", "", `${label}: ${brief[key] || "Chưa biết"}`));
      }
    }
    if (Array.isArray(item.body.unknowns) && item.body.unknowns.length) {
      detail.append(el("div", "", `Khoảng trống chưa biết: ${item.body.unknowns.join(" · ")}`));
    }
    if (detail.childElementCount) card.append(detail);
    const meta=el("div","draft-meta"); const citation=item.body.citations?.[0];
    if(citation){const link=el("a","draft-citation",`Nguồn: ${citation.author} · v${citation.version} ↗`);link.href=citation.url;link.target="_blank";link.rel="noopener";meta.append(link);}
    meta.append(el("p","",item.body.disclaimer)); card.append(meta); root.append(card);
  }
}

function renderSources(items){
  const root=$("#source-grid");root.replaceChildren();
  for (const item of items) {
    const card = el("article", "panel source-card"), top = el("div", "source-card-top");
    top.append(el("h3", "", item.name), el("span", `status-pill ${item.capability_status === "available" ? "" : item.capability_status === "blocked" ? "bad" : "warn"}`, item.capability_status.toUpperCase()));
    card.append(top, el("p", "", item.capability_note || "Chưa kiểm tra"));
    card.append(el("p", "", `Độ mới nguồn: ${item.freshness || "unknown"} · Lần lấy thành công: ${when(item.last_success_at)}`));
    if (item.freshness_note) card.append(el("p", "", item.freshness_note));
    const link = el("a", "", item.url); link.href = item.url; link.target = "_blank"; link.rel = "noopener";
    card.append(link); root.append(card);
  }
}

function setupNav() {
  document.addEventListener("click", event => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest("a[href]");
    if (!link || link.target || link.hasAttribute("download")) return;
    const url = new URL(link.href, location.href);
    if (url.origin !== location.origin || url.pathname !== location.pathname || url.search !== location.search || !url.hash) return;
    const id = decodeURIComponent(url.hash.slice(1));
    if (!document.getElementById(id)) return;
    event.preventDefault();
    navigateToSection(id, {focus: true});
  });
}

function renderWorker(worker) {
  const panel = $("#worker-gate");
  const pill = panel.querySelector(".status-pill");
  const ext = worker.content_extraction || {};
  const currentExt = ext.current_extraction_status || ext.status || "unknown";
  pill.textContent = `${worker.status.toUpperCase()} · EXTRACT: ${String(currentExt).toUpperCase()}`;
  pill.className = `status-pill ${["degraded","stale"].includes(worker.status) ? "bad" : ["not_started","not_configured"].includes(worker.status) || currentExt === "cooldown" ? "warn" : ""}`;
  const policy = worker.crawler?.policy || {};
  const discovery = worker.crawler?.discovery || {};
  const cooldownNote = ext.archive_host_cooldown?.active ? ` · Archive cooldown đến ${when(ext.archive_host_cooldown.until)}` : "";
  panel.querySelector("p").textContent = `Cycle gần nhất ${when(worker.last_cycle_completed_at)} · dữ liệu giữ lại ${worker.retained_data?.status || "unknown"} · backup ${worker.backup?.status || "not_run"}${cooldownNote}. Crawler policy ${policy.status || "not_checked"}; raw API ${policy.raw_api_collection || "not_checked"}. Sitemap discovery ${discovery.status || "not_started"}: ${discovery.pending_count || 0} URL ứng viên, chưa phải bài đã xác minh.`;
}

function renderScannerArchitecture(scanner) {
  const root = $("#scanner-fleet-grid");
  if (!root || !scanner) return;
  root.replaceChildren();
  const auto = scanner.autonomous_operation || {};
  const pacing = auto.adaptive_pacing || {};
  const disc = scanner.discovery_engines || {};
  const fleet = scanner.fleet || {};
  const rep = scanner.replication_architecture || {};

  const c1 = el("article", "panel glossary-card");
  c1.append(
    el("span", `status-pill ${scanner.status === "stale" || scanner.status === "degraded" ? "bad" : ""}`, `TRẠNG THÁI: ${String(scanner.status || "unknown").toUpperCase()}`),
    el("h3", "", "Nhịp quét Thích ứng và Tự phục hồi"),
    el("p", "", `Chế độ nhịp kế tiếp: ${pacing.mode || "standard"} (${pacing.next_interval_seconds || auto.min_interval_seconds}s) · Ứng viên sẵn sàng: ${fmt(pacing.ready_candidates ?? 0)} (${fmt(pacing.ready_vietnamese_candidates ?? 0)} bài Việt).`),
    el("div", "draft-detail-box", auto.startup_note_vi || "")
  );

  const c2 = el("article", "panel glossary-card");
  c2.append(
    el("span", "status-pill", `ĐA NODE · ${fleet.execution_mode?.toUpperCase() || "LOCAL"}`),
    el("h3", "", `Điều phối ${fleet.active_node_count || 1} Node · 1 Writer Duy nhất`),
    el("p", "", `Giới hạn chung: tối đa ${fleet.max_inflight_total || 2} luồng song song, cách nhịp ${fleet.shared_host_spacing_seconds || 1.5}s/host. Trần tài nguyên worker phụ trợ: ${fleet.node_resource_ceiling?.memory_limit_mib || 256}MiB RAM, 1 CPU, Nice ${fleet.node_resource_ceiling?.nice || 10}, 0 tiến trình chờ.`),
    el("div", "draft-detail-box", `Ngôn ngữ quét CDX: ${(disc.locales_supported || ["en"]).join(", ").toUpperCase()} · Sitemap ứng viên: ${fmt(disc.sitemap_candidates_queued ?? 0)}`)
  );

  const c3 = el("article", "panel glossary-card");
  c3.append(
    el("span", "status-pill warn", "NHÂN BẢN AN TOÀN"),
    el("h3", "", "Nhân bản Worker Node và Dự phòng Lạnh"),
    el("p", "", "Hỗ trợ xuất gói thực thi phụ trợ (worker_node ký HMAC-SHA256) hoặc bản sao dự phòng lạnh (cold_standby tự động giữ nguyên toàn bộ trạng thái cooldown/host-hold và khóa dual-writer).")
  );
  const stepsBox = el("div", "draft-detail-box");
  for (const step of (rep.blueprint_steps_vi || [])) stepsBox.append(el("div", "", step));
  c3.append(stepsBox);

  root.append(c1, c2, c3);
}

function renderActiveArticleFilterBar() {
  const bar = $("#article-active-filters");
  const form = $("#article-controls");
  if (!bar || !form) return;
  const tags = [];
  if (savedFilterActive) tags.push(`📌 Đang lọc: Bài đã lưu (${savedBookmarks.size})`);
  if (form.elements.lang?.value) tags.push(`Ngôn ngữ: ${form.elements.lang.selectedOptions[0]?.textContent || form.elements.lang.value}`);
  if (form.elements.topic?.value) tags.push(`Chuyên đề: ${form.elements.topic.selectedOptions[0]?.textContent || form.elements.topic.value}`);
  if (form.elements.asset?.value) tags.push(`Tài sản: $${form.elements.asset.value.toUpperCase()}`);
  if (form.elements.author?.value) tags.push(`Tác giả: ${form.elements.author.value}`);
  else if (form.elements.entity?.value) {
    const slug = form.elements.entity.value;
    const subject = currentDossierData.items.find(item => item.slug === slug)?.display_name
      || currentArticleData.items.find(item => item.entity_slug === slug)?.author || slug;
    tags.push(`Nguồn: ${subject}`);
  }
  if (form.elements.q?.value) tags.push(`Từ khóa: "${form.elements.q.value}"`);
  if (form.elements.basis.value !== "all") tags.push(`Căn cứ: ${form.elements.basis.selectedOptions[0].textContent}`);
  if (form.elements.cohort.value !== "all") tags.push(`Nhóm tác giả: ${form.elements.cohort.selectedOptions[0].textContent}`);
  if (form.elements.quality_only.value) tags.push("Qua sàng lọc kỹ thuật v2 · không thẩm định chất lượng");
  for (const button of document.querySelectorAll("#usecase-flow-grid [data-flow]")) {
    const active = button.dataset.flow === "source-claims" ? form.elements.basis.value === "quoted"
      : button.dataset.flow === "vi-native" ? form.elements.lang.value === "vi" : savedFilterActive;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  }
  if (!tags.length) {
    bar.hidden = true;
    bar.replaceChildren();
    return;
  }
  bar.hidden = false;
  bar.replaceChildren(el("strong", "", "Bộ lọc đang bật:"));
  for (const t of tags) bar.append(el("span", "badge-vi", t));
  const clear = el("button", "reset-filter-btn", "Xóa bộ lọc");
  clear.type = "button"; clear.addEventListener("click", () => $("#article-reset-btn").click());
  bar.append(clear);
}

function renderArticles(data, resetLimit = true) {
  if (feedStates.get("article") !== "ready") return;
  if (data) currentArticleData = data;
  if (data?.vietnamese_summary?.top_assets) renderAssetFilters(data.vietnamese_summary.top_assets);
  // The API applies all filters before bounding the page.
  const root = $("#article-grid"); root.replaceChildren();
  const window = currentArticleData.date_window;
  const coverage = window ? ` Cửa sổ API ${window.date_from || "…"} → ${window.date_to || "…"} · phủ ${researchCoverage?.status || "partial"}.` : "";
  const metricCoverage = currentArticleData.engagement_coverage;
  const known = metricCoverage ? ` Known likes ${metricCoverage.metrics.likes.known_n}/${metricCoverage.sample_n}, comments ${metricCoverage.metrics.comments.known_n}/${metricCoverage.sample_n}, shares ${metricCoverage.metrics.shares.known_n}/${metricCoverage.sample_n}; complete interactions ${metricCoverage.interactions_complete_n}/${metricCoverage.sample_n}.` : "";
  const viCount = currentArticleData.vietnamese_summary?.vi_native_count ?? 0;
  const items = currentArticleData.items || [];
  const basis = $("#article-controls").elements.basis.value;
  const summary = currentArticleData.research_summary;
  const sourceCounts = summary ? ` · ${fmt(summary.quoted)} có câu nguồn · ${fmt(summary.condition)} có kích hoạt/vô hiệu${basis === "all" ? "" : ` / ${fmt(summary.total)} bài trước lọc căn cứ`}` : "";
  $("#article-sample").textContent = `${fmt(currentArticleData.sample_count ?? currentArticleData.pagination?.total ?? items.length)} bài ${savedFilterActive ? "đã lưu " : ""}khớp · ${fmt(viCount)} tiếng Việt${sourceCounts}.`;
  if ($("#article-coverage-note")) $("#article-coverage-note").textContent = `Quan sát dữ liệu: ${when(currentArticleData.as_of)}.${coverage}${known} Luận điểm & điều kiện trước chỉ xét mốc kích hoạt/vô hiệu trích được, rồi câu nguồn, rồi ngày gốc; không xếp hạng độ đúng hoặc lợi nhuận. Bộ trích điều kiện theo quy tắc hiện tại chưa bao phủ mọi ngôn ngữ; thiếu mốc không có nghĩa toàn bộ bài gốc không nêu.`;
  renderActiveArticleFilterBar();
  if (!items.length) {
    const empty = el("div", "empty");
    empty.append(el("p", "", savedFilterActive
      ? savedBookmarks.size ? "Không có bài đã lưu còn được phép công khai khớp tiêu chí." : "Bạn chưa lưu bài nào. Bấm Lưu bài trên thẻ để thêm vào tủ sách."
      : basis === "condition" ? "Chưa có câu nguồn trích được mốc kích hoạt hoặc vô hiệu trong phạm vi này. Không đồng nghĩa tác giả không có phương pháp."
      : basis === "quoted" ? "Chưa có luận điểm từ thân bài đủ quyền trích trong phạm vi này. Bạn vẫn có thể đọc các bài như nguồn bối cảnh."
      : "Không có bài công khai khớp bộ lọc."));
    const broaden = el("button", "inspect-btn", basis === "condition" && summary?.quoted ? "Xem bài có câu nguồn" : basis !== "all" ? "Bỏ riêng bộ lọc căn cứ" : "Đặt lại bộ lọc");
    broaden.type = "button";
    broaden.addEventListener("click", () => {
      if (basis === "all") $("#article-reset-btn").click();
      else { $("#article-controls").elements.basis.value = basis === "condition" && summary?.quoted ? "quoted" : "all"; refreshArticlesFromControls(false); }
    });
    empty.append(broaden); root.append(empty);
    renderPaginationBar("#article-pagination", currentArticleData.pagination, offset => refreshArticlesFromControls(false, offset));
    return;
  }
  const visibleItems = items;
  for (const item of visibleItems) {
    const vi = item.vi_intel || {};
    const postKey = String(item.post_id || item.url.split("/").pop());
    const card = el("article", `panel article-card ${vi.is_vietnamese ? "vi-native-card" : ""}`);
    const meta = el("div", "article-meta");
    const authorBtn = el("button", "author-chip-btn", `👤 ${item.author}`);
    authorBtn.type = "button";
    authorBtn.title = "Mở hồ sơ kiểm toán 3 trụ cột của tác giả này";
    authorBtn.addEventListener("click", () => openDossierDrawer(item.entity_slug));
    meta.append(
      el("span", vi.is_vietnamese ? "badge-vi" : "badge-lang", `Binance Square · ${vi.is_vietnamese ? "Tiếng Việt gốc" : (vi.language_label_vi || "Đa ngôn ngữ").split(" (")[0]}`),
      authorBtn,
      el("span", "", item.published_at ? when(item.published_at) : "Ngày gốc: chưa biết"),
      el("span", "", `v${item.version}`)
    );
    const badges = el("div", "article-badges");
    for (const ticker of (vi.assets || []).slice(0, 3)) badges.append(el("span", "badge-asset", `$${ticker}`));
    for (const topicLabel of (vi.topic_labels_vi || []).slice(0, 2)) badges.append(el("span", "badge-topic", topicLabel));
    if (item.research?.quote) badges.append(el("span", "badge-basis", item.research.trigger || item.research.invalidation ? "Có mốc kích hoạt / vô hiệu" : "Có câu nguồn · chưa trích điều kiện"));
    else badges.append(el("span", "badge-topic", "Bối cảnh · chưa có câu nguồn tách riêng"));
    const heading = el("h3");
    const link = el("a", "", item.title);
    const permalink = new URL(location.href); permalink.searchParams.delete("dossier"); permalink.searchParams.set("article", postKey);
    link.href = permalink.href;
    link.addEventListener("click", (ev) => {
      if (!ev.ctrlKey && !ev.metaKey && !ev.shiftKey) {
        ev.preventDefault();
        openArticleDrawer(postKey);
      }
    });
    heading.append(link);
    const viBox = el("div", "vi-digest-box", vi.vi_summary || item.excerpt);
    if (vi.vi_usage_note) viBox.append(el("small", "", `Khuyến nghị sử dụng: ${vi.vi_usage_note}`));
    const excerptP = el("p", "", `Trích đoạn gốc: "${item.excerpt}"`);
    const quote = String(item.research?.quote || item.excerpt || "");
    const preview = el("blockquote", "article-quote", `“${quote.slice(0, 280)}${quote.length > 280 ? "…" : ""}”`);
    preview.append(el("small", "quote-label", item.research?.quote ? "Câu nguồn trích được · chưa thẩm định độ đúng" : "Trích đoạn lưu trong kho"));
    const provenance = el("details", "article-provenance");
    provenance.append(el("summary", "", "Nguồn, nhãn từ khóa & số liệu"));
    provenance.append(viBox, excerptP, el("p", "", `Nhãn từ khóa, không phải tóm tắt hay bản dịch. Tài sản: ${(vi.assets || []).join(", ") || "Chưa có"} · Chủ đề: ${(vi.topic_labels_vi || []).join(", ") || "Chưa có"}.`));
    const metrics = el("div", "engagement-row");
    metrics.append(
      el("span", "", `Likes ${fmt(item.engagement.likes)}`),
      el("span", "", `Comments ${fmt(item.engagement.comments)}`),
      el("span", "", `Shares ${fmt(item.engagement.shares)}`),
      el("span", "", `Views ${fmt(item.engagement.views)}`),
      el("span", "", `Tổng 3 metric ${fmt(item.total_interactions)}`),
      el("span", "", `snapshot ${when(item.engagement_observed_at)}`)
    );
    const actions = el("div", "article-actions");
    const inspectBtn = el("button", "inspect-btn", "Đọc & chọn bằng chứng");
    inspectBtn.type = "button";
    inspectBtn.addEventListener("click", () => openArticleDrawer(postKey));
    const isSaved = savedBookmarks.has(postKey);
    const saveBtn = el("button", `bookmark-btn ${isSaved ? "saved" : ""}`, isSaved ? "📌 Đã lưu" : "📌 Lưu bài");
    saveBtn.type = "button";
    saveBtn.addEventListener("click", () => {
      const before = new Set(savedBookmarks);
      if (savedBookmarks.has(postKey)) savedBookmarks.delete(postKey); else savedBookmarks.add(postKey);
      if (!persistBookmarks(before)) savedBookmarks = before;
      if (savedFilterActive) refreshArticlesFromControls(false);
      else renderArticles(null, false);
    });
    const extLink = el("a", "", "Mở nguồn gốc ↗");
    extLink.href = item.citation?.source_url || item.url;
    extLink.target = "_blank";
    extLink.rel = "noopener";
    actions.append(inspectBtn, saveBtn, extLink);
    provenance.append(metrics);
    card.append(meta, heading, preview, badges, actions, provenance);
    root.append(card);
  }
  renderPaginationBar("#article-pagination", currentArticleData.pagination, offset => refreshArticlesFromControls(false, offset));
}

function closeDrawer(updateURL = true) {
  drawerReqSeq++;
  const backdrop = $("#drawer-backdrop");
  if (backdrop) backdrop.hidden = true;
  if (updateURL) readerURL(null);
  if (drawerReturnFocus?.getClientRects().length) drawerReturnFocus.focus();
}

async function showAuthorArticles(slug) {
  closeDrawer();
  resetArticleControls();
  savedFilterActive = false;
  $("#article-controls").elements.entity.value = slug;
  await refreshArticlesFromControls(true);
}

async function openArticleDrawer(postKey, updateURL = true) {
  const cleanKey = String(postKey);
  const seq = ++drawerReqSeq;
  const fromFollowing = !$("#moi").closest("[data-page]")?.hidden;
  navigateToSection(fromFollowing ? "moi" : "bai-viet", {history: false, scroll: false});
  if ($("#drawer-backdrop").hidden) drawerReturnFocus = document.activeElement;
  if (updateURL) readerURL("article", cleanKey);
  const backdrop = $("#drawer-backdrop");
  const body = $("#drawer-body");
  $("#drawer-eyebrow").textContent = "TRÍCH ĐOẠN NGUỒN · PHÂN BIỆT LỜI NGUỒN VÀ SUY LUẬN";
  $("#drawer-title").textContent = "Đang tải bài nghiên cứu…";
  body.replaceChildren(el("div", "empty", "Đang truy xuất dữ liệu kiểm toán…"));
  backdrop.hidden = false;
  $("#drawer-close").focus();
  try {
    const item = await getJSON(`/api/articles/${encodeURIComponent(cleanKey)}`);
    if (seq !== drawerReqSeq) return;
    const vi = item.vi_intel || {};
    $("#drawer-title").textContent = item.title;
    body.replaceChildren();

    const list = (fromFollowing ? newArticleData : currentArticleData).items || [];
    const idx = list.findIndex(x => String(x.post_id || x.url.split("/").pop()) === cleanKey);
    const navBar = el("div", "drawer-nav-bar");
    const prevBtn = el("button", "bookmark-btn", "← Bài trước");
    prevBtn.type = "button";
    prevBtn.disabled = idx <= 0;
    if (idx > 0) {
      const prevKey = list[idx - 1].post_id || list[idx - 1].url.split("/").pop();
      prevBtn.addEventListener("click", () => openArticleDrawer(prevKey));
    }
    const isSaved = savedBookmarks.has(cleanKey);
    const saveBtn = el("button", `bookmark-btn ${isSaved ? "saved" : ""}`, isSaved ? "📌 Đã lưu trong tủ sách" : "📌 Lưu bài này");
    saveBtn.type = "button";
    saveBtn.addEventListener("click", () => {
      const before = new Set(savedBookmarks);
      if (savedBookmarks.has(cleanKey)) savedBookmarks.delete(cleanKey); else savedBookmarks.add(cleanKey);
      if (!persistBookmarks(before)) savedBookmarks = before;
      const saved = savedBookmarks.has(cleanKey);
      saveBtn.textContent = saved ? "📌 Đã lưu trong tủ sách" : "📌 Lưu bài này";
      saveBtn.classList.toggle("saved", saved);
      if (savedFilterActive) refreshArticlesFromControls(false);
      else renderArticles(null, false);
    });
    const nextBtn = el("button", "bookmark-btn", "Bài tiếp theo →");
    nextBtn.type = "button";
    nextBtn.disabled = idx < 0 || idx >= list.length - 1;
    if (idx >= 0 && idx < list.length - 1) {
      const nextKey = list[idx + 1].post_id || list[idx + 1].url.split("/").pop();
      nextBtn.addEventListener("click", () => openArticleDrawer(nextKey));
    }
    const add = el("button", "inspect-btn", "Thêm vào nghiên cứu"); add.type = "button";
    add.addEventListener("click", () => {
      if (!BTIWorkspace.activeId()) closeDrawer();
      if (BTIWorkspace.addArticle(item)) {
        add.textContent = "Đã thêm vào nghiên cứu";
        add.setAttribute("aria-live", "polite");
      }
    });
    const share = el("a", "inspect-btn", "Permalink bài viết"); share.href = location.href;
    navBar.append(prevBtn, saveBtn, nextBtn, add, share);
    body.append(navBar);


    const excerptSec = el("section", "drawer-section");
    excerptSec.append(
      el("h4", "", "1. Nguồn nói gì?"),
      el("blockquote", "article-quote", `“${item.excerpt}”`),
      el("small", "", `Tác giả: ${item.author} (@${item.username || item.entity_slug}) · Xuất bản gốc: ${when(item.published_at)} · Thu thập: ${when(item.fetched_at)} · Phiên bản v${item.version} (${item.body_chars} ký tự) · SHA-256: ${String(item.checksum || "").slice(0, 20)}…`)
    );
    const original = el("a", "content-link", "Đọc nguồn gốc ↗");
    original.href = item.citation?.source_url || item.url; original.target = "_blank"; original.rel = "noopener";
    excerptSec.append(original);
    body.append(excerptSec);

    const claimSec = el("section", "drawer-section");
    claimSec.append(el("h4", "", "2. Câu nguồn & điều kiện trích được"));
    const brief = item.research;
    if (brief?.quote) {
      if (brief.quote !== item.excerpt) claimSec.append(el("blockquote", "article-quote", `“${brief.quote}”`));
      const fields = el("dl", "source-condition-grid");
      for (const [key, label] of [["asset", "Tài sản"], ["horizon", "Khung thời gian"], ["trigger", "Kích hoạt"], ["invalidation", "Vô hiệu"]]) {
        const row = el("div");
        row.append(el("dt", "", label), el("dd", "", brief[key] || "Chưa trích được"));
        fields.append(row);
      }
      claimSec.append(fields, el("p", "sample-note", "Các trường chỉ mô tả câu nguồn được phép trích, không phải toàn bộ hệ thống giao dịch. Thiếu trường không có nghĩa tác giả không nêu ở nơi khác; bộ trích theo quy tắc chưa bao phủ mọi ngôn ngữ."));
    } else {
      claimSec.append(el("p", "", "Chưa có câu nguồn từ thân bài đủ quyền trích làm luận điểm trong phiên bản này. Có thể dùng bài như bối cảnh; chưa đủ căn cứ gán phương pháp cho tác giả."));
    }
    claimSec.append(el("p", "sample-note", "Khi thêm nguồn vào nghiên cứu, bản lưu giữ trích đoạn của phiên bản. Các quy tắc bạn tự viết là đề xuất của bạn, không tự động trở thành lời tác giả."));
    body.append(claimSec);

    const evSec = el("section", "drawer-section");
    evSec.append(el("h4", "", `3. Bằng chứng & giới hạn (${(item.evidence || []).length})`));
    if (item.evidence?.length) {
      const ul = el("ul", "drawer-list");
      for (const ev of item.evidence) {
        ul.append(el("li", "", `${ev.pillar_label_vi}: ${ev.value_summary_vi} · Cấp: ${ev.verification_label_vi} · Giới hạn: ${ev.limitations}`));
      }
      evSec.append(ul);
    }
    const drawerActions = el("div", "article-actions");
    const openAuthorBtn = el("button", "inspect-btn", `👤 Xem hồ sơ 3 trụ cột của ${item.author} →`);
    openAuthorBtn.type = "button";
    openAuthorBtn.addEventListener("click", () => openDossierDrawer(item.entity_slug));
    const filterAuthorBtn = el("button", "bookmark-btn", `🔍 Lọc tất cả bài của ${item.author}`);
    filterAuthorBtn.type = "button";
    filterAuthorBtn.addEventListener("click", () => showAuthorArticles(item.entity_slug));
    drawerActions.append(openAuthorBtn, filterAuthorBtn);
    evSec.append(drawerActions);
    body.append(evSec);
    const support = el("details", "drawer-section reader-secondary");
    support.append(el("summary", "", "Nhãn từ khóa & bản ghi kiểm toán"));
    support.append(el("p", "", vi.vi_summary || item.excerpt), el("p", "", `Nhãn hỗ trợ đọc, không phải bản dịch hoặc thẩm định. ${vi.vi_usage_note || ""}`));
    const checks = el("ul", "drawer-list");
    for (const signal of (vi.discipline_checklist?.signals_vi || [])) checks.append(el("li", "", signal));
    if (vi.discipline_checklist?.extracted_numbers?.length) checks.append(el("li", "", `Con số trong trích đoạn, không tự suy diễn thành lệnh: ${vi.discipline_checklist.extracted_numbers.join(", ")}`));
    for (const claim of (item.claims || [])) checks.append(el("li", "", `"${claim.statement}" · ${claim.confidence_label || "Trích xuất xác định"}`));
    support.append(checks, el("small", "", `Ngôn ngữ: ${vi.language_label_vi || item.language} · Nguồn: ${vi.provenance_label_vi || item.provenance_type} · Cách trích: ${brief?.method || "Chưa có"} · ${brief?.reason || ""}`));
    body.append(support);
  } catch (err) {
    if (seq === drawerReqSeq) sectionError("#drawer-body", () => openArticleDrawer(cleanKey, false), "Không tải được bài hoặc nguồn không còn công khai.");
  }
}

async function openDossierDrawer(slug, updateURL = true) {
  const seq = ++drawerReqSeq;
  navigateToSection("ho-so", {history: false, scroll: false});
  if ($("#drawer-backdrop").hidden) drawerReturnFocus = document.activeElement;
  if (updateURL) readerURL("dossier", slug);
  const backdrop = $("#drawer-backdrop");
  const body = $("#drawer-body");
  $("#drawer-eyebrow").textContent = "HỒ SƠ CHỦ THỂ · BA NHÓM BẰNG CHỨNG RIÊNG";
  $("#drawer-title").textContent = "Đang tải hồ sơ nguồn…";
  body.replaceChildren(el("div", "empty", "Đang truy xuất dữ liệu 3 trụ cột…"));
  backdrop.hidden = false; $("#drawer-close").focus();
  try {
    const windowQuery = new URLSearchParams(queryFromForm($("#dossier-controls")));
    const item = await getJSON(`/api/dossiers/${encodeURIComponent(slug)}?${windowQuery}`);
    if (seq !== drawerReqSeq) return;
    $("#drawer-title").textContent = `${item.display_name} (@${item.username || item.slug})`;
    body.replaceChildren();

    const idSec = el("section", "drawer-section");
    const follow = el("button", "inspect-btn", "Theo dõi tác giả này"); follow.type = "button";
    follow.addEventListener("click", () => { BTIWorkspace.watchAuthor(item.slug, item.display_name); closeDrawer(); navigateToSection("moi", {focus: true}); });
    const filterPostsBtn = el("button", "inspect-btn", `Tìm bài của ${item.display_name} →`);
    filterPostsBtn.type = "button";
    filterPostsBtn.addEventListener("click", () => showAuthorArticles(item.slug));
    idSec.append(
      el("h4", "", "1. Phân loại & Liên kết Danh tính"),
      el("p", "", item.vi_profile?.category_label_vi || item.category),
      el("p", "", item.vi_profile?.audit_verdict_vi || item.selection_note),
      el("small", "", `Trạng thái định danh: ${item.identity?.status} — ${item.identity?.note}`),
      filterPostsBtn, follow
    );
    body.append(idSec);

    const pillarSec = el("section", "drawer-section");
    pillarSec.append(el("h4", "", "2. Chi tiết 3 Trụ cột Bằng chứng Độc lập"));
    const pList = el("ul", "drawer-list");
    for (const key of ["engagement", "performance", "recognition"]) {
      const grp = item.pillars[key];
      pList.append(el("li", "", `${labels[key]}: ${pillarSummary(grp)} (${grp.verification_level.toUpperCase()}) — ${grp.limitations}`));
    }
    pillarSec.append(pList);
    body.append(pillarSec);

    const contentSec = el("section", "drawer-section");
    contentSec.append(el("h4", "", `3. Bài viết công khai gần nhất (${(item.contents || []).length})`));
    if (item.contents?.length) {
      for (const post of item.contents) {
        const box = el("div", "vi-digest-box");
        box.append(el("strong", "", post.title), el("small", "", post.vi_intel?.vi_summary || post.excerpt));
        if (post.post_id) {
          const btn = el("button", "inspect-btn", "Xem trích đoạn & bằng chứng →");
          btn.type = "button";
          btn.style.marginTop = "6px";
          btn.addEventListener("click", () => openArticleDrawer(post.post_id));
          box.append(btn);
        }
        contentSec.append(box);
      }
    }
    body.append(contentSec);
  } catch (err) {
    if (seq === drawerReqSeq) sectionError("#drawer-body", () => openDossierDrawer(slug, false), "Không tải được hồ sơ hoặc nguồn không còn công khai.");
  }
}

function queryFromForm(form) {
  const params = new URLSearchParams();
  for (const [key,value] of new FormData(form).entries()) if (String(value).trim()) params.set(key, String(value).trim());
  return params.toString();
}

function utcDateYearsAgo(value, years) {
  const year = value.getUTCFullYear() - years;
  const month = value.getUTCMonth();
  const lastDay = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
  const day = Math.min(value.getUTCDate(), lastDay);
  return new Date(Date.UTC(year, month, day)).toISOString().slice(0, 10);
}

function resetArticleControls() {
  const form = $("#article-controls");
  form.reset();
  // Hidden inputs update defaultValue when assigned, so reset alone keeps a stale identity.
  form.elements.entity.value = "";
  const now = new Date();
  form.elements.date_from.value = utcDateYearsAgo(now, 2);
  form.elements.date_to.value = now.toISOString().slice(0, 10);
}

function syncDossierCriteria() {
  const form = $("#dossier-controls");
  let primaryMatch = false;
  for (const button of document.querySelectorAll("#dossier-category-pills .filter-chip")) {
    const active = button.dataset.program === form.elements.program.value && button.dataset.category === form.elements.category.value;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
    if (active && !button.closest(".cohort-more")) primaryMatch = true;
    if (active && button.closest(".cohort-more")) button.closest(".cohort-more").open = true;
  }
  if (!primaryMatch || form.elements.year.value || form.elements.sort.value !== "official_rank") $("#dossier-extra-filters").open = true;
}

function initializeDossierWindow() {
  const form = $("#dossier-controls");
  const selector = form.elements.window;
  const fields = [...form.querySelectorAll(".custom-window-field")];
  const now = new Date();
  const start = new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000);
  form.elements.date_from.value = start.toISOString().slice(0, 10);
  form.elements.date_to.value = now.toISOString().slice(0, 10);
  const sync = () => {
    const custom = selector.value === "custom";
    for (const field of fields) {
      field.hidden = !custom;
      field.querySelector("input").disabled = !custom;
    }
  };
  if (!form.dataset.windowReady) { selector.addEventListener("change", sync); form.dataset.windowReady = "1"; }
  sync();
  return queryFromForm(form);
}

const feedRequests = new Map();

async function loadList(name, url, render, retry) {
  const previous = feedRequests.get(name);
  if (previous?.url === url && ["loading", "ready"].includes(feedStates.get(name))) return previous.promise;
  const request = {url};
  feedRequests.set(name, request);
  listState(name, "loading");
  request.promise = (async () => {
    try {
      const data = await getJSON(url);
      if (feedRequests.get(name) !== request) return;
      feedStates.set(name, "ready");
      render(data);
      if (name === "article") headerState("Bài viết đã tải · Nguồn công khai, chưa thẩm định kỹ năng");
    } catch (_e) {
      if (feedRequests.get(name) !== request) return;
      listState(name, "error");
      sectionError(name === "evidence" ? "#evidence-body" : `#${name}-grid`, retry);
    } finally {
      if (feedRequests.get(name) === request) $(name === "evidence" ? "#evidence-body" : `#${name}-grid`).removeAttribute("aria-busy");
    }
  })();
  return request.promise;
}

async function refreshArticlesFromControls(scrollToFeed = false, offset = 0, updateURL = true) {
  const form = $("#article-controls");
  if (scrollToFeed) navigateToSection("bai-viet", {load: false, behavior: "smooth"});
  if (updateURL) syncFilterURL("a", form, offset);
  renderActiveArticleFilterBar();
  const query = new URLSearchParams(queryFromForm(form));
  query.set("limit", "12"); query.set("offset", String(offset));
  if (savedFilterActive) query.set("post_ids", [...savedBookmarks].join(",") || "0");
  return loadList("article", `/api/articles?${query}`, renderArticles, () => refreshArticlesFromControls(false, offset, false));
}

async function refreshDossiersFromControls(scrollToSection = false, offset = 0, updateURL = true) {
  const form = $("#dossier-controls");
  if (scrollToSection) navigateToSection("ho-so", {load: false, behavior: "smooth"});
  if (updateURL) syncFilterURL("d", form, offset);
  const query = new URLSearchParams(queryFromForm(form));
  query.set("limit", "12"); query.set("offset", String(offset));
  return loadList("dossier", `/api/dossiers?${query}`, renderDossiers, () => refreshDossiersFromControls(false, offset, false));
}

async function refreshEvidence(offset = 0) {
  const query = new URLSearchParams({
    q: $("#evidence-q").value.trim(), pillar: $("#evidence-pillar-filter").value,
    level: $("#evidence-level-filter").value, limit: "20", offset: String(offset)
  });
  return loadList("evidence", `/api/evidence?${query}`, renderEvidence, () => refreshEvidence(offset));
}

async function refreshOverview() {
  $("#metrics").replaceChildren(el("p", "empty", "Đang tải tổng quan…"));
  try {
    const data = await getJSON("/api/overview");
    researchCoverage = data.research_coverage;
    renderOverview(data); renderWorker(data.worker); renderScannerArchitecture(data.scanner);
  } catch (_e) {
    headerState("Tổng quan chưa tải được · Tra cứu bài vẫn hoạt động", "warn");
    sectionError("#metrics", refreshOverview, "Chưa lấy được tổng quan. Không ảnh hưởng dữ liệu bài đã tải.");
    $("#goal-summary-text").textContent = "Chưa tải được tiến độ; không suy số liệu từ dữ liệu cũ.";
  }
}

async function refreshDrafts() {
  try { renderDrafts((await getJSON("/api/drafts")).items); }
  catch (_e) { sectionError("#draft-grid", refreshDrafts); }
}

async function refreshSources() {
  try { renderSources((await getJSON("/api/sources")).items); }
  catch (_e) { sectionError("#source-grid", refreshSources); }
}

async function refreshNew(offset = 0) {
  const seq = ++newReqSeq, form = $("#new-controls");
  const mode = form.elements.mode.value;
  const requestedControls = new URLSearchParams(new FormData(form)).toString();
  const watch = BTIWorkspace.watches().find(x => x.id === form.elements.watch.value);
  const query = new URLSearchParams({limit: "12", offset: String(offset), sort: mode === "published" ? "latest" : "discovered"});
  if (watch?.entity) query.set("entity", watch.entity);
  if (watch?.topic) query.set("topic", watch.topic);
  if (watch?.asset) query.set("asset", watch.asset);
  if (form.elements.quality_only.value) query.set("quality_only", "1");
  if (mode === "published") query.set("date_from", new Date(Date.now() - 30 * 86400000).toISOString().slice(0, 10));
  else if (mode === "collected24h") query.set("seen_after", new Date(Date.now() - 86400000).toISOString());
  else if (watch?.checked_at) query.set("seen_after", watch.checked_at);
  const requestedAt = new Date().toISOString();
  const keyParams = new URLSearchParams(query); keyParams.delete("offset");
  const key = keyParams.toString();
  if (!offset || key !== newFeedKey) { newVisitedPages = new Set(); newSnapshotAt = requestedAt; newFeedKey = key; }
  listState("new", "loading");
  try {
    const data = await getJSON(`/api/articles?${query}`);
    if (seq !== newReqSeq || requestedControls !== new URLSearchParams(new FormData(form)).toString()) return;
    feedStates.set("new", "ready");
    newArticleData = data; newVisitedPages.add(data.pagination?.offset ?? offset); newLoadedWatchId = watch?.id || "";
    newLoadedMode = mode;
    const clockLabel = mode === "published" ? "Theo ngày xuất bản gốc, 30 ngày gần nhất"
      : mode === "collected24h" ? "Theo thời điểm vào kho trong 24 giờ, không phải số bài mới xuất bản"
      : `Theo ngày hệ thống biết đến bài lần đầu${watch?.checked_at ? `, sau ${when(watch.checked_at)}` : ""}`;
    $("#new-sample").textContent = `${fmt(data.sample_count)} bài khớp · ${clockLabel} · Phạm vi một phần. Sàng lọc kỹ thuật không phải thẩm định chuyên gia.`;
    $("#watch-mark-read").hidden = !watch || mode !== "discovered";
    const root = $("#new-grid"); root.replaceChildren();
    if (!data.items.length) root.append(el("p", "empty", "Chưa có bài khớp. Không đồng nghĩa nguồn không có nội dung mới."));
    for (const item of data.items) {
      const card = el("article", "panel article-card");
      card.append(el("h3", "", item.title), el("p", "", `${item.author} · Xuất bản: ${when(item.published_at)} · Mới biết: ${when(item.first_seen_at)}`), el("p", "", item.excerpt));
      const actions = el("div", "article-actions"), open = el("button", "inspect-btn", "Mở bài & chọn bằng chứng");
      open.type = "button"; open.addEventListener("click", () => openArticleDrawer(item.post_id));
      actions.append(open); card.append(actions); root.append(card);
    }
    renderPaginationBar("#new-pagination", data.pagination, refreshNew);
  } catch (_e) {
    if (seq === newReqSeq) { listState("new", "error"); sectionError("#new-grid", () => refreshNew(offset)); }
  } finally { if (seq === newReqSeq) $("#new-grid").removeAttribute("aria-busy"); }
}

function restoreRoute(initial = false, scroll = false) {
  const params = new URL(location.href).searchParams;
  const changed = new Set();
  for (const [prefix, selector] of [["a", "#article-controls"], ["d", "#dossier-controls"]]) {
    const identity = routeIdentity(prefix, params);
    if (routeIdentities.get(prefix) === identity) continue;
    routeIdentities.set(prefix, identity);
    changed.add(prefix);
    const form = $(selector);
    if (prefix === "a") resetArticleControls();
    else { form.reset(); initializeDossierWindow(); }
    for (const [key, value] of params) {
      if (!key.startsWith(`${prefix}.`)) continue;
      const control = form.elements.namedItem(key.slice(2));
      if (!control || (control.tagName === "SELECT" && ![...control.options].some(option => option.value === value))) continue;
      control.value = value;
    }
    if (prefix === "a") savedFilterActive = params.get("saved") === "1";
    else for (const field of form.querySelectorAll(".custom-window-field")) {
      field.hidden = form.elements.window.value !== "custom";
      field.querySelector("input").disabled = field.hidden;
    }
  }
  renderActiveArticleFilterBar();
  const kind = params.has("article") ? "article" : params.has("dossier") ? "dossier" : "";
  let id = decodeURIComponent(location.hash.slice(1)) || "bai-viet";
  if (kind === "dossier") id = "ho-so";
  else if (kind === "article" && sectionPages[id] !== "following") id = "bai-viet";
  const page = navigateToSection(id, {history: false, scroll: !initial && scroll, load: false});
  if (changed.has("a")) {
    // Invalidate a hidden page without starting its HTTP request.
    if (page !== "discover") { feedRequests.delete("article"); listState("article", "loading"); }
    else refreshArticlesFromControls(false, Number(params.get("a.offset") || 0), false);
  }
  if (changed.has("d")) {
    if (page !== "sources") { feedRequests.delete("dossier"); listState("dossier", "loading"); }
    else refreshDossiersFromControls(false, Number(params.get("d.offset") || 0), false);
  }
  navigateToSection(id, {history: false, scroll: false});
  const reader = kind ? `${kind}:${params.get(kind)}` : "";
  if (reader !== routeReader || initial) {
    routeReader = reader;
    if (kind === "article") openArticleDrawer(params.get(kind), false);
    else if (kind === "dossier") openDossierDrawer(params.get(kind), false);
    else closeDrawer(false);
  }
}

function setupVietnameseQuickControls() {
  const badge = $("#saved-count-badge"); if (badge) badge.textContent = String(savedBookmarks.size);
  const form = $("#article-controls");
  const dossierForm = $("#dossier-controls");
  const applyLang = async (langValue, scroll = true) => {
    savedFilterActive = false;
    if (form.elements.lang) form.elements.lang.value = langValue;
    document.querySelectorAll("#vi-lang-pills .filter-chip").forEach(btn => {
      btn.classList.toggle("active", btn.dataset.lang === langValue);
    });
    $("#top-vi-priority-btn")?.classList.toggle("active", langValue === "vi_first");
    await refreshArticlesFromControls(scroll);
  };
  $("#top-vi-priority-btn")?.addEventListener("click", () => applyLang("vi_first", true));
  document.querySelectorAll("#vi-lang-pills .filter-chip").forEach(btn => {
    btn.addEventListener("click", () => applyLang(btn.dataset.lang || "", true));
  });
  document.querySelectorAll("#vi-topic-pills .filter-chip").forEach(btn => {
    btn.addEventListener("click", async () => {
      savedFilterActive = false;
      const topicVal = btn.dataset.topic || "";
      if (form.elements.topic) form.elements.topic.value = topicVal;
      document.querySelectorAll("#vi-topic-pills .filter-chip").forEach(x => x.classList.toggle("active", x === btn));
      await refreshArticlesFromControls(true);
    });
  });
  $("#vi-asset-pills")?.addEventListener("click", async (event) => {
    const btn = event.target.closest(".asset-chip");
    if (!btn) return;
    savedFilterActive = false;
    const assetVal = btn.dataset.asset || "";
    if (form.elements.asset) form.elements.asset.value = assetVal;
    document.querySelectorAll("#vi-asset-pills .asset-chip").forEach(x => x.classList.toggle("active", x === btn));
    await refreshArticlesFromControls(true);
  });
  document.querySelectorAll("#dossier-category-pills .filter-chip").forEach(btn => {
    btn.addEventListener("click", async () => {
      document.querySelectorAll("#dossier-category-pills .filter-chip").forEach(x => x.classList.toggle("active", x === btn));
      if (dossierForm.elements.program) dossierForm.elements.program.value = btn.dataset.program || "";
      if (dossierForm.elements.category) dossierForm.elements.category.value = btn.dataset.category || "";
      if (dossierForm.elements.sort && (btn.dataset.program || btn.dataset.category)) {
        dossierForm.elements.sort.value = btn.dataset.category === "copy_trading_portfolio" ? "latest" : "official_rank";
      }
      await refreshDossiersFromControls(true);
    });
  });

  // Shortcuts select explicit source facts; topic keywords remain in the topic filter.
  document.querySelectorAll("#usecase-flow-grid .usecase-card").forEach(card => {
    card.addEventListener("click", async () => {
      document.querySelectorAll("#usecase-flow-grid .usecase-card").forEach(x => x.classList.toggle("active", x === card));
      const flow = card.dataset.flow;
      if (flow === "vi-native") {
        savedFilterActive = false;
        resetArticleControls();
        form.elements.lang.value = "vi";
        await refreshArticlesFromControls(true);
      } else if (flow === "source-claims") {
        savedFilterActive = false;
        form.elements.basis.value = "quoted";
        await refreshArticlesFromControls(true);
      } else if (flow === "saved-bookmarks") {
        resetArticleControls();
        savedFilterActive = true;
        await refreshArticlesFromControls(true);
      }
    });
  });

  // Instant reactive select and reset buttons
  for (const sel of form.querySelectorAll("select, input[type='date']")) {
    sel.addEventListener("change", () => { savedFilterActive = false; refreshArticlesFromControls(false); });
  }
  for (const sel of dossierForm.querySelectorAll("select, input[type='date']")) {
    sel.addEventListener("change", () => refreshDossiersFromControls(false));
  }
  let articleDebounce = null;
  for (const inp of form.querySelectorAll("input:not([type='date']):not([type='hidden'])")) {
    inp.addEventListener("input", () => {
      savedFilterActive = false;
      form.elements.entity.value = "";
      clearTimeout(articleDebounce);
      articleDebounce = setTimeout(() => refreshArticlesFromControls(false), 320);
    });
  }
  let dossierDebounce = null;
  for (const inp of dossierForm.querySelectorAll("input:not([type='date'])")) {
    inp.addEventListener("input", () => {
      clearTimeout(dossierDebounce);
      dossierDebounce = setTimeout(() => refreshDossiersFromControls(false), 320);
    });
  }
  $("#article-reset-btn")?.addEventListener("click", async () => {
    savedFilterActive = false;
    resetArticleControls();
    document.querySelectorAll("#vi-lang-pills .filter-chip").forEach(x => x.classList.toggle("active", x.dataset.lang === ""));
    document.querySelectorAll("#vi-topic-pills .filter-chip").forEach(x => x.classList.toggle("active", x.dataset.topic === ""));
    document.querySelectorAll("#vi-asset-pills .asset-chip").forEach(x => x.classList.toggle("active", x.dataset.asset === ""));
    await refreshArticlesFromControls(false);
  });
  $("#dossier-reset-btn")?.addEventListener("click", async () => {
    dossierForm.reset();
    initializeDossierWindow();
    await refreshDossiersFromControls(false);
    $("#dossier-extra-filters").open = false;
    $("#dossier-category-pills .cohort-more").open = false;
  });

  // Evidence Ledger instant filters
  let evidenceDebounce;
  $("#evidence-q")?.addEventListener("input", () => { clearTimeout(evidenceDebounce); evidenceDebounce = setTimeout(() => refreshEvidence(), 320); });
  $("#evidence-pillar-filter")?.addEventListener("change", () => refreshEvidence());
  $("#evidence-level-filter")?.addEventListener("change", () => refreshEvidence());
  $("#evidence-reset-btn")?.addEventListener("click", () => {
    if ($("#evidence-q")) $("#evidence-q").value = "";
    if ($("#evidence-pillar-filter")) $("#evidence-pillar-filter").value = "";
    if ($("#evidence-level-filter")) $("#evidence-level-filter").value = "";
    refreshEvidence();
  });

  // Compare Tray
  $("#open-compare-btn")?.addEventListener("click", openComparisonDrawer);
  $("#clear-compare-btn")?.addEventListener("click", () => {
    comparedDossiers.clear();
    updateCompareTray();
    renderDossiers(currentDossierData, false);
  });

  $("#drawer-close")?.addEventListener("click", closeDrawer);
  $("#drawer-backdrop")?.addEventListener("click", (event) => {
    if (event.target.id === "drawer-backdrop") closeDrawer();
  });
  document.addEventListener("keydown", (event) => {
    if ($("#drawer-backdrop").hidden) return;
    if (event.key === "Escape") closeDrawer();
    if (event.key === "Tab") {
      const controls = [...$("#detail-drawer").querySelectorAll("button:not([disabled]), a[href], input, select, textarea")].filter(x => x.getClientRects().length);
      const first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  });
}

async function bootResearch() {
  resetArticleControls(); initializeDossierWindow();
  setupNav(); setupVietnameseQuickControls();
  // Register actions before any remote request, so one failed section cannot disable forms.
  $("#article-controls").addEventListener("submit", event => {
    event.preventDefault(); savedFilterActive = false;
    if (feedStates.get("article") === "ready") feedRequests.delete("article");
    refreshArticlesFromControls();
  });
  $("#dossier-controls").addEventListener("submit", event => {
    event.preventDefault();
    if (feedStates.get("dossier") === "ready") feedRequests.delete("dossier");
    refreshDossiersFromControls();
  });
  $("#new-controls").addEventListener("submit", event => { event.preventDefault(); refreshNew(); });
  $("#new-controls").addEventListener("change", () => { newReqSeq++; listState("new", "changed"); });
  $("#watch-mark-read").addEventListener("click", () => {
    const page = newArticleData.pagination;
    if (feedStates.get("new") === "ready" && newLoadedWatchId && newLoadedMode === "discovered" && newSnapshotAt
        && newLoadedWatchId === $("#new-watch").value && $("#new-controls").elements.mode.value === "discovered"
        && page && newVisitedPages.size >= Math.max(1, Math.ceil(page.total / page.limit))) {
      BTIWorkspace.markRead(newLoadedWatchId, newSnapshotAt); refreshNew();
    } else alert("Hãy xem hết các trang của danh sách theo dõi đã tải trước khi đánh dấu, để không bỏ qua bài chưa xem.");
  });
  BTIWorkspace.start({getJSON, openArticle: openArticleDrawer, loadNew: refreshNew, navigate: navigateToSection});
  lazySection("bai-viet", () => refreshArticlesFromControls(false, Number(new URL(location.href).searchParams.get("a.offset") || 0), false));
  lazySection("ho-so", () => refreshDossiersFromControls(false, Number(new URL(location.href).searchParams.get("d.offset") || 0), false));
  lazySection("bang-chung", () => refreshEvidence());
  lazySection("ban-nhap", refreshDrafts);
  lazySection("nguon", refreshSources);
  lazySection("moi", () => refreshNew());
  // Operational overview is independent and only requested when More is exposed.
  lazySection("metrics", refreshOverview);
  window.addEventListener("popstate", () => restoreRoute());
  window.addEventListener("hashchange", () => restoreRoute(false, true));
  headerState("Chọn bài viết để nghiên cứu · Dữ liệu nguồn công khai");
  restoreRoute(true);
}
bootResearch().catch(() => sectionError("#article-grid", () => restoreRoute(true), "Không khởi tạo được giao diện. Có thể thử lại mà không xóa nghiên cứu riêng."));
