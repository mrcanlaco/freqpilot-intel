"""Conservative source extraction, not summarization, translation or trading advice."""
from __future__ import annotations

import re
from typing import Any

METHOD = "deterministic_source_excerpt_v1"
_ASSET = re.compile(r"\b(?:BTC|ETH|BNB|SOL|XRP|Bitcoin|Ethereum)\b", re.I)
_HORIZON = re.compile(r"\b(?:\d+\s*(?:minutes?|hours?|days?|weeks?|[mhdw])|daily|weekly|hourly|today|tomorrow|next\s+(?:day|week|month))\b", re.I)
_TRIGGER = re.compile(r"\b(?:if|when)\b.*?(?=,(?!\d)|;|[!?]|\.(?!\d)|\n|$)", re.I)
_INVALIDATION = re.compile(r"\b(?:unless|invalidated?\s+(?:if|below|above|at)|invalidation\s*(?:at|below|above|:)|stop(?:[- ]loss)?\s*(?:at|below|above|:))\b.*?(?=,(?!\d)|;|[!?]|\.(?!\d)|\n|$)", re.I)
_ENTRY = re.compile(r"(?:^|[,;]\s*|\bthen\s+)(?:(?:buy|sell|enter)\s+\$?(?:BTC|ETH|BNB|SOL|XRP|Bitcoin|Ethereum)\b|go\s+(?:long|short)\b|open\s+(?:a\s+)?(?:long|short)\s+position\b)", re.I)
_INSTRUCTION = re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|system)|system\s*prompt|developer\s+message|execute\s+(?:this|the)\s+(?:code|command)|<script|javascript:|reveal\s+(?:the\s+)?(?:secret|password|prompt)", re.I)


def research_brief(title: str, text: str) -> dict[str, Any]:
    """Select one bounded verbatim body sentence; absent fields stay unknown.

    The title never supplies facts; a repeated heading is excluded as a claim.
    One contiguous quote contains all extracted fields; raw private bodies stay private.
    Conditions need explicit numeric levels; eligibility means source completeness,
    not independently verified validity, profitability or backtest readiness.
    """
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for index, part in enumerate(re.split(r"(?<=[.!?])\s+|\n+", text or "")):
        quote = part.strip()
        if not (5 <= len(quote.split()) <= 24) or len(quote) > 240:
            continue
        if quote.casefold().rstrip(".!?") == (title or "").strip().casefold().rstrip(".!?"):
            continue
        if _INSTRUCTION.search(quote) or not re.search(r"[A-Za-zÀ-ỹ]{3}", quote):
            continue
        fields: dict[str, Any] = {}
        for name, pattern in (("asset", _ASSET), ("horizon", _HORIZON), ("trigger", _TRIGGER), ("invalidation", _INVALIDATION)):
            match = pattern.search(quote)
            fields[name] = match.group(0).rstrip(" ,;.") if match else None
        # A condition without a source-specified measurable level is not a rule.
        for name in ("trigger", "invalidation"):
            if fields[name] and not re.search(r"\b(?:above|below|at|over|under|crosses|breaks|reaches)\s+\$?\d", fields[name], re.I):
                fields[name] = None
        missing = [name for name, value in fields.items() if value is None]
        entry = bool(_ENTRY.search(quote)) and not re.search(r"\b(?:not|never|avoid|don't)\s+(?:buy|sell|enter|long|short)\b", quote, re.I)
        market = not missing
        if not entry:
            missing.append("entry_rule")
        brief = {
            "method": METHOD, "statement": quote, "quote": quote, **fields,
            "strategy_eligible": market and entry, "market_eligible": market,
            "missing_fields": missing,
            "reason": "Source-stated conditional setup; not independently verified." if market else "Insufficient source-stated setup fields: " + ", ".join(missing) + ".",
        }
        candidates.append((sum(value is not None for value in fields.values()) + int(entry), -index, brief))
    if candidates:
        return max(candidates, key=lambda item: (item[0], item[1]))[2]
    return {
        "method": METHOD, "statement": None, "quote": None,
        "asset": None, "horizon": None, "trigger": None, "invalidation": None,
        "strategy_eligible": False, "market_eligible": False,
        "missing_fields": ["statement", "asset", "horizon", "trigger", "invalidation", "entry_rule"],
        "reason": "No usable verbatim body sentence within the 24-word/240-character public excerpt limit.",
    }


def permitted_source_claims(conn: Any, version_id: int, entity_id: int, statement: str | None) -> list[Any]:
    """Authorize exactly this statement and every linked provenance dependency."""
    if statement is None:
        return []
    return _permitted_claims(conn, "link.version_id=? AND cl.entity_id=? AND cl.statement=?",
                             (version_id, entity_id, statement))


def permitted_body_claims(conn: Any, version_ids: list[int]) -> list[Any]:
    """Batch-authorize body-method candidates, without loading source bodies."""
    requested = set(version_ids)
    version_ids = [
        row["version_id"] for row in conn.execute(
            """SELECT DISTINCT link.version_id FROM claims cl
               JOIN claim_versions link ON link.claim_id=cl.id
               WHERE cl.confidence_label=? AND cl.public_shareable=1""", (METHOD,),
        ) if row["version_id"] in requested
    ]
    rows = []
    for start in range(0, len(version_ids), 400):
        batch = version_ids[start:start + 400]
        placeholders = ",".join("?" for _ in batch)
        rows.extend(_permitted_claims(
            conn, f"link.version_id IN ({placeholders}) AND cl.confidence_label=?",
            (*batch, METHOD),
        ))
    return rows


def _permitted_claims(conn: Any, selection: str, params: tuple[Any, ...]) -> list[Any]:
    return conn.execute(
        f"""SELECT DISTINCT cl.id,cl.entity_id,cl.claim_type,cl.statement,cl.created_at,
                            link.version_id FROM claims cl
           JOIN claim_versions link ON link.claim_id=cl.id
           WHERE {selection} AND cl.public_shareable=1
             AND NOT EXISTS (
               SELECT 1 FROM claim_versions dependency
               LEFT JOIN content_versions v ON v.id=dependency.version_id
               LEFT JOIN contents c ON c.id=v.content_id LEFT JOIN sources s ON s.id=c.source_id
               LEFT JOIN entities e ON e.id=c.entity_id
               WHERE dependency.claim_id=cl.id AND (
                 v.id IS NULL OR c.id IS NULL OR s.id IS NULL OR e.id IS NULL
                 OR c.public_shareable<>1 OR e.public_shareable<>1 OR c.entity_id<>cl.entity_id
                 OR s.access_level<>'public' OR s.distribution<>'public_shareable')
             )
             AND NOT EXISTS (
               SELECT 1 FROM claim_evidence dependency
               LEFT JOIN evidence ev ON ev.id=dependency.evidence_id
               LEFT JOIN content_versions v ON v.id=ev.version_id
               LEFT JOIN contents c ON c.id=v.content_id LEFT JOIN sources s ON s.id=c.source_id
               LEFT JOIN entities e ON e.id=ev.entity_id LEFT JOIN sources es ON es.id=ev.source_id
               WHERE dependency.claim_id=cl.id AND (
                 ev.id IS NULL OR v.id IS NULL OR c.id IS NULL OR s.id IS NULL OR e.id IS NULL OR es.id IS NULL
                 OR ev.public_shareable<>1 OR c.public_shareable<>1 OR e.public_shareable<>1
                 OR ev.entity_id<>cl.entity_id OR c.entity_id<>ev.entity_id OR c.source_id<>ev.source_id
                 OR es.access_level<>'public' OR es.distribution<>'public_shareable'
                 OR s.access_level<>'public' OR s.distribution<>'public_shareable')
             ) ORDER BY cl.id""", params,
    ).fetchall()


def missing_public_research() -> dict[str, Any]:
    return {
        "method": METHOD, "missing_fields": ["public_claim"],
        "reason": "Chưa có luận điểm được phép công khai đủ căn cứ; không suy luận từ tiêu đề.",
    }


def discovery_research(conn: Any, version_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Load only permitted body-method candidates, in bounded transient batches.

    Authorization of some other sentence must never authorize the extractor's
    preferred sentence. No body or permission result survives this request.
    """
    claims = permitted_body_claims(conn, version_ids)
    statements: dict[int, set[str]] = {}
    for claim in claims:
        statements.setdefault(claim["version_id"], set()).add(claim["statement"])
    result = {}
    candidates = list(statements)
    for start in range(0, len(candidates), 400):
        batch = candidates[start:start + 400]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"""SELECT cv.id,c.title,cv.normalized_text FROM content_versions cv
                JOIN contents c ON c.id=cv.content_id
                WHERE cv.id IN ({placeholders})""", batch,
        )
        for row in rows:
            brief = research_brief(row["title"] or "", row["normalized_text"] or "")
            if brief["statement"] in statements[row["id"]]:
                result[row["id"]] = brief
    return result


def store_source_claim(conn: Any, *, entity_id: int, source_id: int, version_id: int,
                       brief: dict[str, Any], claim_type: str, public: int, created_at: str) -> int | None:
    if brief["statement"] is None:
        return None
    conn.execute(
        """INSERT OR IGNORE INTO claims(entity_id,claim_type,statement,asset,horizon,trigger_text,
           invalidation_text,confidence_label,scorable,public_shareable,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (entity_id, claim_type, brief["statement"], brief["asset"], brief["horizon"],
         brief["trigger"], brief["invalidation"], brief["method"], 0, public, created_at),
    )
    claim = conn.execute("SELECT id FROM claims WHERE entity_id=? AND statement=?", (entity_id, brief["statement"])).fetchone()
    claim_id = int(claim["id"])
    conn.execute("INSERT OR IGNORE INTO claim_versions(claim_id,version_id) VALUES(?,?)", (claim_id, version_id))
    conn.execute(
        """INSERT OR IGNORE INTO claim_evidence(claim_id,evidence_id,dependency_group)
           SELECT ?,id,dependency_group FROM evidence WHERE source_id=? AND entity_id=? AND version_id=?""",
        (claim_id, source_id, entity_id, version_id),
    )
    return claim_id


def migrate_legacy_title_claim(conn: Any, row: Any, brief: dict[str, Any], stamp: str) -> None:
    """Cut over only unrevoked title-only legacy records; never bypass a real claim."""
    if not brief["statement"] or brief["statement"] == row["title"]:
        return
    old = conn.execute(
        """SELECT cl.id,cl.statement,cl.confidence_label,cl.claim_type FROM claims cl
           JOIN claim_versions link ON link.claim_id=cl.id WHERE link.version_id=? AND cl.entity_id=?""",
        (row["version_id"], row["entity_id"]),
    ).fetchall()
    if not old or any(cl["statement"] != row["title"] or cl["confidence_label"] != "source_text_only" for cl in old):
        return
    allowed = permitted_source_claims(conn, row["version_id"], row["entity_id"], row["title"])
    if len(allowed) != len(old):
        return
    existing = conn.execute("SELECT public_shareable FROM claims WHERE entity_id=? AND statement=?",
                            (row["entity_id"], brief["statement"])).fetchone()
    if existing is not None and not existing["public_shareable"]:
        return
    claim_id = store_source_claim(conn, entity_id=row["entity_id"], source_id=row["source_id"],
                                  version_id=row["version_id"], brief=brief, claim_type=old[0]["claim_type"],
                                  public=1, created_at=stamp)
    for legacy in old:
        conn.execute(
            """INSERT OR IGNORE INTO claim_evidence(claim_id,evidence_id,dependency_group)
               SELECT ?,evidence_id,dependency_group FROM claim_evidence WHERE claim_id=?""",
            (claim_id, legacy["id"]),
        )
        conn.execute("UPDATE claims SET public_shareable=0,confidence_label='retired_title_claim' WHERE id=?", (legacy["id"],))
