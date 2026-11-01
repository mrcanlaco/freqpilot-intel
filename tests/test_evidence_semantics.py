from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bti.db import connect, init_db
from bti.public import dossier


class EvidenceSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "semantics.sqlite3"
        init_db(self.db)
        now = "2026-10-18T00:00:00Z"
        with connect(self.db) as conn:
            self.source = conn.execute(
                """INSERT INTO sources(slug,name,url,fetch_url,source_type,adapter,access_level,distribution,created_at,updated_at)
                   VALUES('public-source','Public source','https://www.binance.com/source','https://www.binance.com/source',
                   'test','html','public','public_shareable',?,?)""", (now, now)
            ).lastrowid
            self.entity = conn.execute(
                """INSERT INTO entities(slug,display_name,category,public_shareable,created_at,updated_at)
                   VALUES('candidate','Candidate','square_creator_candidate',1,?,?)""", (now, now)
            ).lastrowid
            self.content = conn.execute(
                """INSERT INTO contents(source_id,entity_id,external_id,canonical_url,title,first_seen_at,public_shareable,created_at,updated_at)
                   VALUES(?,?,'post','https://www.binance.com/post','Neutral market note',?,1,?,?)""",
                (self.source, self.entity, now, now, now),
            ).lastrowid
            self.version = conn.execute(
                """INSERT INTO content_versions(content_id,version_no,fetched_at,checksum,normalized_text,excerpt,metadata_json)
                   VALUES(?,1,?,'hash','Neutral market note','Neutral market note','{}')""", (self.content, now)
            ).lastrowid
            conn.execute("UPDATE contents SET latest_version_id=? WHERE id=?", (self.version, self.content))
            self.engagement = conn.execute(
                """INSERT INTO evidence(source_id,entity_id,version_id,pillar,kind,verification_level,observed_at,value_json,
                   methodology,limitations,dependency_group,public_shareable) VALUES(?,?,?,'engagement','post_metrics','platform',?,?,
                   'platform fields','attention is not performance','same-origin',1)""",
                (self.source, self.entity, self.version, now, json.dumps({"views": 1_000_000, "likes": 50_000})),
            ).lastrowid
            conn.commit()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_high_engagement_does_not_imply_performance(self) -> None:
        item = dossier(self.db, "candidate")
        self.assertIsNotNone(item)
        self.assertEqual(item["pillars"]["engagement"]["status"], "available")
        self.assertEqual(item["pillars"]["performance"]["status"], "unknown")
        self.assertEqual(item["pillars"]["performance"]["items"], [])

    def test_dependency_group_is_counted_once(self) -> None:
        now = "2026-10-18T00:00:01Z"
        with connect(self.db) as conn:
            conn.execute(
                """INSERT INTO evidence(source_id,entity_id,version_id,pillar,kind,verification_level,observed_at,value_json,
                   methodology,limitations,dependency_group,public_shareable) VALUES(?,?,?,'engagement','post_metrics_second','platform',?,?,
                   'same post','not independent','same-origin',1)""",
                (self.source, self.entity, self.version, now, json.dumps({"comments": 100})),
            )
            conn.commit()
        group = dossier(self.db, "candidate")["pillars"]["engagement"]
        self.assertEqual(len(group["items"]), 2)
        self.assertEqual(group["independent_source_count"], 1)

    def test_content_and_trading_recognition_categories_remain_distinct(self) -> None:
        now = "2026-10-18T00:00:02Z"
        with connect(self.db) as conn:
            for suffix, category in (("content", "content"), ("trading", "trading")):
                ev = conn.execute(
                    """INSERT INTO evidence(source_id,entity_id,version_id,pillar,kind,verification_level,observed_at,value_json,
                       methodology,limitations,dependency_group,public_shareable) VALUES(?,?,?,'recognition',?,'official',?,'{}',
                       'official page','category-specific only',?,1)""",
                    (self.source, self.entity, self.version, f"award-{suffix}", now, f"award-{suffix}"),
                ).lastrowid
                conn.execute(
                    """INSERT INTO recognitions(evidence_id,program_name,category_type,known_at,interpretation_limit)
                       VALUES(?,?,?,?,?)""", (ev, f"Award {suffix}", category, now, "No cross-category inference"),
                )
            conn.commit()
        with connect(self.db) as conn:
            categories = {row[0] for row in conn.execute("SELECT category_type FROM recognitions")}
            performance_count = conn.execute("SELECT COUNT(*) FROM evidence WHERE pillar='performance'").fetchone()[0]
        self.assertEqual(categories, {"content", "trading"})
        self.assertEqual(performance_count, 0)


if __name__ == "__main__":
    unittest.main()
