from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from bti.db import connect, init_db
from bti.public import articles, dossier_listing
from bti.server import create_server


TEST_TMP = Path(__file__).resolve().parents[1] / "runtime" / "test-tmp"
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


class EngagementWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TMP)
        self.db = Path(self.temp.name) / "engagement.sqlite3"
        init_db(self.db)
        self.entities: dict[str, tuple[int, int]] = {}
        self.add_entity("alpha", "Alpha Research")
        self.add_entity("beta", "Beta Research")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def add_entity(self, slug: str, display_name: str) -> None:
        now = "2026-10-01T00:00:00Z"
        with connect(self.db) as conn:
            source_id = int(conn.execute(
                """INSERT INTO sources(
                       slug,name,url,fetch_url,source_type,adapter,access_level,distribution,
                       capability_status,created_at,updated_at
                   ) VALUES(?,?,?,?,?,'square_detail','public','public_shareable','available',?,?)""",
                (f"source-{slug}", f"Source {slug}", f"https://www.binance.com/en/square/profile/{slug}",
                 f"https://www.binance.com/en/square/profile/{slug}",
                 "binance_square_creator_posts", now, now),
            ).lastrowid)
            entity_id = int(conn.execute(
                """INSERT INTO entities(
                       slug,display_name,category,source_uid,username,identity_status,
                       identity_evidence_url,public_shareable,created_at,updated_at
                   ) VALUES(?,?, 'square_creator_candidate',?,?, 'source_profile_only',?,1,?,?)""",
                (f"entity-{slug}", display_name, f"uid-{slug}", slug,
                 f"https://www.binance.com/en/square/profile/{slug}", now, now),
            ).lastrowid)
            conn.commit()
        self.entities[slug] = (source_id, entity_id)

    def add_article(
        self,
        entity: str,
        post_id: str,
        *,
        published_at: str | None,
        observed_at: str,
        likes: int | float | None,
        comments: int | float | None,
        shares: int | float | None,
        views: int | float | None,
        title: str | None = None,
        content_public: bool = True,
        source_public: bool = True,
    ) -> None:
        source_id, entity_id = self.entities[entity]
        access = "public" if source_public else "private"
        distribution = "public_shareable" if source_public else "internal_only"
        with connect(self.db) as conn:
            conn.execute(
                "UPDATE sources SET access_level=?,distribution=? WHERE id=?",
                (access, distribution, source_id),
            )
            share = 1 if content_public else 0
            content_id = int(conn.execute(
                """INSERT INTO contents(
                       source_id,entity_id,external_id,canonical_url,title,author,published_at,
                       first_seen_at,public_shareable,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (source_id, entity_id, post_id, f"https://www.binance.com/en/square/post/{post_id}",
                 title or f"Research {post_id}", f"Author {entity}", published_at,
                 observed_at, share, observed_at, observed_at),
            ).lastrowid)
            version_id = int(conn.execute(
                """INSERT INTO content_versions(
                       content_id,version_no,fetched_at,checksum,normalized_text,excerpt,metadata_json
                   ) VALUES(?,1,?,?,?,?, '{}')""",
                (content_id, observed_at, f"hash-{post_id}", f"Useful body {post_id}", f"Excerpt {post_id}"),
            ).lastrowid)
            conn.execute("UPDATE contents SET latest_version_id=? WHERE id=?", (version_id, content_id))
            conn.execute(
                """INSERT INTO evidence(
                       source_id,entity_id,version_id,pillar,kind,verification_level,observed_at,
                       value_json,methodology,limitations,dependency_group,public_shareable
                   ) VALUES(?,?,?,'engagement','post_metrics','platform',?,?,?,?,?,?)""",
                (source_id, entity_id, version_id, observed_at,
                 json.dumps({"likes": likes, "comments": comments, "shares": shares, "views": views}),
                 "latest public snapshot", "snapshot counts only", f"post:{post_id}", share),
            )
            conn.commit()

    def seed_window_fixture(self) -> None:
        self.add_article(
            "alpha", "recent-complete", published_at="2026-10-17T10:00:00Z",
            observed_at="2026-10-17T11:00:00Z", likes=10, comments=2, shares=3, views=100,
        )
        self.add_article(
            "alpha", "recent-partial", published_at="2026-10-15T10:00:00Z",
            observed_at="2026-10-15T11:00:00Z", likes=None, comments=4, shares=1, views=50,
        )
        self.add_article(
            "alpha", "older-complete", published_at="2026-10-01T10:00:00Z",
            observed_at="2026-10-01T11:00:00Z", likes=20, comments=0, shares=5, views=200,
        )
        self.add_article(
            "beta", "recent-unknown", published_at="2026-10-16T10:00:00Z",
            observed_at="2026-10-16T11:00:00Z", likes=None, comments=None, shares=None, views=None,
        )

    def test_profile_7d_30d_custom_arithmetic_and_unknown_last(self) -> None:
        self.seed_window_fixture()
        seven = dossier_listing(self.db, window="7d", sort="likes_total", now=NOW)
        self.assertEqual([item["slug"] for item in seven["items"]], ["entity-alpha", "entity-beta"])
        alpha = seven["items"][0]["engagement_window"]
        self.assertEqual(alpha["sample_n"], 2)
        self.assertEqual(alpha["metrics"]["likes"], {
            "total": 10, "average": 10.0, "known_n": 1, "unknown_n": 1, "coverage_ratio": 0.5,
        })
        self.assertEqual(alpha["metrics"]["comments"]["total"], 6)
        self.assertEqual(alpha["metrics"]["comments"]["average"], 3)
        self.assertEqual(alpha["metrics"]["shares"]["total"], 4)
        self.assertEqual(alpha["metrics"]["views"]["total"], 150)
        self.assertEqual(alpha["interactions"]["total"], 15)
        self.assertEqual(alpha["interactions"]["average"], 15)
        self.assertEqual(alpha["interactions"]["complete_n"], 1)
        self.assertEqual(alpha["interactions"]["incomplete_n"], 1)
        self.assertEqual(alpha["interactions"]["definition"], "likes + comments + shares; views excluded")
        self.assertEqual(alpha["as_of"], "2026-10-17T11:00:00Z")
        self.assertEqual(alpha["growth"]["status"], "unavailable")
        self.assertIsNone(alpha["growth"]["value"])

        beta = seven["items"][1]["engagement_window"]
        self.assertIsNone(beta["metrics"]["likes"]["total"])
        self.assertEqual(beta["metrics"]["likes"]["known_n"], 0)
        self.assertIsNone(beta["interactions"]["total"])

        thirty = dossier_listing(self.db, window="30d", sort="interactions_average", now=NOW)
        alpha_30d = thirty["items"][0]["engagement_window"]
        self.assertEqual(alpha_30d["sample_n"], 3)
        self.assertEqual(alpha_30d["metrics"]["likes"]["total"], 30)
        self.assertEqual(alpha_30d["metrics"]["likes"]["known_n"], 2)
        self.assertEqual(alpha_30d["metrics"]["likes"]["average"], 15)
        self.assertEqual(alpha_30d["interactions"]["total"], 40)
        self.assertEqual(alpha_30d["interactions"]["complete_n"], 2)
        self.assertEqual(alpha_30d["interactions"]["average"], 20)

        custom = dossier_listing(
            self.db, window="custom", date_from="2026-10-01", date_to="2026-10-01",
            sort="shares_total", now=NOW,
        )
        self.assertEqual(custom["window"]["mode"], "custom")
        self.assertEqual(custom["items"][0]["engagement_window"]["sample_n"], 1)
        self.assertEqual(custom["items"][0]["engagement_window"]["metrics"]["shares"]["total"], 5)

    def test_article_sorts_complete_interactions_and_metric_coverage(self) -> None:
        self.seed_window_fixture()
        ranked = articles(
            self.db, sort="total_interactions", date_from="2026-10-01", date_to="2026-10-18", now=NOW,
        )
        self.assertEqual(
            [item["url"].rsplit("/", 1)[-1] for item in ranked["items"]],
            ["older-complete", "recent-complete", "recent-unknown", "recent-partial"],
        )
        self.assertEqual([item["total_interactions"] for item in ranked["items"]], [25, 15, None, None])
        self.assertEqual(ranked["engagement_coverage"]["sample_n"], 4)
        self.assertEqual(ranked["engagement_coverage"]["metrics"]["likes"], {"known_n": 2, "unknown_n": 2})
        self.assertEqual(ranked["engagement_coverage"]["metrics"]["shares"], {"known_n": 3, "unknown_n": 1})
        self.assertEqual(ranked["engagement_coverage"]["interactions_complete_n"], 2)
        self.assertEqual(ranked["engagement_coverage"]["interactions_incomplete_n"], 2)

        shares = articles(
            self.db, sort="shares", date_from="2026-10-01", date_to="2026-10-18", now=NOW,
        )
        self.assertEqual([item["engagement"]["shares"] for item in shares["items"]], [5, 3, 1, None])

    def test_private_chain_never_affects_public_profile_aggregate(self) -> None:
        self.add_article(
            "alpha", "safe", published_at="2026-10-17T08:00:00Z",
            observed_at="2026-10-17T09:00:00Z", likes=2, comments=1, shares=1, views=10,
        )
        self.add_article(
            "alpha", "private-marker", published_at="2026-10-17T08:30:00Z",
            observed_at="2026-10-17T09:30:00Z", likes=99999, comments=99999, shares=99999,
            views=99999, title="PRIVATE_ENGAGEMENT_MARKER", content_public=False,
        )
        payload = dossier_listing(self.db, window="7d", sort="interactions_total", now=NOW)
        serialized = json.dumps(payload)
        self.assertNotIn("PRIVATE_ENGAGEMENT_MARKER", serialized)
        alpha = next(item for item in payload["items"] if item["slug"] == "entity-alpha")
        self.assertEqual(alpha["engagement_window"]["sample_n"], 1)
        self.assertEqual(alpha["engagement_window"]["interactions"]["total"], 4)

    def test_validation_rejects_ambiguous_custom_or_naive_clock(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires date_from and date_to"):
            dossier_listing(self.db, window="custom", date_from="2026-10-01", now=NOW)
        with self.assertRaisesRegex(ValueError, "must not be after"):
            dossier_listing(
                self.db, window="custom", date_from="2026-10-02", date_to="2026-10-01", now=NOW,
            )
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            dossier_listing(self.db, window="7d", now=datetime(2026, 10, 18, 12, 0))

    def test_http_api_window_and_interaction_sort_contract(self) -> None:
        self.seed_window_fixture()
        server = create_server(self.db, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            query = urllib.parse.urlencode({
                "window": "custom", "date_from": "2026-10-01", "date_to": "2026-10-17",
                "sort": "interactions_total",
            })
            status, dossier_payload = self.get(server.server_port, f"/api/dossiers?{query}")
            self.assertEqual(status, 200)
            self.assertEqual(dossier_payload["window"]["mode"], "custom")
            self.assertEqual(dossier_payload["items"][0]["engagement_window"]["interactions"]["total"], 40)
            self.assertEqual(dossier_payload["items"][0]["engagement_window"]["interactions"]["complete_n"], 2)

            article_query = urllib.parse.urlencode({
                "date_from": "2026-10-01", "date_to": "2026-10-17", "sort": "total_interactions",
            })
            status, article_payload = self.get(server.server_port, f"/api/articles?{article_query}")
            self.assertEqual(status, 200)
            self.assertEqual(article_payload["items"][0]["total_interactions"], 25)

        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    @staticmethod
    def get(port: int, path: str) -> tuple[int, dict[str, object]]:
        status, text = EngagementWindowTests.get_text(port, path)
        return status, json.loads(text)

    @staticmethod
    def get_text(port: int, path: str) -> tuple[int, str]:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", path, headers={"Accept": "application/json"})
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        status = response.status
        connection.close()
        return status, body


if __name__ == "__main__":
    unittest.main()
