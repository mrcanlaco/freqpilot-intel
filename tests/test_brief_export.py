"""Synthetic fixtures only: verify permission boundaries and brief semantics."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from bti.brief import BRIEF_SCHEMA, build_intelligence_brief
from bti.cli import ROOT, main as cli_main
from bti.db import connect, init_db, public_excerpt
from bti.research import research_brief, store_source_claim

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


class BriefExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "brief.sqlite3"
        init_db(self.db)
        self._seq = 0

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _seed_record(
        self,
        *,
        slug: str,
        display_name: str,
        title: str,
        body: str,
        published_at: str,
        likes: int | None = 25,
        comments: int | None = 5,
        shares: int | None = 2,
        views: int | None = None,
        source_access: str = "public",
        source_distribution: str = "public_shareable",
        entity_public: int = 1,
        content_public: int = 1,
        claim_public: int = 1,
        with_award: bool = False,
    ) -> dict[str, int]:
        self._seq += 1
        idx = self._seq
        stamp = "2026-11-14T10:00:00Z"
        excerpt = public_excerpt(body)
        with connect(self.db) as conn:
            src_row = conn.execute("SELECT id FROM sources WHERE slug=?", (f"src-{idx}",)).fetchone()
            if src_row:
                source_id = int(src_row["id"])
            else:
                source_id = int(conn.execute(
                    """INSERT INTO sources(slug,name,url,fetch_url,source_type,adapter,access_level,distribution,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        f"src-{idx}",
                        f"Synthetic Source {idx}",
                        f"https://www.binance.com/en/square/source-{idx}",
                        f"https://www.binance.com/en/square/source-{idx}",
                        "binance_square_creator_posts",
                        "square_user_posts",
                        source_access,
                        source_distribution,
                        stamp,
                        stamp,
                    ),
                ).lastrowid)
            ent_row = conn.execute("SELECT id FROM entities WHERE slug=?", (slug,)).fetchone()
            if ent_row:
                entity_id = int(ent_row["id"])
            else:
                category = "official_trader_awardee" if with_award else "square_creator_candidate"
                entity_id = int(conn.execute(
                    """INSERT INTO entities(slug,display_name,category,username,profile_url,public_shareable,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        slug,
                        display_name,
                        category,
                        slug,
                        f"https://www.binance.com/en/square/profile/{slug}",
                        entity_public,
                        stamp,
                        stamp,
                    ),
                ).lastrowid)
            content_id = int(conn.execute(
                """INSERT INTO contents(source_id,entity_id,external_id,canonical_url,title,author,language,
                                        published_at,first_seen_at,public_shareable,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    source_id,
                    entity_id,
                    str(100000 + idx),
                    f"https://www.binance.com/en/square/post/{100000 + idx}",
                    title,
                    display_name,
                    "vi",
                    published_at,
                    stamp,
                    content_public,
                    stamp,
                    stamp,
                ),
            ).lastrowid)
            version_id = int(conn.execute(
                """INSERT INTO content_versions(content_id,version_no,fetched_at,checksum,normalized_text,excerpt,metadata_json)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    content_id,
                    1,
                    stamp,
                    f"sha256-{idx}",
                    body,
                    excerpt,
                    json.dumps({"provenance_type": "direct_public_response"}),
                ),
            ).lastrowid)
            conn.execute("UPDATE contents SET latest_version_id=? WHERE id=?", (version_id, content_id))
            eng_value = {"likes": likes, "comments": comments, "shares": shares, "views": views}
            conn.execute(
                """INSERT INTO evidence(source_id,entity_id,version_id,pillar,kind,verification_level,observed_at,
                                        value_json,quote,methodology,limitations,dependency_group,public_shareable)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    source_id,
                    entity_id,
                    version_id,
                    "engagement",
                    "post_metrics",
                    "platform",
                    stamp,
                    json.dumps(eng_value),
                    excerpt,
                    "Synthetic engagement snapshot",
                    "Snapshot interactions do not prove trading PnL.",
                    f"eng-{idx}",
                    1,
                ),
            )
            if with_award:
                rec_ev_id = int(conn.execute(
                    """INSERT INTO evidence(source_id,entity_id,version_id,pillar,kind,verification_level,observed_at,
                                            period_label,value_json,quote,methodology,limitations,dependency_group,public_shareable)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        source_id,
                        entity_id,
                        version_id,
                        "recognition",
                        "official_award",
                        "official",
                        stamp,
                        "2026",
                        json.dumps({"program": "Blockchain 100", "category_name": "Trader", "rank": 7}),
                        excerpt,
                        "Synthetic award fixture",
                        "Content/influence recognition is not audited PnL.",
                        f"rec-{idx}",
                        1,
                    ),
                ).lastrowid)
                conn.execute(
                    """INSERT INTO recognitions(evidence_id,program_name,category_type,category_name,result,
                                                period_label,known_at,interpretation_limit)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        rec_ev_id,
                        "Blockchain 100",
                        "trading",
                        "Trader",
                        "Awardee",
                        "2026",
                        stamp,
                        "Award recognition does not establish audited trading profitability.",
                    ),
                )
            brief = research_brief(title, body)
            claim_id = store_source_claim(
                conn,
                entity_id=entity_id,
                source_id=source_id,
                version_id=version_id,
                brief=brief,
                claim_type="opinion",
                public=claim_public,
                created_at=stamp,
            )
            conn.commit()
        return {
            "source_id": source_id,
            "entity_id": entity_id,
            "content_id": content_id,
            "version_id": version_id,
            "claim_id": claim_id or 0,
        }

    def test_brief_extracts_digest_scenarios_and_three_pillar_spotlights_without_body_leak(self) -> None:
        tail_secret = " ".join(["TAIL_PRIVATE_BODY_MARKER"] * 40)
        self._seed_record(
            slug="creator-alpha",
            display_name="Alpha Research VN",
            title="Kịch bản quản trị rủi ro $BTC tuần tới",
            body=(
                "Synthetic commentary for public research testing with twenty six benign introductory "
                "words before the conditional setup so full body text beyond the excerpt stays local. "
                f"If BTC closes above 95000 daily, buy BTC unless price falls below 91000. {tail_secret}"
            ),
            published_at="2026-11-12T08:00:00Z",
            likes=40,
            comments=10,
            shares=5,
            views=None,
            with_award=True,
        )
        # Older than 7d window — must be excluded from 7d brief
        self._seed_record(
            slug="creator-old",
            display_name="Old Archive Creator",
            title="Phân tích $ETH tháng trước",
            body="If ETH closes above 4000 daily, buy ETH unless price falls below 3700.",
            published_at="2026-10-20T08:00:00Z",
        )

        first = build_intelligence_brief(self.db, window="7d", now=NOW)
        second = build_intelligence_brief(self.db, window="7d", now=NOW)

        self.assertEqual(first["schema"], BRIEF_SCHEMA)
        self.assertEqual(first["brief_id"], second["brief_id"])
        self.assertEqual(first["allowed_assets"], ["BTC"])
        self.assertNotIn("ETH", first["allowed_assets"])

        serialized = json.dumps(first, ensure_ascii=False)
        self.assertNotIn("TAIL_PRIVATE_BODY_MARKER", serialized)

        digest = first["sections"]["market_digest"]
        self.assertTrue(digest["eligible"])
        self.assertEqual(digest["sample_count"], 1)
        self.assertEqual(digest["notable_articles"][0]["total_interactions"], 55)
        self.assertIsNone(digest["notable_articles"][0]["engagement"]["views"])

        scenarios = first["sections"]["conditional_scenarios"]
        self.assertTrue(scenarios["eligible"])
        self.assertEqual(len(scenarios["items"]), 1)
        sc = scenarios["items"][0]
        self.assertEqual(sc["asset"], "BTC")
        self.assertEqual(sc["horizon"], "daily")
        self.assertIn("above 95000", sc["trigger"] or "")
        self.assertIn("below 91000", sc["invalidation"] or "")
        self.assertIn("Verified trading performance", sc["missing_fields"])

        spotlights = first["sections"]["creator_spotlights"]
        self.assertTrue(spotlights["eligible"])
        sp = spotlights["items"][0]
        self.assertEqual(sp["slug"], "creator-alpha")
        self.assertFalse(sp["three_pillars"]["performance"]["has_platform_verified_pnl"])
        self.assertEqual(sp["three_pillars"]["recognition"]["status"], "available")

    def test_private_or_revoked_dependencies_never_leak_into_brief(self) -> None:
        self._seed_record(
            slug="private-content-user",
            display_name="Private Content",
            title="Bí mật $SOL",
            body="If SOL closes above 250 daily, buy SOL unless price falls below 220. SECRET_CONTENT_MARKER",
            published_at="2026-11-13T08:00:00Z",
            content_public=0,
        )
        self._seed_record(
            slug="internal-source-user",
            display_name="Internal Source",
            title="Nội bộ $ADA",
            body="If ADA closes above 2 daily, buy ADA unless price falls below 1. SECRET_SOURCE_MARKER",
            published_at="2026-11-13T08:00:00Z",
            source_distribution="internal_only",
        )
        revoked = self._seed_record(
            slug="revoked-claim-user",
            display_name="Public Post With Revoked Claim",
            title="Cập nhật dòng tiền $BNB",
            body="If BNB closes above 700 daily, buy BNB unless price falls below 650.",
            published_at="2026-11-13T09:00:00Z",
            claim_public=0,
        )
        self.assertGreater(revoked["claim_id"], 0)

        brief = build_intelligence_brief(self.db, window="7d", now=NOW)
        payload = json.dumps(brief, ensure_ascii=False)
        self.assertNotIn("SECRET_CONTENT_MARKER", payload)
        self.assertNotIn("SECRET_SOURCE_MARKER", payload)
        self.assertNotIn("SOL", brief["allowed_assets"])
        self.assertNotIn("ADA", brief["allowed_assets"])
        # Revoked claim must not appear as a conditional scenario or research_statement
        self.assertEqual(brief["sections"]["conditional_scenarios"]["items"], [])
        self.assertIsNone(brief["sections"]["market_digest"]["notable_articles"][0]["research_statement"])

    def test_cli_export_brief_stdout_and_root_containment(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_main(["--db", str(self.db), "export-brief", "--window", "7d"])
        self.assertEqual(rc, 0)
        parsed = json.loads(buf.getvalue())
        self.assertEqual(parsed["schema"], BRIEF_SCHEMA)

        outside_path = Path(self.temp.name) / "outside.json"
        with self.assertRaises(SystemExit):
            cli_main(["--db", str(self.db), "export-brief", "--output", str(outside_path)])

        inside_dir = ROOT / "runtime" / "test-tmp"
        inside_dir.mkdir(parents=True, exist_ok=True)
        inside_file = inside_dir / "test_brief_cli_output.json"
        try:
            out_buf = io.StringIO()
            with redirect_stdout(out_buf):
                rc2 = cli_main(["--db", str(self.db), "export-brief", "--output", str(inside_file)])
            self.assertEqual(rc2, 0)
            self.assertTrue(inside_file.exists())
            saved = json.loads(inside_file.read_text(encoding="utf-8"))
            self.assertEqual(saved["schema"], BRIEF_SCHEMA)
        finally:
            inside_file.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
