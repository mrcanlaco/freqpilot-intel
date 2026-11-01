from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import connect, json_load, utc_now
from .research import migrate_legacy_title_claim, permitted_source_claims, research_brief


PUBLIC_ARTICLE_SOURCE_TYPES = (
    "binance_square_creator_posts",
    "binance_square_public_feed",
    "binance_square_public_archive",
)
GENERATOR = "grounded_research_v1"


def _citation(row: Any) -> dict[str, Any]:
    metadata = json_load(row["metadata_json"], {})
    return {
        "title": row["title"], "author": row["display_name"],
        "url": row["canonical_url"], "content_id": row["content_id"],
        "version_id": row["version_id"], "checksum": row["checksum"],
        "source_snapshot_url": metadata.get("archive_snapshot_url") or row["source_url"],
        "provenance_type": metadata.get("provenance_type", "direct_public_response"),
        "version": row["version_no"], "published_at": row["published_at"],
        "first_seen_at": row["first_seen_at"], "fetched_at": row["fetched_at"],
    }


def _timestamp(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed and parsed.tzinfo else None


def build_drafts(db_path: str | Path, *, now: datetime | None = None) -> dict[str, int]:
    """Create cited source cards and only source-complete conditional research.

    Selection uses the current version and a trailing two-calendar-year original
    publication window, never body length, title claims or a five-article quota.
    Source excerpts remain unverified source statements, not analyst conclusions.
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("Research draft time must be timezone-aware")
    current = current.astimezone(timezone.utc)
    try:
        start = current.replace(year=current.year - 2)
    except ValueError:
        start = current.replace(year=current.year - 2, day=28)
    placeholders = ",".join("?" for _ in PUBLIC_ARTICLE_SOURCE_TYPES)
    counts = {"created_or_updated": 0, "analyst_dossiers": 0, "drafts": 0, "backlog": 0}
    # These are representative drafts, not an exhaustive second copy of the corpus.
    caps = {"source_excerpt": 12, "research_backlog": 12, "strategy_hypothesis": 6, "market_scenario": 6}
    selected = dict.fromkeys(caps, 0)
    with connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT c.id AS content_id,c.entity_id,c.source_id,c.title,c.canonical_url,c.published_at,c.first_seen_at,
                   cv.id AS version_id,cv.version_no,cv.fetched_at,cv.checksum,
                   cv.normalized_text,cv.metadata_json,e.display_name,s.url AS source_url
            FROM contents c JOIN entities e ON e.id=c.entity_id
            JOIN sources s ON s.id=c.source_id
            JOIN content_versions cv ON cv.id=c.latest_version_id AND cv.content_id=c.id
            WHERE c.public_shareable=1 AND e.public_shareable=1
              AND s.access_level='public' AND s.distribution='public_shareable'
              AND s.source_type IN ({placeholders})
              AND date(c.published_at) BETWEEN date(?) AND date(?)
            ORDER BY c.published_at DESC,c.id DESC LIMIT 200
            """, (*PUBLIC_ARTICLE_SOURCE_TYPES, start.isoformat(), current.isoformat()),
        ).fetchall()
        stamp = utc_now()
        # Internal-only artifacts may be deliberately revoked; regeneration cannot reapprove them.
        existing = conn.execute("SELECT id,artifact_type,title,status,distribution,body_json FROM output_artifacts").fetchall()
        blocked = {(old["artifact_type"], old["title"]) for old in existing
                   if old["distribution"] != "public_shareable" or old["status"] != "draft"}
        # Generation activity and distribution permission are independent state.
        for old in existing:
            body = json_load(old["body_json"], {})
            if body.get("generator") == GENERATOR:
                body["generation_active"] = False
                conn.execute("UPDATE output_artifacts SET body_json=?,updated_at=? WHERE id=?",
                             (json.dumps(body, ensure_ascii=False, sort_keys=True), stamp, old["id"]))
            elif body.get("derived_output"):
                conn.execute("UPDATE output_artifacts SET distribution='internal_only',updated_at=? WHERE id=?", (stamp, old["id"]))
        for row in rows:
            published = _timestamp(row["published_at"])
            observed = _timestamp(row["fetched_at"])
            first_seen = _timestamp(row["first_seen_at"])
            if not published or not observed or not first_seen:
                continue
            if not (start <= published <= current) or observed > current or first_seen > current:
                continue
            brief = research_brief(row["title"], row["normalized_text"])
            migrate_legacy_title_claim(conn, row, brief, stamp)
            allowed_claims = permitted_source_claims(conn, row["version_id"], row["entity_id"], brief["statement"])
            claim_id = allowed_claims[0]["id"] if allowed_claims else None
            # A known but revoked claim cannot be bypassed through a claimless card.
            if claim_id is None and conn.execute(
                "SELECT 1 FROM claim_versions WHERE version_id=? LIMIT 1", (row["version_id"],)
            ).fetchone():
                continue
            kinds = []
            if brief["statement"] is not None and claim_id is not None:
                kinds.append(("content", "source_excerpt"))
            if brief["strategy_eligible"] and claim_id is not None:
                kinds.append(("strategy_hypothesis", "strategy_hypothesis"))
            if brief["market_eligible"] and claim_id is not None:
                kinds.append(("market_scenario", "market_scenario"))
            if not brief["strategy_eligible"] or not brief["market_eligible"] or claim_id is None:
                kinds.append(("content", "research_backlog"))
            for artifact_type, kind in kinds:
                title = f"{kind}: article {row['content_id']} v{row['version_no']} — {row['title'][:82]}"
                if (artifact_type, title) in blocked:
                    continue
                if selected[kind] >= caps[kind]:
                    continue
                selected[kind] += 1
                body = {
                    "generator": GENERATOR, "generation_active": True, "derived_output": True, "output_kind": kind,
                    "derived_label": "Deterministic source extraction — unverified research draft",
                    "research_brief": brief, "source_excerpt": brief["quote"],
                    "source_claim": brief["statement"], "citations": [_citation(row)],
                    "date_scope": {"original_publication_at": row["published_at"], "source_observed_at": row["fetched_at"]},
                    "unknowns": list(brief["missing_fields"]) + ["Independent accuracy", "Verified trading performance", "Fees, sizing and validation"],
                    "disclaimer": "Verbatim source text, not a summary or translation. Not independently verified performance, investment advice or a trading signal. No automatic publishing.",
                }
                summary = brief["reason"] if kind == "research_backlog" else "Source-cited extraction; source conditions are not independently validated."
                conn.execute(
                    """INSERT INTO output_artifacts(artifact_type,title,summary,body_json,status,distribution,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)
                       ON CONFLICT(artifact_type,title) DO UPDATE SET summary=excluded.summary,
                         body_json=excluded.body_json,status='draft',distribution=excluded.distribution,updated_at=excluded.updated_at""",
                    (artifact_type, title, summary, json.dumps(body, ensure_ascii=False, sort_keys=True), "draft", "public_shareable", stamp, stamp),
                )
                artifact_id = conn.execute("SELECT id FROM output_artifacts WHERE artifact_type=? AND title=?", (artifact_type, title)).fetchone()[0]
                conn.execute("DELETE FROM artifact_claims WHERE artifact_id=?", (artifact_id,))
                if claim_id is not None:
                    conn.execute("INSERT INTO artifact_claims(artifact_id,claim_id) VALUES(?,?)", (artifact_id, claim_id))
                counts["created_or_updated"] += 1
                counts["backlog" if kind == "research_backlog" else "analyst_dossiers" if kind == "source_excerpt" else "drafts"] += 1
        conn.commit()
    return counts
