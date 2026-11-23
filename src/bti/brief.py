"""Permission-checked intelligence brief projection for downstream research/content tools.

Reads exclusively through public projections so live source/entity/content/claim/evidence
revocations, short-excerpt bounds and unknown-metric semantics remain enforced.
No external network calls, trading logic or automatic publishing.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .db import public_excerpt, utc_now
from .public import (
    ASSET_NAME_MAP,
    KNOWN_TICKERS,
    TOPIC_RULES,
    _aware_utc,
    _engagement_window,
    _inside_engagement_window,
    _public_window,
    articles,
    dossier_listing,
    dossiers,
)

BRIEF_SCHEMA = "bti-intelligence-brief-v1"
TOPIC_LABELS_VI = {
    **{code: label for code, label, _ in TOPIC_RULES},
    "ecosystem": "Hệ sinh thái & Thị trường",
}
STANDARD_UNKNOWNS = (
    "Independent accuracy",
    "Verified trading performance",
    "Fees, sizing and validation",
)
DISCLAIMERS_VI = [
    "Tổng hợp dữ liệu nghiên cứu công khai từ Binance Square; không phải khuyến nghị đầu tư hay tín hiệu giao dịch.",
    "Số liệu tương tác (lượt thích, bình luận, chia sẻ) là ảnh chụp tại thời điểm quan sát, không chứng minh lợi nhuận thực tế.",
    "Giải thưởng nội dung/cộng đồng (như Blockchain 100) không phải chứng nhận kiểm toán hiệu suất PnL.",
]


def _canonical_digest(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_asset(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = raw.strip().lstrip("$")
    if not cleaned:
        return None
    mapped = ASSET_NAME_MAP.get(cleaned.casefold(), cleaned.upper())
    return mapped if mapped in KNOWN_TICKERS else None


def _bounded_quote(text: str | None) -> str | None:
    if not text or not text.strip():
        return None
    return public_excerpt(text.strip(), max_words=24, max_chars=240)


def build_intelligence_brief(
    db_path: str | Path,
    *,
    window: str = "7d",
    date_from: str | None = None,
    date_to: str | None = None,
    asset: str | None = None,
    topic: str | None = None,
    lang: str = "vi_first",
    max_items: int = 6,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a bounded, citation-bearing intelligence brief from public-shareable records."""
    if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 20:
        raise ValueError("max_items must be an integer between 1 and 20")

    current = _aware_utc(now, "Brief generation time")
    window_meta = _engagement_window(
        window,
        date_from=date_from,
        date_to=date_to,
        current=current,
    )
    normalized_asset = _normalize_asset(asset)
    if asset and normalized_asset is None:
        raise ValueError(f"Unsupported asset filter: {asset}")
    normalized_topic = topic.strip().casefold() if topic and topic.strip() else None
    if normalized_topic and normalized_topic not in TOPIC_LABELS_VI:
        raise ValueError(f"Unsupported topic filter: {topic}")

    article_projection = articles(
        db_path,
        asset=normalized_asset,
        topic=normalized_topic,
        lang=lang,
        sort="total_interactions",
        date_from=window_meta["query_date_from"],
        date_to=window_meta["query_date_to"],
        now=current,
    )
    window_articles = [
        item
        for item in article_projection["items"]
        if _inside_engagement_window(item.get("published_at"), window_meta, current)
    ]

    asset_counts: dict[str, int] = {}
    topic_counts: dict[str, int] = {}
    vi_native_count = 0
    for item in window_articles:
        vi_intel = item.get("vi_intel") or {}
        if vi_intel.get("is_vietnamese"):
            vi_native_count += 1
        item_assets = set(vi_intel.get("assets") or [])
        res_asset = _normalize_asset((item.get("research") or {}).get("asset"))
        if res_asset:
            item_assets.add(res_asset)
        for ticker in sorted(item_assets):
            asset_counts[ticker] = asset_counts.get(ticker, 0) + 1
        for tcode in vi_intel.get("topics") or []:
            topic_counts[tcode] = topic_counts.get(tcode, 0) + 1

    top_assets = [
        {"asset": ticker, "article_count": count}
        for ticker, count in sorted(asset_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:8]
    ]
    top_topics = [
        {
            "topic": tcode,
            "label_vi": TOPIC_LABELS_VI.get(tcode, tcode),
            "article_count": count,
        }
        for tcode, count in sorted(topic_counts.items(), key=lambda pair: (-pair[1], pair[0]))
    ]

    notable_articles = []
    for item in window_articles[:max_items]:
        vi_intel = item.get("vi_intel") or {}
        research = item.get("research") or {}
        citation = item.get("citation") or {}
        notable_articles.append({
            "post_id": item["post_id"],
            "title": item["title"],
            "author": item["author"],
            "entity_slug": item["entity_slug"],
            "url": item["url"],
            "source_url": citation.get("source_url") or item["url"],
            "version": item["version"],
            "published_at": item["published_at"],
            "fetched_at": item["fetched_at"],
            "language": item["language"],
            "excerpt": _bounded_quote(item.get("excerpt")) or "",
            "assets": list(vi_intel.get("assets") or []),
            "topics": list(vi_intel.get("topics") or []),
            "topic_labels_vi": list(vi_intel.get("topic_labels_vi") or []),
            "engagement": dict(item.get("engagement") or {}),
            "total_interactions": item.get("total_interactions"),
            "engagement_observed_at": item.get("engagement_observed_at"),
            "research_statement": _bounded_quote(research.get("statement")),
        })

    scenario_candidates = [
        item
        for item in window_articles
        if (item.get("research") or {}).get("statement")
        and ((item.get("research") or {}).get("trigger") or (item.get("research") or {}).get("invalidation"))
    ]
    scenario_items = []
    for item in scenario_candidates[:max_items]:
        research = item["research"]
        citation = item.get("citation") or {}
        vi_intel = item.get("vi_intel") or {}
        item_asset = _normalize_asset(research.get("asset")) or (
            vi_intel["assets"][0] if vi_intel.get("assets") else None
        )
        missing = list(research.get("missing_fields") or [])
        for std_unknown in STANDARD_UNKNOWNS:
            if std_unknown not in missing:
                missing.append(std_unknown)
        scenario_items.append({
            "post_id": item["post_id"],
            "title": item["title"],
            "author": item["author"],
            "entity_slug": item["entity_slug"],
            "url": item["url"],
            "source_url": citation.get("source_url") or item["url"],
            "version": item["version"],
            "published_at": item["published_at"],
            "fetched_at": item["fetched_at"],
            "asset": item_asset,
            "horizon": research.get("horizon"),
            "trigger": research.get("trigger"),
            "invalidation": research.get("invalidation"),
            "entry_rule": research.get("entry_rule"),
            "verbatim_quote": _bounded_quote(research.get("quote") or research.get("statement")),
            "missing_fields": missing,
            "engagement": dict(item.get("engagement") or {}),
            "total_interactions": item.get("total_interactions"),
        })

    articles_by_author: dict[str, list[dict[str, Any]]] = {}
    for item in sorted(window_articles, key=lambda row: row["published_at"], reverse=True):
        articles_by_author.setdefault(item["entity_slug"], []).append(item)

    ranked_authors = dossier_listing(
        db_path,
        window=window,
        date_from=date_from,
        date_to=date_to,
        sort="interactions_total",
        now=current,
    )["items"] if window_articles else []
    full_dossiers = {
        entry["slug"]: entry
        for entry in dossiers(db_path, include_official=False, lightweight=False)
    } if ranked_authors else {}

    spotlight_items = []
    for ranked in ranked_authors:
        eng_win = ranked.get("engagement_window") or {}
        matching_articles = articles_by_author.get(ranked["slug"])
        if not matching_articles or (eng_win.get("sample_n") or 0) <= 0:
            continue
        detail = full_dossiers.get(ranked["slug"], ranked)
        pillars = detail.get("pillars") or {}
        perf_pillar = pillars.get("performance") or {}
        rec_pillar = pillars.get("recognition") or {}
        eng_pillar = pillars.get("engagement") or {}
        perf_levels = list(perf_pillar.get("verification_levels") or [])
        has_platform_pnl = any(level in {"platform", "official"} for level in perf_levels)
        recent_titles = [
            {key: article[key] for key in ("post_id", "title", "url", "published_at", "version")}
            for article in matching_articles[:3]
        ]
        spotlight_items.append({
            "slug": detail["slug"],
            "display_name": detail["display_name"],
            "username": detail.get("username"),
            "profile_url": detail.get("profile_url"),
            "category": detail["category"],
            "category_label_vi": detail.get("category_label_vi"),
            "engagement_window": {
                "sample_n": eng_win.get("sample_n", 0),
                "totals": dict(eng_win.get("totals") or {}),
                "averages": dict(eng_win.get("averages") or {}),
                "known_counts": dict(eng_win.get("known_counts") or {}),
                "unknown_counts": dict(eng_win.get("unknown_counts") or {}),
                "as_of": eng_win.get("as_of"),
            },
            "three_pillars": {
                "engagement": {
                    "status": eng_pillar.get("status", "unknown"),
                    "verification_levels": list(eng_pillar.get("verification_levels") or []),
                },
                "performance": {
                    "status": perf_pillar.get("status", "unknown"),
                    "verification_levels": perf_levels,
                    "has_platform_verified_pnl": has_platform_pnl,
                    "summary_vi": (
                        "Có dữ liệu hiệu suất công khai cấp nền tảng trong cửa sổ quan sát."
                        if has_platform_pnl
                        else "Chưa có bằng chứng hiệu suất giao dịch (PnL/ROI) xác thực cấp nền tảng."
                    ),
                },
                "recognition": {
                    "status": rec_pillar.get("status", "unknown"),
                    "award_category": detail.get("award_category"),
                    "award_rank": detail.get("award_rank"),
                    "award_votes": detail.get("award_votes"),
                    "interpretation_limit_vi": detail.get("selection_note"),
                },
            },
            "audit_verdict_vi": (detail.get("vi_profile") or {}).get("audit_verdict_vi"),
            "recent_articles": recent_titles,
        })
        if len(spotlight_items) >= max_items:
            break

    allowed_assets_set = set(asset_counts.keys())
    for sc in scenario_items:
        if sc.get("asset"):
            allowed_assets_set.add(sc["asset"])
    if normalized_asset:
        allowed_assets_set.add(normalized_asset)

    market_digest_body = {
        "eligible": bool(notable_articles),
        "sample_count": len(window_articles),
        "vi_native_count": vi_native_count,
        "as_of": max(
            (item.get("engagement_observed_at") or item.get("fetched_at") or "" for item in window_articles),
            default="",
        ) or None,
        "top_assets": top_assets,
        "top_topics": top_topics,
        "notable_articles": notable_articles,
    }
    market_digest_fp = _canonical_digest({
        "top_assets": top_assets,
        "top_topics": top_topics,
        "articles": [
            (a["post_id"], a["version"], a["total_interactions"], a["research_statement"])
            for a in notable_articles
        ],
    })

    scenarios_fp = _canonical_digest([
        (s["post_id"], s["version"], s["asset"], s["trigger"], s["invalidation"])
        for s in scenario_items
    ])
    conditional_scenarios_body = {
        "eligible": bool(scenario_items),
        "sample_count": len(scenario_candidates),
        "items": scenario_items,
    }

    spotlights_fp = _canonical_digest([
        (
            sp["slug"],
            sp["engagement_window"]["sample_n"],
            sp["engagement_window"]["totals"],
            sp["three_pillars"]["performance"]["status"],
            sp["three_pillars"]["recognition"]["status"],
        )
        for sp in spotlight_items
    ])
    creator_spotlights_body = {
        "eligible": bool(spotlight_items),
        "sample_count": len(spotlight_items),
        "items": spotlight_items,
    }

    filters_obj = {
        "asset": normalized_asset,
        "topic": normalized_topic,
        "lang": lang,
        "max_items": max_items,
    }
    brief_id = _canonical_digest({
        "schema": BRIEF_SCHEMA,
        "window_mode": window_meta["mode"],
        "filters": filters_obj,
        "section_fingerprints": {
            "market_digest": market_digest_fp,
            "conditional_scenarios": scenarios_fp,
            "creator_spotlights": spotlights_fp,
        },
    })

    return {
        "schema": BRIEF_SCHEMA,
        "brief_id": brief_id,
        "generated_at": utc_now(),
        "window": _public_window(window_meta),
        "filters": filters_obj,
        "allowed_assets": sorted(allowed_assets_set),
        "policy_contract": {
            "read_only_projection": True,
            "no_auto_publish_in_bti": True,
            "max_excerpt_words": 24,
            "max_excerpt_chars": 240,
            "engagement_is_not_pnl": True,
            "awards_are_not_audited_performance": True,
            "unknown_metrics_preserved": True,
            "disclaimers_vi": list(DISCLAIMERS_VI),
        },
        "sections": {
            "market_digest": {
                "fingerprint": market_digest_fp,
                **market_digest_body,
            },
            "conditional_scenarios": {
                "fingerprint": scenarios_fp,
                **conditional_scenarios_body,
            },
            "creator_spotlights": {
                "fingerprint": spotlights_fp,
                **creator_spotlights_body,
            },
        },
    }
