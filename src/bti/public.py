from __future__ import annotations

import copy
import functools
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .crawler_policy import public_crawler_status
from .content_volume import QUALITY_GATE_VERSION, content_volume_status
from .db import PILLARS, SCHEMA_VERSION, connect, json_load, public_excerpt, utc_now
from .scanner import scanner_public_status
from .research import discovery_research, missing_public_research, permitted_source_claims


UNKNOWN = {
    "status": "unknown",
    "label": "Chưa có bằng chứng",
    "verification_level": "unknown",
    "limitations": "Nguồn công khai đã ingest chưa cung cấp bằng chứng cho trụ cột này.",
    "items": [],
}

VI_STRICT_DIACRITIC_RE = re.compile(
    r"[ăđơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ]",
    re.IGNORECASE,
)


@functools.lru_cache(maxsize=512)
def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(keyword)}(?!\w)")


def _has_keyword(folded_text: str, keyword: str) -> bool:
    kw = keyword.strip().casefold()
    if not kw or kw not in folded_text:
        return False
    return bool(_keyword_pattern(kw).search(folded_text))
KNOWN_TICKERS = (
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "TON", "SUI", "AVAX",
    "LINK", "PEPE", "ARB", "OP", "APT", "DOT", "SHIB", "LTC", "TRX", "FIL",
    "NEAR", "UNI", "AAVE", "FET", "WLD", "ENA", "TIA", "INJ", "SEI", "PENDLE",
)
UNAMBIGUOUS_BARE_TICKERS = (
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "SUI", "AVAX",
    "PEPE", "ARB", "SHIB", "LTC", "TRX", "FIL", "AAVE", "FET", "WLD", "PENDLE",
)
ASSET_NAME_MAP = {
    "bitcoin": "BTC", "ethereum": "ETH", "bnb chain": "BNB", "binance coin": "BNB",
    "solana": "SOL", "cardano": "ADA", "dogecoin": "DOGE",
    "toncoin": "TON", "filecoin": "FIL",
    "chainlink": "LINK", "polkadot": "DOT", "litecoin": "LTC", "uniswap": "UNI",
}
TOPIC_RULES: list[tuple[str, str, tuple[str, ...]]] = [
    (
        "risk",
        "Quản trị rủi ro & Vốn",
        (
            "risk", "stop loss", "stop-loss", "stoploss", "invalidation", "invalidated",
            "drawdown", "sizing", "position size", "leverage", "margin", "rủi ro",
            "cắt lỗ", "quản lý vốn", "quản trị vốn", "đòn bẩy", "gồng lỗ", "bảo toàn vốn",
        ),
    ),
    (
        "structure",
        "Cấu trúc giá & Kỹ thuật",
        (
            "support", "resistance", "breakout", "liquidity", "trend", "structure",
            "chart", "rsi", "macd", "ema", "fibonacci", "orderblock", "supply", "demand",
            "hỗ trợ", "kháng cự", "xu hướng", "thanh khoản", "kỹ thuật", "khung giá", "cấu trúc",
        ),
    ),
    (
        "macro",
        "Vĩ mô & Dòng tiền",
        (
            "etf", "fed", "cpi", "fomc", "inflation", "interest rate", "institutional",
            "whale", "inflow", "outflow", "tvl", "on-chain", "onchain", "liquidation",
            "vĩ mô", "lãi suất", "lạm phát", "dòng tiền", "tổ chức", "cá voi", "thanh lý",
        ),
    ),
    (
        "strategy",
        "Chiến lược & Kỷ luật",
        (
            "strategy", "backtest", "hypothesis", "setup", "entry", "target",
            "take profit", "process", "plan", "framework", "chiến lược", "hệ thống",
            "kịch bản", "điểm vào", "chốt lời", "quy trình", "phương pháp",
        ),
    ),
    (
        "psychology",
        "Tâm lý giao dịch",
        (
            "psychology", "emotion", "fomo", "fear", "greed", "discipline", "mindset",
            "losses", "recover", "patience", "tâm lý", "kỷ luật", "cảm xúc", "thua lỗ", "kiên nhẫn",
        ),
    ),
]
LANGUAGE_NAMES_VI = {
    "vi": "Tiếng Việt",
    "en": "Tiếng Anh",
    "zh": "Tiếng Trung",
    "zh-cn": "Tiếng Trung",
    "cn": "Tiếng Trung",
    "ru": "Tiếng Nga",
    "ar": "Tiếng Ả Rập",
    "es": "Tiếng Tây Ban Nha",
    "pt": "Tiếng Bồ Đào Nha",
    "id": "Tiếng Indonesia",
    "uk": "Tiếng Ukraina",
    "de": "Tiếng Đức",
    "it": "Tiếng Ý",
    "hi": "Tiếng Hindi",
}
VERIFICATION_LABELS_VI = {
    "official": "Chính thức từ Binance (Official)",
    "platform": "Số liệu trực tiếp trên nền tảng (Platform)",
    "secondary": "Snapshot lưu trữ công khai (Secondary Archive)",
    "operator_capture": "Ảnh chụp trình duyệt công khai (Operator Capture — Chưa xác thực độc lập)",
    "self_claim": "Tác giả tự công bố (Self-Claim — Chưa kiểm chứng)",
    "unknown": "Chưa có bằng chứng (Unknown)",
}
PILLAR_LABELS_VI = {
    "engagement": "Trụ cột 1: Tương tác & Phản hồi Follower",
    "performance": "Trụ cột 2: Hiệu suất Thực tế Công khai",
    "recognition": "Trụ cột 3: Giải thưởng & Công nhận Chính thức",
}


@functools.lru_cache(maxsize=4096)
def _analyze_vietnamese_context_cached(
    title: str | None,
    excerpt: str | None,
    language: str | None = None,
    author: str | None = None,
    provenance_type: str | None = None,
) -> dict[str, Any]:
    combined = f"{title or ''} {excerpt or ''}".strip()
    folded = combined.casefold()
    raw_lang = (language or "").strip().lower()
    if raw_lang:
        is_vi = raw_lang.startswith("vi")
    else:
        is_vi = len(VI_STRICT_DIACRITIC_RE.findall(combined)) >= 2
    if is_vi:
        norm_lang = "vi"
    elif raw_lang in {"zh-cn", "cn", "zh"}:
        norm_lang = "zh"
    elif raw_lang:
        norm_lang = raw_lang.split("-")[0]
    else:
        norm_lang = "en"
    lang_name_vi = LANGUAGE_NAMES_VI.get(norm_lang, LANGUAGE_NAMES_VI.get(raw_lang, "Đa ngôn ngữ"))
    language_label_vi = (
        "Tiếng Việt (Bản gốc)"
        if is_vi
        else f"{lang_name_vi} (Gắn khung kiểm toán Tiếng Việt)"
    )

    assets: list[str] = []
    for match in re.findall(
        r"\$([A-Za-z]{2,10})\b|\{(?:future|spot)\}\(([A-Za-z]{2,10}?)(?:USDT|USDC|BUSD|FDUSD)?\)|\b([A-Za-z]{2,10})(?:/|-)?(?:USDT|USDC|FDUSD|BUSD)\b",
        combined,
    ):
        candidate = (match[0] or match[1] or match[2]).upper()
        if candidate in KNOWN_TICKERS and candidate not in assets:
            assets.append(candidate)
    for ticker in UNAMBIGUOUS_BARE_TICKERS:
        if ticker in combined and re.search(rf"\b{re.escape(ticker)}\b", combined) and ticker not in assets:
            assets.append(ticker)
    for name_key, mapped_ticker in ASSET_NAME_MAP.items():
        if _has_keyword(folded, name_key) and mapped_ticker not in assets:
            assets.append(mapped_ticker)
    assets = assets[:5]
    topics: list[str] = []
    topic_labels_vi: list[str] = []
    for code, label_vi, keywords in TOPIC_RULES:
        if any(_has_keyword(folded, keyword) for keyword in keywords):
            topics.append(code)
            topic_labels_vi.append(label_vi)
    if not topics:
        topics.append("ecosystem")
        topic_labels_vi.append("Hệ sinh thái & Thị trường")

    has_invalidation = any(
        _has_keyword(folded, token)
        for token in (
            "invalidation", "invalidated", "stop loss", "stop-loss", "stoploss",
            "cắt lỗ", "vô hiệu", "dừng lỗ", "mất mốc", "thủng vùng",
        )
    )
    has_risk_sizing = any(
        _has_keyword(folded, token)
        for token in (
            "risk", "position size", "sizing", "leverage", "drawdown",
            "rủi ro", "quản lý vốn", "quản trị vốn", "đòn bẩy", "tỷ lệ r:r", "khối lượng lệnh",
        )
    )
    has_conditional = any(
        _has_keyword(folded, token)
        for token in (
            "if", "scenario", "condition", "confirmation", "unless",
            "nếu", "kịch bản", "điều kiện", "xác nhận", "trường hợp",
        )
    )
    extracted_numbers = []
    for num_match in re.findall(r"(?:\$[0-9][0-9,.]*(?:\s*(?:k|m|b|billion|million))?|[0-9]+(?:[.,][0-9]+)?%)", combined, re.IGNORECASE):
        cleaned = num_match.strip()
        if cleaned and cleaned not in extracted_numbers:
            extracted_numbers.append(cleaned)
        if len(extracted_numbers) >= 4:
            break

    signals_vi: list[str] = []
    if has_invalidation:
        signals_vi.append("Có nêu mốc vô hiệu hóa / cắt lỗ")
    if has_risk_sizing:
        signals_vi.append("Có đề cập quản trị rủi ro / vị thế")
    if has_conditional:
        signals_vi.append("Có kịch bản điều kiện")
    if not signals_vi:
        signals_vi.append("Chưa nêu rõ mốc cắt lỗ / vô hiệu hóa trong trích đoạn")

    author_name = author or "Tác giả Square"
    asset_phrase = f" đối với {', '.join(assets)}" if assets else " trên thị trường tài sản số"
    numbers_phrase = f" Mốc số liệu trích dẫn trong bài: {', '.join(extracted_numbers)}." if extracted_numbers else ""
    if is_vi:
        vi_summary = (
            f"[Bản gốc Tiếng Việt] {author_name} phân tích {', '.join(topic_labels_vi[:2]).lower()}{asset_phrase}."
            f"{numbers_phrase} Đánh giá phương pháp: {'; '.join(signals_vi)}."
        )
    else:
        vi_summary = (
            f"[Khung kiểm toán Tiếng Việt · Nguồn {lang_name_vi}] {author_name} thảo luận về "
            f"{', '.join(topic_labels_vi[:2]).lower()}{asset_phrase}.{numbers_phrase} "
            f"Kiểm tra kỷ luật: {'; '.join(signals_vi)}."
        )
    vi_usage_note = (
        "Phù hợp để nghiên cứu phương pháp quản trị rủi ro hoặc xây dựng giả thuyết cần kiểm chứng thêm; "
        "không phải tín hiệu mua/bán tự động."
        if (has_invalidation or has_risk_sizing)
        else "Phù hợp để theo dõi góc nhìn và tâm lý thị trường (Market Narrative); "
             "chưa đủ điều kiện định lượng để lập quy tắc giao dịch."
    )
    provenance_label_vi = (
        "Bản lưu trữ tĩnh công khai (Wayback Static Snapshot)"
        if provenance_type == "archive_snapshot"
        else "Bản lưu phản hồi công khai đã đối chiếu SHA-256"
        if provenance_type == "retained_public_response"
        else "Phản hồi công khai trực tiếp từ nền tảng"
    )
    return {
        "is_vietnamese": is_vi,
        "language_code": norm_lang,
        "language_name_vi": lang_name_vi,
        "language_label_vi": language_label_vi,
        "assets": assets,
        "topics": topics,
        "topic_labels_vi": topic_labels_vi,
        "discipline_checklist": {
            "has_invalidation": has_invalidation,
            "has_risk_sizing": has_risk_sizing,
            "has_conditional_scenario": has_conditional,
            "extracted_numbers": extracted_numbers,
            "signals_vi": signals_vi,
        },
        "vi_summary": vi_summary,
        "vi_usage_note": vi_usage_note,
        "provenance_label_vi": provenance_label_vi,
    }


def analyze_vietnamese_context(
    *,
    title: str | None,
    excerpt: str | None,
    language: str | None = None,
    author: str | None = None,
    provenance_type: str | None = None,
) -> dict[str, Any]:
    return copy.deepcopy(
        _analyze_vietnamese_context_cached(title, excerpt, language, author, provenance_type)
    )


def _format_evidence_value_vi(pillar: str, kind: str, value: dict[str, Any]) -> str:
    if not isinstance(value, dict) or not value:
        return "Không có chi tiết định lượng"
    if pillar == "recognition":
        parts = []
        if value.get("program"):
            parts.append(f"Chương trình: {value['program']}")
        if value.get("category_name"):
            parts.append(f"Hạng mục: {value['category_name']} (Nội dung/Ảnh hưởng)")
        if value.get("rank") is not None:
            parts.append(f"Hạng: #{value['rank']}")
        if value.get("award_vote_count") is not None:
            parts.append(f"Phiếu bình chọn: {value['award_vote_count']:,}".replace(",", "."))
        return " · ".join(parts) or "Công nhận chính thức từ Binance"
    if pillar == "performance":
        if value.get("reported_text"):
            return f"Tự công bố trong bài viết: \"{str(value['reported_text'])[:120]}\" (Chưa xác minh nền tảng)"
        parts = []
        if value.get("portfolio_id"):
            parts.append(f"Portfolio ID: {value['portfolio_id']}")
        if value.get("roi_percent") is not None:
            parts.append(f"ROI 30d: {value['roi_percent']}%")
        if value.get("leader_pnl") is not None:
            parts.append(f"PnL Leader: {value['leader_pnl']:,.2f} USDT")
        if value.get("copier_pnl") is not None:
            parts.append(f"PnL Copier: {value['copier_pnl']:,.2f} USDT")
        if value.get("mdd_percent") is not None:
            parts.append(f"Drawdown tối đa (MDD): {value['mdd_percent']}%")
        if value.get("win_rate_percent") is not None:
            parts.append(f"Win rate: {value['win_rate_percent']}%")
        if value.get("sharpe_ratio") is not None:
            parts.append(f"Sharpe: {value['sharpe_ratio']}")
        return " · ".join(parts) or "Snapshot hiệu suất danh mục công khai"
    parts = []
    for k, label in (("views", "Lượt xem"), ("likes", "Lượt thích"), ("comments", "Bình luận"), ("shares", "Chia sẻ")):
        if value.get(k) is not None:
            parts.append(f"{label}: {value[k]:,}".replace(",", "."))
    return " · ".join(parts) or "Số liệu tương tác bài viết tại thời điểm chụp"


def _public_evidence(conn: Any, entity_id: int, pillar: str) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT ev.id,ev.kind,ev.verification_level,ev.observed_at,ev.period_label,ev.value_json,
               ev.quote,ev.methodology,ev.limitations,ev.dependency_group,s.url,c.canonical_url,
               cv.version_no,cv.fetched_at
        FROM evidence ev
        JOIN entities owner ON owner.id=ev.entity_id
        JOIN sources s ON s.id=ev.source_id
        JOIN content_versions cv ON cv.id=ev.version_id
        JOIN contents c ON c.id=cv.content_id
        JOIN sources content_source ON content_source.id=c.source_id
        WHERE ev.entity_id=? AND ev.pillar=? AND ev.public_shareable=1
          AND owner.public_shareable=1
          AND s.access_level='public' AND s.distribution='public_shareable'
          AND c.public_shareable=1 AND c.entity_id=ev.entity_id AND c.source_id=ev.source_id
          AND content_source.access_level='public' AND content_source.distribution='public_shareable'
        ORDER BY ev.observed_at DESC, ev.id DESC
        """,
        (entity_id, pillar),
    ).fetchall()
    if not rows:
        return dict(UNKNOWN)
    items = []
    seen_dependencies: set[str] = set()
    for row in rows:
        independent = row["dependency_group"] not in seen_dependencies
        seen_dependencies.add(row["dependency_group"])
        items.append({
            "id": row["id"],
            "kind": row["kind"],
            "verification_level": row["verification_level"],
            "observed_at": row["observed_at"],
            "period_label": row["period_label"],
            "value": json_load(row["value_json"], {}),
            "quote": public_excerpt(row["quote"] or ""),
            "methodology": row["methodology"],
            "limitations": row["limitations"],
            "dependency_group": row["dependency_group"],
            "counts_as_independent_source": independent,
            "source_url": row["canonical_url"] or row["url"],
            "version": row["version_no"],
            "fetched_at": row["fetched_at"],
        })
    return {
        "status": "available",
        "label": "Có bằng chứng công khai",
        "verification_level": items[0]["verification_level"],
        "limitations": items[0]["limitations"],
        "independent_source_count": len(seen_dependencies),
        "items": items,
    }


def _build_pillar_group(rows: list[Any], *, lightweight: bool = False) -> dict[str, Any]:
    if not rows:
        return dict(UNKNOWN)
    items = []
    seen_dependencies: set[str] = set()
    independent_source_count = len({row["dependency_group"] for row in rows})
    if lightweight:
        rows = [next((row for row in rows if row["recognition_result"] == "Awardee"
                      and row["verification_level"] == "official"), rows[0])]
    for row in rows:
        independent = row["dependency_group"] not in seen_dependencies
        seen_dependencies.add(row["dependency_group"])
        items.append({
            "id": row["id"],
            "kind": row["kind"],
            "verification_level": row["verification_level"],
            "observed_at": row["observed_at"],
            "period_label": row["period_label"],
            "value": json_load(row["value_json"], {}),
            "quote": public_excerpt(row["quote"] or ""),
            "methodology": row["methodology"],
            "limitations": row["limitations"],
            "dependency_group": row["dependency_group"],
            "recognition_result": row["recognition_result"],
            "recognition_category": row["recognition_category"],
            "recognition_program": row["recognition_program"],
            "counts_as_independent_source": independent,
            "source_url": row["canonical_url"] or row["url"],
            "version": row["version_no"],
            "fetched_at": row["fetched_at"],
        })
    return {
        "status": "available",
        "label": "Có bằng chứng công khai",
        "verification_level": items[0]["verification_level"],
        "limitations": items[0]["limitations"],
        "independent_source_count": independent_source_count,
        "items": items,
    }


def dossiers(
    db_path: str | Path, include_official: bool = False, *,
    lightweight: bool = False, slug: str | None = None,
) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        category_sql = "" if include_official else (
            "AND e.category IN ('official_trader_awardee','square_creator_candidate','copy_trading_portfolio')"
        )
        if slug is not None:
            category_sql += " AND e.slug=?"
        entities = conn.execute(
            f"""
            SELECT e.*,
                   (SELECT COUNT(*) FROM contents c JOIN sources cs ON cs.id=c.source_id
                    WHERE c.entity_id=e.id AND c.public_shareable=1
                      AND cs.access_level='public' AND cs.distribution='public_shareable') AS content_count,
                   (SELECT MAX(COALESCE(c.published_at,c.first_seen_at)) FROM contents c JOIN sources cs ON cs.id=c.source_id
                    WHERE c.entity_id=e.id AND c.public_shareable=1
                      AND cs.access_level='public' AND cs.distribution='public_shareable') AS latest_content_at
            FROM entities e
            WHERE e.public_shareable=1 {category_sql}
              AND EXISTS (
                SELECT 1 FROM contents eligible_content
                JOIN sources eligible_source ON eligible_source.id=eligible_content.source_id
                WHERE eligible_content.entity_id=e.id AND eligible_content.public_shareable=1
                  AND eligible_source.access_level='public'
                  AND eligible_source.distribution='public_shareable'
              )
            ORDER BY CASE e.category WHEN 'official_trader_awardee' THEN 0
                                     WHEN 'copy_trading_portfolio' THEN 1 ELSE 2 END,
                     latest_content_at DESC, e.display_name
            """, (slug,) if slug is not None else (),
        ).fetchall()
        if not entities:
            return []
        all_contents = conn.execute(
            """
            SELECT c.entity_id,c.external_id AS post_id,c.title,c.canonical_url,c.language,c.published_at,
                   c.first_seen_at,cv.version_no,cv.fetched_at,cv.excerpt,cv.metadata_json,s.source_type
            FROM contents c
            JOIN content_versions cv ON cv.id=c.latest_version_id AND cv.content_id=c.id
            JOIN sources s ON s.id=c.source_id
            JOIN entities e ON e.id=c.entity_id
            WHERE c.public_shareable=1 AND e.public_shareable=1
              AND s.access_level='public' AND s.distribution='public_shareable'
            ORDER BY c.entity_id,
                     CASE WHEN s.source_type='binance_square_creator_posts' THEN 0 ELSE 1 END,
                     COALESCE(c.published_at,c.first_seen_at) DESC
            """
        ).fetchall() if not lightweight else []
        contents_by_entity: dict[int, list[Any]] = {}
        for crow in all_contents:
            eid = int(crow["entity_id"])
            bucket = contents_by_entity.setdefault(eid, [])
            bucket.append(crow)

        all_evidence = conn.execute(
            """
            SELECT ev.entity_id,ev.pillar,ev.id,ev.kind,ev.verification_level,ev.observed_at,ev.period_label,
                   ev.value_json,ev.quote,ev.methodology,ev.limitations,ev.dependency_group,s.url,
                   c.canonical_url,cv.version_no,cv.fetched_at,
                   r.result AS recognition_result,r.category_name AS recognition_category,
                   r.program_name AS recognition_program
            FROM evidence ev
            JOIN entities owner ON owner.id=ev.entity_id
            JOIN sources s ON s.id=ev.source_id
            JOIN content_versions cv ON cv.id=ev.version_id
            JOIN contents c ON c.id=cv.content_id
            JOIN sources content_source ON content_source.id=c.source_id
            LEFT JOIN recognitions r ON r.evidence_id=ev.id
            WHERE ev.public_shareable=1 AND owner.public_shareable=1
              AND s.access_level='public' AND s.distribution='public_shareable'
              AND c.public_shareable=1 AND c.entity_id=ev.entity_id AND c.source_id=ev.source_id
              AND content_source.access_level='public' AND content_source.distribution='public_shareable'
            ORDER BY ev.entity_id, ev.pillar, ev.observed_at DESC, ev.id DESC
            """
        ).fetchall()
        evidence_by_entity_pillar: dict[tuple[int, str], list[Any]] = {}
        for erow in all_evidence:
            key = (int(erow["entity_id"]), str(erow["pillar"]))
            evidence_by_entity_pillar.setdefault(key, []).append(erow)

        result = []
        for entity in entities:
            eid = int(entity["id"])
            contents = contents_by_entity.get(eid, [])
            pillars = {
                pillar: _build_pillar_group(evidence_by_entity_pillar.get((eid, pillar), []), lightweight=lightweight)
                for pillar in PILLARS
            }
            award = _award_record({"pillars": pillars})
            content_items = []
            for row in contents:
                row_dict = dict(row)
                row_dict.pop("entity_id", None)
                row_dict["excerpt"] = public_excerpt(row_dict.get("excerpt") or "")
                meta = json_load(row_dict.pop("metadata_json", "{}"), {})
                prov = meta.get("provenance_type", "direct_public_response")
                row_dict["provenance_type"] = prov
                row_dict["vi_intel"] = analyze_vietnamese_context(
                    title=row_dict.get("title"),
                    excerpt=row_dict.get("excerpt"),
                    language=row_dict.get("language"),
                    author=entity["display_name"],
                    provenance_type=prov,
                )
                content_items.append(row_dict)
            available_pillars = [p for p in PILLARS if pillars[p]["status"] == "available"]
            award = award or _award_record({"pillars": pillars})
            category_label_vi = (
                "Awardee Chính Thức (Blockchain 100 — Hạng mục Trader)"
                if entity["category"] == "official_trader_awardee" and award
                else f"Awardee Chính Thức ({award.get('program')} — Hạng mục {award.get('category_name')})"
                if award and award.get("program")
                else "Danh mục Copy Trading Công Khai (Chưa nối danh tính Square)"
                if entity["category"] == "copy_trading_portfolio"
                else "Chương trình chính thức — tiêu chí / thông báo (không phải awardee)"
                if entity["category"] == "official_program"
                else "Tác giả Nội dung Binance Square"
            )
            result.append({
                "slug": entity["slug"],
                "display_name": entity["display_name"],
                "category": entity["category"],
                "category_label_vi": category_label_vi,
                "source_uid": entity["source_uid"],
                "username": entity["username"],
                "profile_url": entity["profile_url"],
                "bio": entity["bio"],
                "award_category": award.get("category_name") if award else None,
                "award_rank": award.get("rank") if award else None,
                "award_votes": award.get("award_vote_count") if award else None,
                "identity": {
                    "status": entity["identity_status"],
                    "evidence_url": entity["identity_evidence_url"],
                    "note": (
                        "Khóa squareUid đến từ official award payload; không nối sang portfolio copy-trading nếu thiếu bằng chứng first-party."
                        if (entity["category"] == "official_trader_awardee" or entity["identity_status"] == "official_award_payload_uid") else
                        "Danh tính chỉ ở cấp portfolio công khai; chưa xác định chủ thể Square hoặc con người đứng sau."
                        if entity["category"] == "copy_trading_portfolio" else
                        "Chỉ gắn với profile tác giả do payload nguồn cung cấp; chưa nối sang portfolio khác."
                    ),
                },
                "content_count": entity["content_count"],
                "latest_content_at": entity["latest_content_at"],
                "pillars": pillars,
                "contents": content_items,
                "vi_profile": {
                    "category_label_vi": category_label_vi,
                    "available_pillar_count": len(available_pillars),
                    "available_pillars_vi": [PILLAR_LABELS_VI[p] for p in available_pillars],
                    "missing_pillars_vi": [PILLAR_LABELS_VI[p] for p in PILLARS if p not in available_pillars],
                    "audit_verdict_vi": (
                        "Đã có công nhận nội dung chính thức từ Binance nhưng CHƯA có bằng chứng hiệu suất giao dịch (PnL/ROI) cấp nền tảng."
                        if award and award.get("program") and pillars["performance"]["status"] != "available"
                        else "Chỉ mới là snapshot danh mục Copy Trading; tách biệt PnL của Leader và PnL của Copier, chưa liên kết với tài khoản Square."
                        if entity["category"] == "copy_trading_portfolio"
                        else "Hồ sơ phục vụ nghiên cứu luận điểm và tương tác cộng đồng; không coi tương tác cao là bằng chứng giao dịch có lãi."
                    ),
                },
                "selection_note": (
                    "Awardee category Trader của chương trình content/influence; không phải chứng nhận hiệu suất giao dịch."
                    if entity["category"] == "official_trader_awardee" and award else
                    f"Awardee hạng mục {award.get('category_name')} ({award.get('program')}); công nhận nội dung/cộng đồng, không phải kiểm toán PnL."
                    if award and award.get("program") else
                    "Snapshot hiệu suất cấp portfolio; không phải danh tính awardee hay bằng chứng thành công trọn đời."
                    if entity["category"] == "copy_trading_portfolio" else
                    "Hồ sơ ứng viên để nghiên cứu nội dung; không phải xếp hạng trader hoặc chứng nhận thành công."
                ),
            })
        return result


def _award_record(item: dict[str, Any]) -> dict[str, Any] | None:
    records = item.get("pillars", {}).get("recognition", {}).get("items", [])
    for record in records:
        value = record.get("value") or {}
        if (record.get("verification_level") == "official"
                and record.get("recognition_result") == "Awardee"):
            return {**value, "program": record.get("recognition_program") or value.get("program"),
                    "category_name": record.get("recognition_category") or value.get("category_name"),
                    "period_label": record.get("period_label")}
    return None


def dossier_listing(
    db_path: str | Path,
    *,
    program: str | None = None,
    year: str | None = None,
    category: str | None = None,
    author: str | None = None,
    window: str = "30d",
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str = "official_rank",
    now: datetime | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    engagement_sorts = {
        f"{metric}_{measure}"
        for metric in ("likes", "comments", "shares", "views", "interactions")
        for measure in ("total", "average")
    }
    if sort not in {"official_rank", "award_votes", "latest", *engagement_sorts}:
        raise ValueError("Unsupported dossier sort")
    _pagination(0, limit, offset)
    current = _aware_utc(now, "Engagement window time")
    window_data, articles_by_entity, _ = _engagement_projection(
        db_path,
        window=window,
        date_from=date_from,
        date_to=date_to,
        current=current,
    )
    items = dossiers(db_path, lightweight=True)
    filtered = []
    author_query = (author or "").strip().casefold()
    for item in items:
        award = _award_record(item)
        if program and (not award or program.casefold() not in str(award.get("program", "")).casefold()):
            continue
        if year and (not award or year not in str(award.get("period_label", ""))):
            continue
        if category:
            category_values = [item.get("category", "")]
            if award:
                category_values.extend([award.get("category_name", ""), award.get("category_type", "")])
            if not any(category.casefold() == str(value).casefold() for value in category_values):
                continue
        if author_query and not any(
            author_query in str(value or "").casefold()
            for value in (item.get("display_name"), item.get("username"), item.get("slug"))
        ):
            continue
        item["engagement_window"] = _aggregate_engagement(
            articles_by_entity.get(str(item["slug"]), []), window_data,
        )
        filtered.append(item)

    def metric(item: dict[str, Any], field: str) -> int | float | None:
        value = (_award_record(item) or {}).get(field)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    if sort == "official_rank":
        filtered.sort(key=lambda item: (
            metric(item, "rank") is None,
            0 if item.get("category") == "official_trader_awardee" else 1,
            metric(item, "rank") if metric(item, "rank") is not None else float("inf"),
            item["display_name"].casefold(),
        ))
    elif sort == "award_votes":
        filtered.sort(key=lambda item: (
            metric(item, "award_vote_count") is None,
            -(metric(item, "award_vote_count") or 0),
            item["display_name"].casefold(),
        ))
    elif sort == "latest":
        filtered.sort(key=lambda item: (item.get("latest_content_at") is None, item.get("latest_content_at") or ""), reverse=False)
        filtered = sorted(filtered, key=lambda item: item.get("latest_content_at") or "", reverse=True)
    else:
        metric_name, measure = sort.rsplit("_", 1)
        filtered.sort(key=lambda item: _engagement_sort_key(item, metric_name, measure))
    content_as_of = max((item.get("latest_content_at") or "" for item in filtered), default="") or None
    engagement_as_of = max(
        (item["engagement_window"].get("as_of") or "" for item in filtered),
        default="",
    ) or None
    return {
        "items": _page_items(filtered, limit, offset),
        "pagination": _pagination(len(filtered), limit, offset),
        "sample_count": len(filtered),
        "as_of": engagement_as_of or content_as_of,
        "content_as_of": content_as_of,
        "engagement_as_of": engagement_as_of,
        "sort": sort,
        "window": _public_window(window_data),
        "eligible_article_count": sum(item["engagement_window"]["sample_n"] for item in filtered),
        "scope_note": (
            "Ranking within the collected public dataset only, after applying the selected original-publication UTC window; "
            "not a Binance-wide leaderboard. Counts are the latest observed snapshot per canonical article, "
            "not interactions accrued during the selected period."
        ),
    }


def _pagination(total: int, limit: int | None, offset: int) -> dict[str, Any]:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100):
        raise ValueError("limit must be an integer between 1 and 100")
    end = total if limit is None else offset + limit
    has_more = end < total
    return {"limit": limit, "offset": offset, "total": total,
            "has_more": has_more, "next_offset": end if has_more else None}


def _page_items(items: list[Any], limit: int | None, offset: int) -> list[Any]:
    _pagination(len(items), limit, offset)
    return items[offset:] if limit is None else items[offset:offset + limit]


def _aware_utc(value: datetime | None, label: str) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return current.astimezone(timezone.utc)


def _engagement_window(
    window: str,
    *,
    date_from: str | None,
    date_to: str | None,
    current: datetime,
) -> dict[str, Any]:
    if window not in {"7d", "30d", "custom"}:
        raise ValueError("Unsupported engagement window")
    if window == "custom":
        start_day = _date_bound(date_from, "date_from")
        end_day = _date_bound(date_to, "date_to")
        if start_day is None or end_day is None:
            raise ValueError("Custom engagement window requires date_from and date_to")
        if start_day > end_day:
            raise ValueError("date_from must not be after date_to")
        start = datetime.fromisoformat(start_day).replace(tzinfo=timezone.utc)
        end_exclusive = datetime.fromisoformat(end_day).replace(tzinfo=timezone.utc) + timedelta(days=1)
        return {
            "mode": "custom",
            "label": f"{start_day} to {end_day}",
            "start_at": start.isoformat().replace("+00:00", "Z"),
            "end_at": end_exclusive.isoformat().replace("+00:00", "Z"),
            "end_exclusive": True,
            "query_date_from": start_day,
            "query_date_to": end_day,
            "original_publication_utc": True,
        }
    days = 7 if window == "7d" else 30
    start = current - timedelta(days=days)
    return {
        "mode": window,
        "label": f"Trailing {days} days",
        "start_at": start.isoformat().replace("+00:00", "Z"),
        "end_at": current.isoformat().replace("+00:00", "Z"),
        "end_exclusive": False,
        "query_date_from": start.date().isoformat(),
        "query_date_to": current.date().isoformat(),
        "original_publication_utc": True,
    }


def _public_window(window: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in window.items() if not key.startswith("query_")}


def _inside_engagement_window(
    published_at: str | None,
    window: dict[str, Any],
    current: datetime,
) -> bool:
    published = _parse_utc(published_at)
    if published is None or published > current:
        return False
    start = _parse_utc(window["start_at"])
    end = _parse_utc(window["end_at"])
    if start is None or end is None or published < start:
        return False
    return published < end if window["end_exclusive"] else published <= end


def _valid_engagement_value(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or not math.isfinite(float(value)):
        return None
    return value


def _aggregate_engagement(
    articles_in_window: list[dict[str, Any]],
    window: dict[str, Any],
) -> dict[str, Any]:
    article_count = len(articles_in_window)
    metrics: dict[str, dict[str, Any]] = {}
    for metric_name in ("likes", "comments", "shares", "views"):
        values = [
            value for article in articles_in_window
            if (value := _valid_engagement_value(article["engagement"].get(metric_name))) is not None
        ]
        known_n = len(values)
        total = sum(values) if known_n else None
        metrics[metric_name] = {
            "total": total,
            "average": (total / known_n) if known_n else None,
            "known_n": known_n,
            "unknown_n": article_count - known_n,
            "coverage_ratio": (known_n / article_count) if article_count else None,
        }
    complete_values = [
        value for article in articles_in_window
        if (value := article.get("total_interactions")) is not None
    ]
    complete_n = len(complete_values)
    complete_total = sum(complete_values) if complete_n else None
    observed_times = [
        str(article["engagement_observed_at"])
        for article in articles_in_window if article.get("engagement_observed_at")
    ]
    return {
        "window": _public_window(window),
        "sample_n": article_count,
        "period_article_count": article_count,
        "metrics": metrics,
        "interactions": {
            "definition": "likes + comments + shares; views excluded",
            "total": complete_total,
            "average": (complete_total / complete_n) if complete_n else None,
            "complete_n": complete_n,
            "incomplete_n": article_count - complete_n,
            "coverage_ratio": (complete_n / article_count) if article_count else None,
        },
        "coverage": {
            "period_article_n": article_count,
            "articles_with_any_known_metric_n": sum(
                any(article["engagement"].get(name) is not None
                    for name in ("likes", "comments", "shares", "views"))
                for article in articles_in_window
            ),
            "complete_interactions_n": complete_n,
        },
        "as_of": max(observed_times, default=None),
        "observation_semantics": (
            "Latest observed snapshot counts for canonical articles originally published in the selected UTC window; "
            "not interactions accrued during that window."
        ),
        "growth": {
            "status": "unavailable",
            "value": None,
            "reason": "No authentic bracketing snapshots for the selected period; growth is not inferred.",
        },
    }


def _engagement_sort_key(
    item: dict[str, Any], metric_name: str, measure: str,
) -> tuple[Any, ...]:
    aggregate = item["engagement_window"]
    if metric_name == "interactions":
        value = aggregate["interactions"][measure]
    else:
        value = aggregate["metrics"][metric_name][measure]
    return (
        value is None,
        -float(value) if value is not None else 0.0,
        item["display_name"].casefold(),
        item["slug"],
    )


def _engagement_projection(
    db_path: str | Path,
    *,
    window: str,
    date_from: str | None,
    date_to: str | None,
    current: datetime,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]], int]:
    window_data = _engagement_window(
        window, date_from=date_from, date_to=date_to, current=current,
    )
    article_data = articles(
        db_path,
        date_from=window_data["query_date_from"],
        date_to=window_data["query_date_to"],
        now=current,
    )
    eligible_articles = [
        article for article in article_data["items"]
        if _inside_engagement_window(article.get("published_at"), window_data, current)
    ]
    articles_by_entity: dict[str, list[dict[str, Any]]] = {}
    for article in eligible_articles:
        articles_by_entity.setdefault(str(article["entity_slug"]), []).append(article)
    return window_data, articles_by_entity, len(eligible_articles)


def _date_bound(value: str | None, name: str) -> str | None:
    if value is None or value == "":
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError(f"Invalid {name}; expected YYYY-MM-DD") from exc


def articles(
    db_path: str | Path,
    *,
    q: str | None = None,
    author: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str = "latest",
    lang: str | None = None,
    asset: str | None = None,
    topic: str | None = None,
    now: datetime | None = None,
    limit: int | None = None,
    offset: int = 0,
    entity: str | None = None,
    post_ids: str | None = None,
    seen_after: str | None = None,
    quality_only: bool = False,
    basis: str = "all",
    cohort: str = "all",
) -> dict[str, Any]:
    if sort not in {"likes", "comments", "shares", "views", "total_interactions", "latest", "discovered", "research"}:
        raise ValueError("Unsupported article sort")
    if basis not in {"all", "quoted", "condition"}:
        raise ValueError("Unsupported research basis")
    if cohort not in {"all", "trader_awardees"}:
        raise ValueError("Unsupported article cohort")
    lang_mode = (lang or "").strip().lower() or "all"
    if lang_mode not in {"all", "vi_first", "vi", "en", "zh", "other"}:
        raise ValueError("Unsupported language filter")
    asset_filter = (asset or "").strip().upper() or None
    topic_filter = (topic or "").strip().lower() or None
    current = _aware_utc(now, "Article window time")
    _pagination(0, limit, offset)
    selected_ids = {value.strip() for value in post_ids.split(",") if value.strip()} if post_ids is not None else None
    seen_bound = _parse_utc(seen_after) if seen_after else None
    if seen_after:
        try:
            parsed_seen = datetime.fromisoformat(seen_after.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("seen_after must be a timezone-aware ISO timestamp") from exc
        if seen_bound is None or parsed_seen.tzinfo is None:
            raise ValueError("seen_after must be a timezone-aware ISO timestamp")
    explicit_window = bool(date_from or date_to)
    if explicit_window:
        start = _date_bound(date_from, "date_from")
        end = _date_bound(date_to, "date_to")
    else:
        try:
            rolling_start = current.replace(year=current.year - 2)
        except ValueError:
            rolling_start = current.replace(year=current.year - 2, day=28)
        start = rolling_start.date().isoformat()
        end = current.date().isoformat()
    if start and end and start > end:
        raise ValueError("date_from must not be after date_to")
    with connect(db_path) as conn:
        rows = conn.execute(
            """WITH latest_metrics AS (
                   SELECT latest_ev.*,latest_version.content_id,
                          ROW_NUMBER() OVER (
                              PARTITION BY latest_version.content_id,latest_ev.entity_id,latest_ev.source_id
                              ORDER BY latest_ev.observed_at DESC,latest_ev.id DESC
                          ) AS metric_rank
                   FROM evidence latest_ev
                   JOIN content_versions latest_version ON latest_version.id=latest_ev.version_id
                   WHERE latest_ev.pillar='engagement' AND latest_ev.kind='post_metrics'
                     AND latest_ev.public_shareable=1
               )
               SELECT c.id,c.external_id,c.title,c.canonical_url,c.author,c.language,c.published_at,c.first_seen_at,
                      cv.id AS version_id,c.entity_id,cv.version_no,cv.fetched_at,cv.excerpt,e.slug AS entity_slug,e.display_name,
                      s.url AS source_url,s.source_type,cv.metadata_json,
                      ev.observed_at AS engagement_observed_at,ev.value_json
               FROM contents c
               JOIN entities e ON e.id=c.entity_id
               JOIN sources s ON s.id=c.source_id
               JOIN content_versions cv ON cv.id=c.latest_version_id AND cv.content_id=c.id
               LEFT JOIN latest_metrics ev ON ev.content_id=c.id AND ev.entity_id=e.id
                 AND ev.source_id=s.id AND ev.metric_rank=1
               WHERE c.public_shareable=1 AND e.public_shareable=1
                 AND s.access_level='public' AND s.distribution='public_shareable'
                 AND s.source_type IN (
                     'binance_square_public_feed','binance_square_creator_posts',
                     'binance_square_public_archive'
                 )
               ORDER BY c.id"""
        ).fetchall()
        trader_ids = set()
        if cohort == "trader_awardees":
            trader_ids = {row["entity_id"] for row in conn.execute(
                """SELECT DISTINCT ev.entity_id FROM evidence ev
                   JOIN recognitions r ON r.evidence_id=ev.id
                   JOIN entities e ON e.id=ev.entity_id
                   JOIN sources s ON s.id=ev.source_id
                   JOIN content_versions cv ON cv.id=ev.version_id
                   JOIN contents c ON c.id=cv.content_id
                   JOIN sources cs ON cs.id=c.source_id
                   WHERE ev.pillar='recognition' AND ev.verification_level='official'
                     AND ev.public_shareable=1 AND e.public_shareable=1
                     AND r.program_name='Blockchain 100 2025' AND r.category_type='content'
                     AND r.category_name='Trader' AND r.result='Awardee'
                     AND c.public_shareable=1 AND c.entity_id=e.id AND c.source_id=ev.source_id
                     AND s.access_level='public' AND s.distribution='public_shareable'
                     AND cs.access_level='public' AND cs.distribution='public_shareable'"""
            )}
    items: list[dict[str, Any]] = []
    query = (q or "").strip().casefold()
    author_query = (author or "").strip().casefold()
    for row in rows:
        if cohort == "trader_awardees" and row["entity_id"] not in trader_ids:
            continue
        if entity is not None and row["entity_slug"] != entity:
            continue
        if selected_ids is not None and str(row["external_id"]) not in selected_ids:
            continue
        if seen_bound is not None:
            first_seen = _parse_utc(row["first_seen_at"])
            if first_seen is None or first_seen <= seen_bound:
                continue
        published = _parse_utc(row["published_at"])
        if published is None or published > current:
            continue
        day = published.date().isoformat()
        if start and day < start:
            continue
        if end and day > end:
            continue
        if author_query and author_query not in str(row["author"] or row["display_name"]).casefold():
            continue
        observed = json_load(row["value_json"], {})
        metadata = json_load(row["metadata_json"], {})
        quality_audited = (
            metadata.get("content_volume_goal") is True
            and metadata.get("qualifying_body") is True
            and isinstance(metadata.get("quality_gate_version"), (int, float))
            and metadata["quality_gate_version"] >= QUALITY_GATE_VERSION
        )
        if quality_only and not quality_audited:
            continue
        provenance_type = metadata.get("provenance_type", "direct_public_response")
        author_display = row["author"] or row["display_name"]
        vi_intel = analyze_vietnamese_context(
            title=row["title"],
            excerpt=public_excerpt(row["excerpt"] or ""),
            language=row["language"],
            author=author_display,
            provenance_type=provenance_type,
        )
        searchable = " ".join([
            str(row["title"] or ""),
            str(row["excerpt"] or ""),
            str(author_display or ""),
            str(row["display_name"] or ""),
            str(vi_intel["vi_summary"] or ""),
            " ".join(vi_intel["topic_labels_vi"]),
            " ".join(vi_intel["assets"]),
        ]).casefold()
        provenance_url = metadata.get("archive_snapshot_url") or row["source_url"]
        engagement = {
            key: _valid_engagement_value(observed.get(key))
            for key in ("likes", "comments", "views", "shares")
        }
        complete_interactions = all(engagement[key] is not None for key in ("likes", "comments", "shares"))
        total_interactions = (
            sum(engagement[key] for key in ("likes", "comments", "shares"))
            if complete_interactions else None
        )
        post_id = str(row["external_id"] or row["canonical_url"].rstrip("/").rsplit("/", 1)[-1])
        items.append({
            "id": row["id"],
            "post_id": post_id,
            "title": row["title"], "url": row["canonical_url"], "author": author_display,
            "entity_slug": row["entity_slug"], "published_at": row["published_at"],
            "first_seen_at": row["first_seen_at"], "fetched_at": row["fetched_at"],
            "version": row["version_no"], "excerpt": public_excerpt(row["excerpt"] or ""),
            "language": vi_intel["language_code"],
            "provenance_type": provenance_type,
            "quality": {"audited": quality_audited, "gate_version": metadata.get("quality_gate_version"),
                        "label": "quality_gate_passed" if quality_audited else "not_audited"},
            "engagement": engagement, "engagement_observed_at": row["engagement_observed_at"],
            "total_interactions": total_interactions,
            "total_interactions_complete": complete_interactions,
            "_version_id": row["version_id"], "_searchable": searchable,
            "vi_intel": vi_intel,
            "citation": {"content_url": row["canonical_url"], "source_url": provenance_url,
                         "version": row["version_no"], "fetched_at": row["fetched_at"]},
        })
    deduped: dict[str, dict[str, Any]] = {}
    for item in items:
        key = item["url"].rstrip("/")
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = item
            continue
        newest_content, older_content = (
            (item, existing) if item["fetched_at"] > existing["fetched_at"] else (existing, item)
        )
        newest_metrics = max(
            (existing, item),
            key=lambda candidate: candidate["engagement_observed_at"] or "",
        )
        newest_content["engagement"] = newest_metrics["engagement"]
        newest_content["engagement_observed_at"] = newest_metrics["engagement_observed_at"]
        newest_content["total_interactions"] = newest_metrics["total_interactions"]
        newest_content["total_interactions_complete"] = newest_metrics["total_interactions_complete"]
        if newest_content.get("published_at") is None:
            newest_content["published_at"] = older_content.get("published_at")
        deduped[key] = newest_content
    all_deduped = list(deduped.values())

    filtered_items = all_deduped
    if lang_mode == "vi":
        filtered_items = [item for item in filtered_items if item["vi_intel"]["is_vietnamese"]]
    elif lang_mode == "en":
        filtered_items = [item for item in filtered_items if item["vi_intel"]["language_code"] == "en"]
    elif lang_mode == "zh":
        filtered_items = [item for item in filtered_items if item["vi_intel"]["language_code"] == "zh"]
    elif lang_mode == "other":
        filtered_items = [item for item in filtered_items if item["vi_intel"]["language_code"] not in {"vi", "en", "zh"}]
    if topic_filter:
        filtered_items = [item for item in filtered_items if topic_filter in item["vi_intel"]["topics"]]
    research_by_version = {}
    if filtered_items:
        with connect(db_path) as conn:
            research_by_version = discovery_research(conn, [item["_version_id"] for item in filtered_items])
    for item in filtered_items:
        item["research"] = research_by_version.get(item.pop("_version_id"), missing_public_research())
        searchable = item.pop("_searchable")
        item["_query_match"] = (
            not query or query in searchable
            or query in str(item["research"].get("statement") or "").casefold()
        )
    filtered_items = [item for item in filtered_items if item.pop("_query_match")]
    if asset_filter:
        filtered_items = [
            item for item in filtered_items
            if asset_filter in item["vi_intel"]["assets"]
            or asset_filter == ASSET_NAME_MAP.get(
                str(item["research"].get("asset") or "").casefold(),
                str(item["research"].get("asset") or "").upper(),
            )
        ]

    research_summary = {
        "scope": "before_basis_filter",
        "total": len(filtered_items),
        "quoted": sum(bool(item["research"].get("statement")) for item in filtered_items),
        "condition": sum(bool(item["research"].get("trigger") or item["research"].get("invalidation"))
                         for item in filtered_items),
    }
    if basis == "quoted":
        filtered_items = [item for item in filtered_items if item["research"].get("statement")]
    elif basis == "condition":
        filtered_items = [
            item for item in filtered_items
            if item["research"].get("trigger") or item["research"].get("invalidation")
        ]
    items = filtered_items
    lang_counts: dict[str, int] = {}
    asset_counts: dict[str, int] = {}
    topic_counts: dict[str, int] = {}
    for item in items:
        lcode = item["vi_intel"]["language_code"]
        lang_counts[lcode] = lang_counts.get(lcode, 0) + 1
        for ticker in item["vi_intel"]["assets"]:
            asset_counts[ticker] = asset_counts.get(ticker, 0) + 1
        for tcode in item["vi_intel"]["topics"]:
            topic_counts[tcode] = topic_counts.get(tcode, 0) + 1
    vi_priority = lang_mode == "vi_first"
    if sort == "research":
        items.sort(key=lambda item: (
            not bool(item["research"].get("trigger") or item["research"].get("invalidation")),
            not bool(item["research"].get("statement")),
            -_parse_utc(item["published_at"]).timestamp(),
            item["url"],
            item["id"],
        ))
    elif sort in {"latest", "discovered"}:
        date_field = "first_seen_at" if sort == "discovered" else "published_at"
        items.sort(key=lambda item: (
            not item["vi_intel"]["is_vietnamese"] if vi_priority else False,
            _parse_utc(item.get(date_field)) is None,
            -_parse_utc(item[date_field]).timestamp() if _parse_utc(item.get(date_field)) else 0,
            item["url"],
        ))
    else:
        metric_name = sort
        items.sort(key=lambda item: (
            not item["vi_intel"]["is_vietnamese"] if vi_priority else False,
            (item["total_interactions"] if metric_name == "total_interactions" else item["engagement"][metric_name]) is None,
            -float(item["total_interactions"] if metric_name == "total_interactions" else item["engagement"][metric_name])
            if (item["total_interactions"] if metric_name == "total_interactions" else item["engagement"][metric_name]) is not None else 0.0,
            -datetime.fromisoformat(item["published_at"].replace("Z", "+00:00")).timestamp(),
            item["url"],
        ))
    metric_coverage = {
        metric_name: {
            "known_n": sum(item["engagement"][metric_name] is not None for item in items),
            "unknown_n": sum(item["engagement"][metric_name] is None for item in items),
        }
        for metric_name in ("likes", "comments", "shares", "views")
    }
    complete_n = sum(item["total_interactions"] is not None for item in items)
    top_assets = [
        {"asset": k, "count": v}
        for k, v in sorted(asset_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:10]
    ]
    return {
        "items": _page_items(items, limit, offset),
        "pagination": _pagination(len(items), limit, offset),
        "sample_count": len(items),
        "as_of": max((item["engagement_observed_at"] or item["fetched_at"] for item in items), default=None),
        "sort": sort,
        "lang": lang_mode,
        "asset": asset_filter,
        "topic": topic_filter,
        "basis": basis,
        "cohort": cohort,
        "research_summary": research_summary,
        "vietnamese_summary": {
            "total_in_window": len(items),
            "vi_native_count": lang_counts.get("vi", 0),
            "keyword_tagged_articles": len(items),
            "language_breakdown": lang_counts,
            "top_assets": top_assets,
            "topic_counts": topic_counts,
        },
        "engagement_coverage": {
            "sample_n": len(items),
            "metrics": metric_coverage,
            "interactions_complete_n": complete_n,
            "interactions_incomplete_n": len(items) - complete_n,
            "interaction_definition": "likes + comments + shares; views excluded",
        },
        "date_window": {
            "mode": "explicit_audit" if explicit_window else "default_trailing_two_calendar_years",
            "date_from": start,
            "date_to": end,
            "future_and_unknown_original_dates_excluded": True,
        },
        "scope_note": "Top means this collected dataset and applied original-date window only; unknown metrics sort last and are never treated as zero.",
        "scope_note_vi": "Thứ hạng chỉ tính trong tập dữ liệu công khai đã thu thập và trong cửa sổ ngày xuất bản gốc (UTC); chỉ số chưa biết luôn xếp cuối và không bao giờ quy thành 0.",
    }


def article_detail(db_path: str | Path, post_key: str) -> dict[str, Any] | None:
    cleaned = (post_key or "").strip().rstrip("/")
    if not cleaned:
        return None
    with connect(db_path) as conn:
        rows = conn.execute(
            """SELECT c.id,c.external_id,c.title,c.canonical_url,c.author,c.language,c.published_at,c.first_seen_at,
                      cv.id AS version_id,cv.version_no,cv.fetched_at,cv.checksum,cv.excerpt,
                      LENGTH(cv.normalized_text) AS body_chars,cv.normalized_text,cv.metadata_json,
                      e.id AS entity_id,e.slug AS entity_slug,e.display_name,e.category AS entity_category,
                      e.username,e.identity_status,
                      s.id AS source_id,s.name AS source_name,s.url AS source_url,s.source_type
               FROM contents c
               JOIN entities e ON e.id=c.entity_id
               JOIN sources s ON s.id=c.source_id
               JOIN content_versions cv ON cv.id=c.latest_version_id AND cv.content_id=c.id
               WHERE c.public_shareable=1 AND e.public_shareable=1
                 AND s.access_level='public' AND s.distribution='public_shareable'
                 AND (
                     c.external_id=? OR CAST(c.id AS TEXT)=?
                     OR c.canonical_url LIKE ?
                 )
               ORDER BY cv.fetched_at DESC, c.id DESC""",
            (cleaned, cleaned, f"%/{cleaned}"),
        ).fetchall()
        if not rows:
            return None
        row = rows[0]
        content_id = int(row["id"])
        entity_id = int(row["entity_id"])
        source_id = int(row["source_id"])
        metadata = json_load(row["metadata_json"], {})
        provenance_type = metadata.get("provenance_type", "direct_public_response")
        provenance_url = metadata.get("archive_snapshot_url") or row["source_url"]
        author_display = row["author"] or row["display_name"]
        vi_intel = analyze_vietnamese_context(
            title=row["title"],
            excerpt=public_excerpt(row["excerpt"] or ""),
            language=row["language"],
            author=author_display,
            provenance_type=provenance_type,
        )
        ev_rows = conn.execute(
            """SELECT ev.id,ev.pillar,ev.kind,ev.verification_level,ev.observed_at,ev.period_label,
                      ev.value_json,ev.quote,ev.methodology,ev.limitations,ev.dependency_group,
                      ev.version_id,cv.version_no,cv.checksum
               FROM evidence ev
               JOIN content_versions cv ON cv.id=ev.version_id
               WHERE cv.content_id=? AND ev.entity_id=? AND ev.source_id=? AND ev.public_shareable=1
               ORDER BY ev.observed_at DESC, ev.id DESC""",
            (content_id, entity_id, source_id),
        ).fetchall()
        evidence_items = []
        latest_engagement_raw: dict[str, Any] = {}
        latest_engagement_at: str | None = None
        for ev in ev_rows:
            val = json_load(ev["value_json"], {})
            if ev["pillar"] == "engagement" and ev["kind"] == "post_metrics" and latest_engagement_at is None:
                latest_engagement_raw = val
                latest_engagement_at = ev["observed_at"]
            evidence_items.append({
                "id": ev["id"],
                "version_id": ev["version_id"], "version": ev["version_no"], "checksum": ev["checksum"],
                "pillar": ev["pillar"],
                "pillar_label_vi": PILLAR_LABELS_VI.get(ev["pillar"], ev["pillar"]),
                "kind": ev["kind"],
                "verification_level": ev["verification_level"],
                "verification_label_vi": VERIFICATION_LABELS_VI.get(ev["verification_level"], ev["verification_level"]),
                "observed_at": ev["observed_at"],
                "period_label": ev["period_label"],
                "value": val,
                "value_summary_vi": _format_evidence_value_vi(ev["pillar"], ev["kind"], val),
                "methodology": ev["methodology"],
                "limitations": ev["limitations"],
            })
        engagement = {
            key: _valid_engagement_value(latest_engagement_raw.get(key))
            for key in ("likes", "comments", "views", "shares")
        }
        complete_interactions = all(engagement[key] is not None for key in ("likes", "comments", "shares"))
        total_interactions = (
            sum(engagement[key] for key in ("likes", "comments", "shares"))
            if complete_interactions else None
        )
        brief = discovery_research(conn, [row["version_id"]]).get(row["version_id"])
        claim_rows = permitted_source_claims(
            conn, row["version_id"], entity_id, brief["statement"] if brief else None,
        )
        claims_list = [{
            "id": cl["id"], "claim_type": cl["claim_type"],
            "claim_type_vi": "Trích xuất xác định từ nguồn (không phải phân tích AI)",
            "statement": brief["statement"], "quote": brief["quote"],
            "asset": brief["asset"], "horizon": brief["horizon"],
            "trigger": brief["trigger"], "invalidation": brief["invalidation"],
            "method": brief["method"], "confidence_label": "source_excerpt",
            "scorable": False, "created_at": cl["created_at"],
        } for cl in claim_rows]
        version_rows = conn.execute(
            """SELECT version_no,fetched_at,checksum,excerpt,metadata_json,LENGTH(normalized_text) AS body_chars
               FROM content_versions WHERE content_id=? ORDER BY version_no DESC""",
            (content_id,),
        ).fetchall()
        versions = []
        for version in version_rows:
            version_meta = json_load(version["metadata_json"], {})
            versions.append({
                key: version[key] for key in ("version_no", "fetched_at", "checksum", "body_chars")
            } | {"excerpt": public_excerpt(version["excerpt"] or ""),
                 "source_url": version_meta.get("archive_snapshot_url") or row["source_url"]})
        pillars = {pillar: _public_evidence(conn, entity_id, pillar) for pillar in PILLARS}
        return {
            "id": content_id,
            "post_id": str(row["external_id"]),
            "title": row["title"],
            "url": row["canonical_url"],
            "author": author_display,
            "username": row["username"],
            "entity_slug": row["entity_slug"],
            "entity_category": row["entity_category"],
            "published_at": row["published_at"],
            "first_seen_at": row["first_seen_at"],
            "fetched_at": row["fetched_at"],
            "version": row["version_no"],
            "checksum": row["checksum"],
            "body_chars": row["body_chars"],
            "excerpt": public_excerpt(row["excerpt"] or ""),
            "language": vi_intel["language_code"],
            "provenance_type": provenance_type,
            "engagement": engagement,
            "engagement_observed_at": latest_engagement_at,
            "total_interactions": total_interactions,
            "total_interactions_complete": complete_interactions,
            "vi_intel": vi_intel,
            "claims": claims_list,
            "research": brief if claims_list else missing_public_research(),
            "evidence": evidence_items,
            "versions": versions,
            "author_pillars": {
                p: {
                    "status": pillars[p]["status"],
                    "label": pillars[p]["label"],
                    "verification_level": pillars[p]["verification_level"],
                }
                for p in PILLARS
            },
            "citation": {
                "content_url": row["canonical_url"],
                "source_url": provenance_url,
                "source_name": row["source_name"],
                "version": row["version_no"],
                "fetched_at": row["fetched_at"],
                "checksum": row["checksum"],
            },
        }


def dossier(
    db_path: str | Path,
    slug: str,
    *,
    window: str = "30d",
    date_from: str | None = None,
    date_to: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    current = _aware_utc(now, "Engagement window time")
    window_data, articles_by_entity, _ = _engagement_projection(
        db_path,
        window=window,
        date_from=date_from,
        date_to=date_to,
        current=current,
    )
    for item in dossiers(db_path, include_official=True, slug=slug):
        if item["slug"] == slug:
            item["engagement_window"] = _aggregate_engagement(
                articles_by_entity.get(str(item["slug"]), []), window_data,
            )
            item["claims"] = []
            for content in item["contents"]:
                detail = article_detail(db_path, str(content["post_id"]))
                if detail is None:
                    continue
                item["claims"].extend({
                    **claim, "article_title": detail["title"], "article_url": detail["url"],
                    "published_at": detail["published_at"],
                } for claim in detail["claims"])
            return item
    return None


def _table_exists(conn: Any, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _freshness(last_success_at: str | None, cadence_seconds: int | None, schedule_class: str | None) -> str:
    if not last_success_at:
        return "empty"
    if schedule_class in {"manual", "disabled"}:
        return "retained_snapshot"
    observed = _parse_utc(last_success_at)
    if observed is None:
        return "stale"
    allowance = max((cadence_seconds or 86400) * 2, 7200)
    return "fresh" if (datetime.now(timezone.utc) - observed).total_seconds() <= allowance else "stale"


def worker_public_status(db_path: str | Path) -> dict[str, Any]:
    """Return bounded operational status without paths, exceptions, or run notes."""
    with connect(db_path) as conn:
        if not _table_exists(conn, "worker_state"):
            return {
                "status": "not_configured", "heartbeat_at": None,
                "last_cycle_completed_at": None, "next_cycle_due_at": None,
                "last_cycle_status": None, "cycle_count": 0,
                "source_summary": {"due": 0, "cooldown": 0, "outage": 0, "policy_paused": 0},
                "retained_data": {"status": "unknown", "last_success_at": None},
                "backup": {"status": "not_configured", "last_completed_at": None},
                "crawler": public_crawler_status(db_path),
                "content_extraction": content_volume_status(db_path),
            }
        row = conn.execute("SELECT * FROM worker_state WHERE id=1").fetchone()
        if row is None:
            return {"status": "not_started", "cycle_count": 0}
        status = row["status"]
        heartbeat = _parse_utc(row["heartbeat_at"])
        stale_after = max(int(row["interval_seconds"]) * 2, 300)
        if status in {"running", "idle", "degraded", "stopping"} and (
            heartbeat is None or (datetime.now(timezone.utc) - heartbeat).total_seconds() > stale_after
        ):
            status = "stale"
        schedule_summary = {"due": 0, "cooldown": 0, "outage": 0, "policy_paused": 0}
        if _table_exists(conn, "source_schedule"):
            now = utc_now()
            schedule_summary = {
                "due": int(conn.execute(
                    """SELECT COUNT(*) FROM source_schedule schedule JOIN sources source ON source.id=schedule.source_id
                       WHERE source.access_level='public' AND source.distribution='public_shareable'
                         AND schedule.cadence_seconds IS NOT NULL AND schedule.next_due_at<=?
                         AND (schedule.cooldown_until IS NULL OR schedule.cooldown_until<=?)""",
                    (now, now),
                ).fetchone()[0]),
                "cooldown": int(conn.execute(
                    """SELECT COUNT(*) FROM source_schedule schedule JOIN sources source ON source.id=schedule.source_id
                       WHERE source.access_level='public' AND source.distribution='public_shareable'
                         AND schedule.cooldown_until>?""", (now,)
                ).fetchone()[0]),
                "outage": int(conn.execute(
                    """SELECT COUNT(*) FROM source_schedule schedule JOIN sources source ON source.id=schedule.source_id
                       WHERE source.access_level='public' AND source.distribution='public_shareable'
                         AND schedule.last_outcome IN ('blocked','error')"""
                ).fetchone()[0]),
                "policy_paused": int(conn.execute(
                    """SELECT COUNT(*) FROM sources
                       WHERE access_level='public' AND distribution='public_shareable'
                         AND capability_status='policy_paused'"""
                ).fetchone()[0]),
            }
        last_public = conn.execute(
            "SELECT MAX(last_success_at) FROM sources WHERE access_level='public' AND distribution='public_shareable'"
        ).fetchone()[0]
        return {
            "status": status,
            "interval_seconds": int(row["interval_seconds"]),
            "heartbeat_at": row["heartbeat_at"],
            "last_cycle_completed_at": row["last_cycle_completed_at"],
            "next_cycle_due_at": row["next_cycle_due_at"],
            "last_cycle_status": row["last_cycle_status"],
            "cycle_count": int(row["cycle_count"]),
            "source_summary": schedule_summary,
            "retained_data": {
                "status": _freshness(last_public, 86400, "daily") if last_public else "empty",
                "last_success_at": last_public,
            },
            "backup": {
                "status": row["last_backup_status"] or "not_run",
                "last_completed_at": row["last_backup_at"],
            },
            "crawler": public_crawler_status(db_path),
            "content_extraction": content_volume_status(db_path),
        }


def sources(db_path: str | Path) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT slug,name,url,source_type,access_level,distribution,expected_pillar,capability_status,
                   available_fields_json,last_attempt_at,last_success_at
            FROM sources WHERE access_level='public' AND distribution='public_shareable' ORDER BY name
            """
        ).fetchall()
        schedules: dict[str, dict[str, Any]] = {}
        if _table_exists(conn, "source_schedule"):
            schedules = {
                schedule["slug"]: dict(schedule)
                for schedule in conn.execute(
                    """SELECT source.slug,schedule.schedule_class,schedule.cadence_seconds,
                              schedule.next_due_at,schedule.cooldown_until,schedule.last_outcome
                       FROM sources source JOIN source_schedule schedule ON schedule.source_id=source.id"""
                ).fetchall()
            }
        safe_notes = {
            "available": "Adapter đã lấy được các trường công khai được liệt kê.",
            "blocked": "Nguồn gặp cổng truy cập; adapter đã dừng, không bypass.",
            "error": "Nguồn chưa cung cấp payload có thể ingest trong lần thử gần nhất.",
            "untested": "Nguồn chưa được kiểm tra.",
            "policy_paused": "Thu thập tự động đang tạm dừng theo crawler policy; bằng chứng đã lưu vẫn được giữ nguyên.",
        }
        result = []
        for row in rows:
            item = dict(row)
            item["available_fields"] = json_load(item.pop("available_fields_json"), [])
            item["capability_note"] = safe_notes.get(item["capability_status"], "Trạng thái nguồn chưa xác định.")
            schedule = schedules.get(item["slug"], {})
            cooldown = _parse_utc(schedule.get("cooldown_until"))
            item["schedule"] = {
                "class": schedule.get("schedule_class", "not_configured"),
                "cadence_seconds": schedule.get("cadence_seconds"),
                "next_due_at": schedule.get("next_due_at"),
                "cooldown_active": bool(cooldown and cooldown > datetime.now(timezone.utc)),
                "last_outcome": schedule.get("last_outcome"),
            }
            item["freshness"] = _freshness(
                item["last_success_at"], schedule.get("cadence_seconds"), schedule.get("schedule_class")
            )
            if schedule.get("schedule_class") == "manual":
                item["freshness_note"] = "Retained operator snapshot; not auto-refreshed and may be stale."
            elif item["capability_status"] == "policy_paused":
                item["freshness_note"] = "Retained evidence only; no raw API request was made while policy-paused."
            result.append(item)
        return result


def drafts(db_path: str | Path, *, now: datetime | None = None) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        current = _aware_utc(now, "Draft window time")
        try:
            window_start = current.replace(year=current.year - 2)
        except ValueError:
            window_start = current.replace(year=current.year - 2, day=28)
        rows = conn.execute(
            """
            SELECT artifact.id,artifact.artifact_type,artifact.title,artifact.summary,artifact.body_json,
                   artifact.status,artifact.created_at,artifact.updated_at
            FROM output_artifacts artifact
            WHERE artifact.distribution='public_shareable' AND artifact.status='draft'
              AND (
                EXISTS (SELECT 1 FROM artifact_claims present WHERE present.artifact_id=artifact.id)
                OR (
                  json_extract(artifact.body_json,'$.generator')='grounded_research_v1'
                  AND json_extract(artifact.body_json,'$.output_kind')='research_backlog'
                )
              )
              AND NOT EXISTS (
                SELECT 1
                FROM artifact_claims dependency
                JOIN claims claim ON claim.id=dependency.claim_id
                LEFT JOIN entities owner ON owner.id=claim.entity_id
                WHERE dependency.artifact_id=artifact.id
                  AND (
                    claim.public_shareable<>1 OR owner.id IS NULL OR owner.public_shareable<>1
                    OR NOT EXISTS (
                      SELECT 1 FROM claim_versions cv_present
                      WHERE cv_present.claim_id=claim.id
                    )
                    OR EXISTS (
                      SELECT 1
                      FROM claim_versions cv_link
                      LEFT JOIN content_versions version ON version.id=cv_link.version_id
                      LEFT JOIN contents content ON content.id=version.content_id
                      LEFT JOIN sources source ON source.id=content.source_id
                      WHERE cv_link.claim_id=claim.id
                        AND (
                          version.id IS NULL OR content.id IS NULL OR source.id IS NULL
                          OR content.public_shareable<>1 OR content.entity_id<>claim.entity_id
                          OR source.access_level<>'public' OR source.distribution<>'public_shareable'
                        )
                    )
                    OR EXISTS (
                      SELECT 1
                      FROM claim_evidence ce
                      LEFT JOIN evidence ev ON ev.id=ce.evidence_id
                      LEFT JOIN entities ev_owner ON ev_owner.id=ev.entity_id
                      LEFT JOIN sources ev_source ON ev_source.id=ev.source_id
                      LEFT JOIN content_versions ev_version ON ev_version.id=ev.version_id
                      LEFT JOIN contents ev_content ON ev_content.id=ev_version.content_id
                      LEFT JOIN sources ev_content_source ON ev_content_source.id=ev_content.source_id
                      WHERE ce.claim_id=claim.id
                        AND (
                          ev.id IS NULL OR ev.public_shareable<>1
                          OR ev_owner.id IS NULL OR ev_owner.public_shareable<>1
                          OR ev_source.id IS NULL OR ev_source.access_level<>'public' OR ev_source.distribution<>'public_shareable'
                          OR ev_version.id IS NULL OR ev_content.id IS NULL OR ev_content.public_shareable<>1
                          OR ev_content.entity_id<>ev.entity_id OR ev_content.source_id<>ev.source_id
                          OR ev_content_source.id IS NULL OR ev_content_source.access_level<>'public'
                          OR ev_content_source.distribution<>'public_shareable'
                        )
                    )
                  )
              )
            ORDER BY CASE artifact_type WHEN 'content' THEN 1 WHEN 'strategy_hypothesis' THEN 2 ELSE 3 END, updated_at DESC
            """
        ).fetchall()
        result = []
        for row in rows:
            body = json_load(row["body_json"], {})
            if body.get("generator") != "grounded_research_v1" or body.get("generation_active") is not True:
                continue
            citations = body.get("citations")
            if not isinstance(citations, list) or not citations:
                continue
            permitted = True
            for citation in citations:
                if not isinstance(citation, dict):
                    permitted = False
                    break
                match = conn.execute(
                    """SELECT v.id,c.published_at FROM content_versions v
                       JOIN contents c ON c.id=v.content_id JOIN entities e ON e.id=c.entity_id
                       JOIN sources s ON s.id=c.source_id
                       WHERE c.id=? AND v.id=? AND c.latest_version_id=v.id AND v.checksum=?
                         AND c.public_shareable=1 AND e.public_shareable=1
                         AND s.access_level='public' AND s.distribution='public_shareable'
                         AND v.fetched_at<=?""",
                    (citation.get("content_id"), citation.get("version_id"), citation.get("checksum"), current.replace(microsecond=0).isoformat().replace("+00:00", "Z")),
                ).fetchone()
                if match is None:
                    permitted = False
                    break
                published = _parse_utc(match["published_at"])
                if published is None or not window_start <= published <= current:
                    permitted = False
                    break
            if permitted:
                result.append({**{key: row[key] for key in row.keys() if key != "body_json"}, "body": body})
        return result


def evidence_ledger(db_path: str | Path) -> list[dict[str, Any]]:
    """Complete public ledger for Python exports; HTTP uses evidence_listing."""
    with connect(db_path) as conn:
        rows = conn.execute(
            """SELECT ev.*,e.slug AS entity_slug,e.display_name AS entity,
                      c.external_id AS post_id,c.canonical_url,c.published_at,
                      cv.version_no AS version,cv.checksum,cv.fetched_at,s.url AS source_url
               FROM evidence ev JOIN entities e ON e.id=ev.entity_id
               JOIN sources s ON s.id=ev.source_id
               JOIN content_versions cv ON cv.id=ev.version_id
               JOIN contents c ON c.id=cv.content_id
               WHERE ev.public_shareable=1 AND e.public_shareable=1 AND c.public_shareable=1
                 AND c.entity_id=ev.entity_id AND c.source_id=ev.source_id
                 AND s.access_level='public' AND s.distribution='public_shareable'
               ORDER BY ev.observed_at DESC,ev.id DESC"""
        ).fetchall()
    result = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        item = dict(row)
        value = json_load(item.pop("value_json"), {})
        key = (item["entity_slug"], item["pillar"], item["dependency_group"])
        item.update({
            "value": value, "source_url": item["canonical_url"] or item["source_url"],
            "quote": public_excerpt(item.get("quote") or ""),
            "counts_as_independent_source": key not in seen,
            "pillar_label_vi": PILLAR_LABELS_VI.get(item["pillar"], item["pillar"]),
            "verification_label_vi": VERIFICATION_LABELS_VI.get(item["verification_level"], item["verification_level"]),
            "value_summary_vi": _format_evidence_value_vi(item["pillar"], item["kind"], value),
        })
        seen.add(key)
        result.append(item)
    return result


def evidence_listing(
    db_path: str | Path, *, q: str | None = None, pillar: str | None = None,
    level: str | None = None, limit: int | None = None, offset: int = 0,
) -> dict[str, Any]:
    query = (q or "").strip().casefold()
    _pagination(0, limit, offset)
    items = [item for item in evidence_ledger(db_path)
             if (not pillar or item["pillar"] == pillar)
             and (not level or item["verification_level"] == level)
             and (not query or query in json.dumps(item, ensure_ascii=False).casefold())]
    return {"items": _page_items(items, limit, offset), "sample_count": len(items),
            "pagination": _pagination(len(items), limit, offset)}

def health(db_path: str | Path) -> dict[str, Any]:
    with connect(db_path) as conn:
        schema_row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        actual_schema_version = int(schema_row[0]) if schema_row and str(schema_row[0]).isdigit() else None
        counts = {
            "sources": int(conn.execute("SELECT COUNT(*) FROM sources WHERE access_level='public' AND distribution='public_shareable'").fetchone()[0]),
            "entities": int(conn.execute(
                """SELECT COUNT(*) FROM entities e WHERE e.public_shareable=1 AND EXISTS(
                     SELECT 1 FROM contents c JOIN sources s ON s.id=c.source_id
                     WHERE c.entity_id=e.id AND c.public_shareable=1 AND s.access_level='public' AND s.distribution='public_shareable')"""
            ).fetchone()[0]),
            "contents": int(conn.execute(
                """SELECT COUNT(*) FROM contents c JOIN entities e ON e.id=c.entity_id JOIN sources s ON s.id=c.source_id
                     WHERE c.public_shareable=1 AND e.public_shareable=1 AND s.access_level='public' AND s.distribution='public_shareable'"""
            ).fetchone()[0]),
            "content_versions": int(conn.execute(
                """SELECT COUNT(*) FROM content_versions cv JOIN contents c ON c.id=cv.content_id
                     JOIN entities e ON e.id=c.entity_id JOIN sources s ON s.id=c.source_id
                     WHERE c.public_shareable=1 AND e.public_shareable=1 AND s.access_level='public' AND s.distribution='public_shareable'"""
            ).fetchone()[0]),
            "evidence": int(conn.execute(
                """SELECT COUNT(*) FROM evidence ev JOIN entities e ON e.id=ev.entity_id JOIN sources s ON s.id=ev.source_id
                     JOIN content_versions cv ON cv.id=ev.version_id JOIN contents c ON c.id=cv.content_id
                     WHERE ev.public_shareable=1 AND e.public_shareable=1 AND c.public_shareable=1
                       AND c.entity_id=ev.entity_id AND c.source_id=ev.source_id
                       AND s.access_level='public' AND s.distribution='public_shareable'"""
            ).fetchone()[0]),
            "claims": int(conn.execute(
                """SELECT COUNT(DISTINCT cl.id) FROM claims cl JOIN entities e ON e.id=cl.entity_id
                     JOIN claim_versions link ON link.claim_id=cl.id JOIN content_versions cv ON cv.id=link.version_id
                     JOIN contents c ON c.id=cv.content_id JOIN sources s ON s.id=c.source_id
                     WHERE cl.public_shareable=1 AND e.public_shareable=1 AND c.public_shareable=1
                       AND c.entity_id=cl.entity_id AND s.access_level='public' AND s.distribution='public_shareable'"""
            ).fetchone()[0]),
            "output_artifacts": len(drafts(db_path)),
        }
        candidate_count = int(conn.execute(
            """SELECT COUNT(*) FROM entities e WHERE e.public_shareable=1
               AND e.category IN ('square_creator_candidate','official_trader_awardee')
               AND EXISTS(SELECT 1 FROM contents c JOIN sources s ON s.id=c.source_id WHERE c.entity_id=e.id
                 AND c.public_shareable=1 AND s.access_level='public' AND s.distribution='public_shareable')"""
        ).fetchone()[0])
        candidate_with_engagement = int(conn.execute(
            """SELECT COUNT(DISTINCT e.id) FROM entities e JOIN evidence ev ON ev.entity_id=e.id
               JOIN sources s ON s.id=ev.source_id JOIN content_versions cv ON cv.id=ev.version_id
               JOIN contents c ON c.id=cv.content_id
               WHERE e.public_shareable=1 AND e.category IN ('square_creator_candidate','official_trader_awardee')
                 AND ev.public_shareable=1
                 AND ev.pillar='engagement' AND c.public_shareable=1 AND c.entity_id=e.id AND c.source_id=ev.source_id
                 AND s.access_level='public' AND s.distribution='public_shareable'"""
        ).fetchone()[0])
        substantive = int(conn.execute(
            """SELECT COUNT(*) FROM contents c JOIN entities e ON e.id=c.entity_id JOIN content_versions cv ON cv.id=c.latest_version_id
               JOIN sources s ON s.id=c.source_id
               WHERE c.public_shareable=1 AND e.public_shareable=1
                 AND e.category IN ('square_creator_candidate','official_trader_awardee')
                 AND s.source_type IN (
                     'binance_square_public_feed','binance_square_creator_posts',
                     'binance_square_public_archive'
                 )
                 AND s.access_level='public' AND s.distribution='public_shareable' AND LENGTH(cv.normalized_text)>=200"""
        ).fetchone()[0])
        official_awardees = int(conn.execute(
            """SELECT COUNT(DISTINCT e.id) FROM entities e
               JOIN evidence ev ON ev.entity_id=e.id
               JOIN recognitions r ON r.evidence_id=ev.id
               JOIN sources s ON s.id=ev.source_id
               JOIN content_versions cv ON cv.id=ev.version_id
               JOIN contents c ON c.id=cv.content_id
               WHERE e.public_shareable=1 AND e.category='official_trader_awardee'
                 AND ev.public_shareable=1 AND ev.verification_level='official'
                 AND r.program_name='Blockchain 100 2025' AND r.category_type='content'
                 AND r.category_name='Trader' AND r.result='Awardee'
                 AND c.public_shareable=1 AND c.entity_id=e.id AND c.source_id=ev.source_id
                 AND s.access_level='public' AND s.distribution='public_shareable'"""
        ).fetchone()[0])
        awardees_with_substantive_content = int(conn.execute(
            """SELECT COUNT(DISTINCT e.id) FROM entities e
               JOIN contents c ON c.entity_id=e.id JOIN content_versions cv ON cv.id=c.latest_version_id
               JOIN sources s ON s.id=c.source_id
               WHERE e.public_shareable=1 AND e.category='official_trader_awardee'
                 AND c.public_shareable=1 AND s.source_type='binance_square_creator_posts'
                 AND s.access_level='public' AND s.distribution='public_shareable'
                 AND LENGTH(cv.normalized_text)>=200"""
        ).fetchone()[0])
        awardee_substantive_contents = int(conn.execute(
            """SELECT COUNT(*) FROM entities e
               JOIN contents c ON c.entity_id=e.id JOIN content_versions cv ON cv.id=c.latest_version_id
               JOIN sources s ON s.id=c.source_id
               WHERE e.public_shareable=1 AND e.category='official_trader_awardee'
                 AND c.public_shareable=1 AND s.source_type='binance_square_creator_posts'
                 AND s.access_level='public' AND s.distribution='public_shareable'
                 AND LENGTH(cv.normalized_text)>=200"""
        ).fetchone()[0])
        awardees_with_platform_performance = int(conn.execute(
            """SELECT COUNT(DISTINCT e.id) FROM entities e JOIN evidence ev ON ev.entity_id=e.id
               JOIN sources s ON s.id=ev.source_id JOIN content_versions cv ON cv.id=ev.version_id
               JOIN contents c ON c.id=cv.content_id
               WHERE e.public_shareable=1 AND e.category='official_trader_awardee'
                 AND ev.public_shareable=1 AND ev.pillar='performance' AND ev.verification_level='platform'
                 AND c.public_shareable=1 AND c.entity_id=e.id AND c.source_id=ev.source_id
                 AND s.access_level='public' AND s.distribution='public_shareable'"""
        ).fetchone()[0])
        platform_performance_portfolios = int(conn.execute(
            """SELECT COUNT(DISTINCT e.id) FROM entities e JOIN evidence ev ON ev.entity_id=e.id
               JOIN sources s ON s.id=ev.source_id JOIN content_versions cv ON cv.id=ev.version_id
               JOIN contents c ON c.id=cv.content_id
               WHERE e.public_shareable=1 AND e.category='copy_trading_portfolio'
                 AND ev.public_shareable=1 AND ev.pillar='performance' AND ev.verification_level='platform'
                 AND c.public_shareable=1 AND c.entity_id=e.id AND c.source_id=ev.source_id
                 AND s.access_level='public' AND s.distribution='public_shareable'"""
        ).fetchone()[0])
        captured_performance_portfolios = int(conn.execute(
            """SELECT COUNT(DISTINCT e.id) FROM entities e JOIN evidence ev ON ev.entity_id=e.id
               JOIN sources s ON s.id=ev.source_id JOIN content_versions cv ON cv.id=ev.version_id
               JOIN contents c ON c.id=cv.content_id
               WHERE e.public_shareable=1 AND e.category='copy_trading_portfolio'
                 AND ev.public_shareable=1 AND ev.pillar='performance'
                 AND ev.verification_level='operator_capture'
                 AND c.public_shareable=1 AND c.entity_id=e.id AND c.source_id=ev.source_id
                 AND s.access_level='public' AND s.distribution='public_shareable'"""
        ).fetchone()[0])
        last_public_ingest_at = conn.execute(
            "SELECT MAX(last_success_at) FROM sources WHERE access_level='public' AND distribution='public_shareable'"
        ).fetchone()[0]
        worker_status = worker_public_status(db_path)
        crawler_status = worker_status.get("crawler") or public_crawler_status(db_path)
        volume_status = worker_status.get("content_extraction") or content_volume_status(db_path)
        return {
            "status": "ok",
            "app": {"status": "up", "mode": "public_read_only"},
            "checked_at": utc_now(),
            "schema_version": actual_schema_version,
            "supported_schema_version": SCHEMA_VERSION,
            "worker": worker_status,
            "crawler": crawler_status,
            "content_extraction": volume_status,
            "counts": counts,
            "last_ingest": {"status": "available" if last_public_ingest_at else "not_run", "finished_at": last_public_ingest_at},
            "pipeline_smoke": {
                "status": "passed" if candidate_with_engagement > 0 and substantive > 0 else "not_met",
                "creator_candidates": candidate_count,
                "candidates_with_engagement": candidate_with_engagement,
                "substantive_non_official_contents": substantive,
                "definition": "Chỉ xác nhận pipeline ingest → provenance → public projection; không phải chứng minh chất lượng trader.",
            },
            "trader_quality": {
                "status": "not_established" if awardees_with_platform_performance == 0 else "partially_observed",
                "official_trader_awardees": official_awardees,
                "awardees_with_substantive_content": awardees_with_substantive_content,
                "awardee_substantive_contents": awardee_substantive_contents,
                "awardees_with_platform_performance": awardees_with_platform_performance,
                "unlinked_platform_performance_portfolios": platform_performance_portfolios,
                "unlinked_captured_performance_portfolios": captured_performance_portfolios,
                "definition": "Recognition, direct-platform evidence và operator capture tách biệt; không nối danh tính hoặc nâng trust khi thiếu provenance độc lập.",
            },
        }


def overview(db_path: str | Path) -> dict[str, Any]:
    data = health(db_path)
    with connect(db_path) as conn:
        dossier_count = int(conn.execute(
            """SELECT COUNT(*) FROM entities e
               WHERE e.public_shareable=1
                 AND e.category IN ('official_trader_awardee','square_creator_candidate','copy_trading_portfolio')
                 AND EXISTS (
                   SELECT 1 FROM contents eligible_content
                   JOIN sources eligible_source ON eligible_source.id=eligible_content.source_id
                   WHERE eligible_content.entity_id=e.id AND eligible_content.public_shareable=1
                     AND eligible_source.access_level='public'
                     AND eligible_source.distribution='public_shareable'
                 )"""
        ).fetchone()[0])
        pillar_coverage = {
            pillar: int(conn.execute(
                """SELECT COUNT(DISTINCT ev.entity_id)
                   FROM evidence ev
                   JOIN entities owner ON owner.id=ev.entity_id
                   JOIN sources s ON s.id=ev.source_id
                   JOIN content_versions cv ON cv.id=ev.version_id
                   JOIN contents c ON c.id=cv.content_id
                   JOIN sources content_source ON content_source.id=c.source_id
                   WHERE ev.pillar=? AND ev.public_shareable=1
                     AND owner.public_shareable=1
                     AND owner.category IN ('official_trader_awardee','square_creator_candidate','copy_trading_portfolio')
                     AND s.access_level='public' AND s.distribution='public_shareable'
                     AND c.public_shareable=1 AND c.entity_id=ev.entity_id AND c.source_id=ev.source_id
                     AND content_source.access_level='public' AND content_source.distribution='public_shareable'""",
                (pillar,),
            ).fetchone()[0])
            for pillar in PILLARS
        }
        b100_rows = conn.execute(
            """SELECT r.category_name, COUNT(DISTINCT ev.entity_id) AS cnt
               FROM recognitions r
               JOIN evidence ev ON ev.id=r.evidence_id
               JOIN entities e ON e.id=ev.entity_id
               JOIN content_versions cv ON cv.id=ev.version_id
               JOIN contents c ON c.id=cv.content_id JOIN sources s ON s.id=c.source_id
               WHERE ev.public_shareable=1 AND e.public_shareable=1 AND c.public_shareable=1
                 AND c.entity_id=ev.entity_id AND c.source_id=ev.source_id
                 AND s.access_level='public' AND s.distribution='public_shareable'
                 AND ev.verification_level='official' AND r.result='Awardee'
                 AND r.program_name='Blockchain 100 2025'
               GROUP BY r.category_name"""
        ).fetchall()
        b100_categories = {str(row["category_name"]): int(row["cnt"]) for row in b100_rows if row["category_name"]}
    checked_at = datetime.now(timezone.utc)
    try:
        window_start = checked_at.replace(year=checked_at.year - 2)
    except ValueError:
        window_start = checked_at.replace(year=checked_at.year - 2, day=28)
    # Summary reads only identity/clocks/language, never complete article analysis or dossiers.
    with connect(db_path) as conn:
        summary_rows = conn.execute(
            """SELECT c.canonical_url,c.published_at,c.first_seen_at,c.language,v.fetched_at
               FROM contents c JOIN entities e ON e.id=c.entity_id JOIN sources s ON s.id=c.source_id
               JOIN content_versions v ON v.id=c.latest_version_id AND v.content_id=c.id
               WHERE c.public_shareable=1 AND e.public_shareable=1
                 AND s.access_level='public' AND s.distribution='public_shareable'
                 AND s.source_type IN ('binance_square_public_feed','binance_square_creator_posts','binance_square_public_archive')
               ORDER BY v.fetched_at DESC,c.id DESC"""
        ).fetchall()
    unique_rows: dict[str, dict[str, Any]] = {}
    for summary in summary_rows:
        published = _parse_utc(summary["published_at"])
        if published is not None and window_start.date() <= published.date() <= checked_at.date() and published <= checked_at:
            unique_rows.setdefault(summary["canonical_url"].rstrip("/"), dict(summary))
    summary_items = list(unique_rows.values())
    languages: dict[str, int] = {}
    for summary in summary_items:
        language = (summary["language"] or "unknown").lower()
        languages[language] = languages.get(language, 0) + 1
    article_data = {
        "items": summary_items, "sample_count": len(summary_items),
        "as_of": max((item["fetched_at"] for item in summary_items), default=None),
        "vietnamese_summary": {"vi_native_count": languages.get("vi", 0),
                               "keyword_tagged_articles": len(summary_items), "language_breakdown": languages},
    }
    current_year = checked_at.year
    target_years = [str(current_year - 2), str(current_year - 1), str(current_year)]
    observed_by_year = {
        year: sum(
            1 for item in article_data["items"]
            if str(item.get("published_at") or "").startswith(year)
        )
        for year in target_years
    }
    content_ext = data.get("content_extraction") or {}
    pub_cov = content_ext.get("publication_coverage") or {}
    cooldown_info = content_ext.get("archive_host_cooldown")
    vi_summary = article_data.get("vietnamese_summary") or {}
    data.update({
        "dossier_count": dossier_count,
        "freshness": {
            "latest_first_seen_at": max((item["first_seen_at"] for item in summary_items), default=None),
            "latest_published_at": max((item["published_at"] for item in summary_items), default=None),
            "basis": "public retained articles; observation is not publication",
            "coverage": "partial",
        },
        "draft_count": int(data["counts"].get("output_artifacts", 0)),
        "pillar_coverage": pillar_coverage,
        "blockchain100_roster": {
            "total_official_awardees": sum(b100_categories.values()),
            "categories": b100_categories,
        },
        "scanner": scanner_public_status(db_path, volume_status=content_ext),
        "vietnamese_portal": {
            "priority_mode": "vi_first",
            "vi_native_articles": vi_summary.get("vi_native_count", 0),
            "keyword_tagged_articles": vi_summary["keyword_tagged_articles"],
            "language_breakdown": vi_summary.get("language_breakdown", {}),
            "top_assets": vi_summary.get("top_assets", []),
            "topic_counts": vi_summary.get("topic_counts", {}),
            "summary_basis": "stored language metadata; asset/topic analysis available on filtered article pages",
            "goal_and_backlog_vi": {
                "target_quality_bodies": 1000,
                "quality_audited_bodies": content_ext.get("quality_audited_bodies", 0),
                "total_unique_qualifying_bodies": content_ext.get("total_unique_qualifying_bodies", article_data["sample_count"]),
                "recent_7d_bodies": pub_cov.get("trailing_7d", 0),
                "recent_30d_bodies": pub_cov.get("trailing_30d", 0),
                "historical_archive_bodies": pub_cov.get("older_within_two_year_window", 0),
                "current_extraction_status": content_ext.get("current_extraction_status", content_ext.get("status", "unknown")),
                "lifetime_extraction_status": content_ext.get("lifetime_status", content_ext.get("status", "unknown")),
                "archive_cooldown": cooldown_info,
                "last_real_extraction_at": content_ext.get("last_successful_content_extraction_at"),
                "notices_vi": [
                    f"Mục tiêu 1.000 bài kiểm định chất lượng: {'ĐÃ ĐẠT' if content_ext.get('quality_audited_bodies', 0) >= 1000 else 'CHƯA ĐẠT'} ({content_ext.get('quality_audited_bodies', 0)}/1.000).",
                    "Bài viết lưu trữ lịch sử (cửa sổ 2 năm) được tách riêng khỏi bài mới 7/30 ngày gần nhất; không tính bài archive cũ là bài mới trong tháng.",
                    "Tương tác (Likes/Comments/Shares) phản ánh mức độ chú ý của cộng đồng, KHÔNG phải bằng chứng lợi nhuận giao dịch.",
                    "Giải thưởng Blockchain 100 là công nhận nội dung/cộng đồng từ Binance, KHÔNG phải chứng nhận kiểm toán PnL.",
                ],
            },
        },
        "research_coverage": {
            "requested_window": {
                "start": window_start.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "end": checked_at.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "basis": "trailing_two_calendar_years",
            },
            "target_calendar_years": target_years,
            "observed_article_counts": observed_by_year,
            "sample_count": article_data["sample_count"],
            "as_of": article_data["as_of"],
            "observed_months": sorted({
                item["published_at"][:7] for item in article_data["items"] if item.get("published_at")
            }),
            "unknown_original_date_count": sum(
                1 for item in article_data["items"] if not item.get("published_at")
            ),
            "status": "partial",
            "definition": "Observed retained records inside the requested trailing two-calendar-year window only; coverage remains partial and completeness has not been verified.",
        },
        "warnings": [
            "Không hồ sơ nào được xem là bằng chứng thành công giao dịch nếu thiếu performance cấp platform.",
            "Tương tác cao không chứng minh luận điểm đúng hoặc lợi nhuận tương lai.",
            "Giải content/community không được quy đổi thành giải trading.",
        ],
    })
    return data


def public_export(db_path: str | Path) -> dict[str, Any]:
    return {
        "generated_at": utc_now(),
        "overview": overview(db_path),
        "sources": sources(db_path),
        "discovery": public_crawler_status(db_path),
        "dossiers": dossiers(db_path, include_official=True),
        "articles": articles(db_path),
        "evidence": evidence_ledger(db_path),
        "drafts": drafts(db_path),
    }
