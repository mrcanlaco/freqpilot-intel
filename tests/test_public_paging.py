from __future__ import annotations

import json
import unittest
import threading

from bti.db import connect
from bti.public import articles, article_detail, dossier, dossier_listing, evidence_ledger, evidence_listing, overview
from bti.research import research_brief, store_source_claim
from bti.server import create_server
import test_engagement_windows as engagement

NOW = engagement.NOW


class PublicPagingTests(unittest.TestCase):
    # Reuse only fixture builders, not the unrelated suite's test methods.
    setUp = engagement.EngagementWindowTests.setUp
    tearDown = engagement.EngagementWindowTests.tearDown
    add_entity = engagement.EngagementWindowTests.add_entity
    add_article = engagement.EngagementWindowTests.add_article
    seed_window_fixture = engagement.EngagementWindowTests.seed_window_fixture

    def article_page(self, **kwargs):
        return articles(self.db, date_from="2026-10-01", date_to="2026-10-18", now=NOW, **kwargs)

    def test_filter_before_paging_stable_boundaries_and_full_coverage(self):
        self.seed_window_fixture()
        complete = self.article_page(sort="likes")
        first = self.article_page(sort="likes", limit=2)
        second = self.article_page(sort="likes", limit=2, offset=2)
        self.assertEqual(first["items"] + second["items"], complete["items"])
        self.assertEqual(first["pagination"], {"limit": 2, "offset": 0, "total": 4, "has_more": True, "next_offset": 2})
        self.assertEqual(second["pagination"]["next_offset"], None)
        self.assertEqual(first["sample_count"], 4)
        self.assertEqual(first["engagement_coverage"], complete["engagement_coverage"])
        self.assertEqual(second["items"][-1]["engagement"]["likes"], None)
        selected = self.article_page(entity="entity-alpha", post_ids="recent-partial,older-complete", limit=1, sort="latest")
        self.assertEqual(selected["sample_count"], 2)
        self.assertEqual(selected["items"][0]["post_id"], "recent-partial")
        self.assertEqual(self.article_page(entity="alpha")["sample_count"], 0)
        self.assertEqual(self.article_page(limit=2, offset=100)["items"], [])
        with connect(self.db) as conn:
            conn.execute("UPDATE contents SET language='vi' WHERE entity_id=?", (self.entities["beta"][1],))
            conn.commit()
        english = self.article_page(lang="en", limit=1)
        self.assertEqual(english["sample_count"], 3)
        self.assertEqual(english["vietnamese_summary"]["vi_native_count"], 0)
        self.assertEqual(english["vietnamese_summary"]["language_breakdown"], {"en": 3})

    def test_discovery_clock_quality_metadata_and_invalid_bounds(self):
        self.seed_window_fixture()
        with connect(self.db) as conn:
            conn.execute("UPDATE contents SET first_seen_at='2026-10-18T10:00:00Z' WHERE external_id='older-complete'")
            conn.execute("UPDATE content_versions SET metadata_json=? WHERE content_id=(SELECT id FROM contents WHERE external_id='older-complete')", (json.dumps({"content_volume_goal": True, "qualifying_body": True, "quality_gate_version": 2}),))
            conn.commit()
        discovered = self.article_page(sort="discovered", seen_after="2026-10-18T09:00:00+00:00", limit=1)
        self.assertEqual([item["post_id"] for item in discovered["items"]], ["older-complete"])
        self.assertEqual(discovered["items"][0]["published_at"], "2026-10-01T10:00:00Z")
        quality = self.article_page(quality_only=True)
        self.assertEqual([item["post_id"] for item in quality["items"]], ["older-complete"])
        self.assertTrue(quality["items"][0]["quality"]["audited"])
        for kwargs in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"seen_after": "2026-10-18"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.article_page(**kwargs)

    def test_evidence_filters_citations_and_public_revocation_across_pages(self):
        self.seed_window_fixture()
        page = evidence_listing(self.db, pillar="engagement", level="platform", q="entity-alpha", limit=1)
        self.assertEqual(page["sample_count"], 3)
        self.assertEqual(page["pagination"]["next_offset"], 1)
        evidence = page["items"][0]
        self.assertTrue(evidence["post_id"])
        self.assertTrue(evidence["checksum"])
        self.assertIn("canonical_url", evidence)
        self.assertEqual(len(evidence_ledger(self.db)), 4)
        with connect(self.db) as conn:
            conn.execute("UPDATE sources SET distribution='internal_only' WHERE id=?", (self.entities["alpha"][0],))
            conn.commit()
        self.assertEqual(self.article_page(limit=1)["sample_count"], 1)
        self.assertEqual(self.article_page(limit=1, offset=1)["items"], [])
        self.assertEqual(evidence_listing(self.db, q="entity-alpha", limit=1, offset=1)["sample_count"], 0)
        self.assertIsNone(article_detail(self.db, "recent-complete"))
        self.assertEqual(dossier_listing(self.db, limit=1, offset=1, now=NOW)["items"], [])

    def test_exact_verified_award_cohort_and_lightweight_dossiers(self):
        self.seed_window_fixture()
        self.add_entity("criteria", "Programme author")
        self.add_article("criteria", "criteria-post", published_at="2026-10-17T10:00:00Z", observed_at="2026-10-17T11:00:00Z", likes=None, comments=None, shares=None, views=None)
        with connect(self.db) as conn:
            for name, result, category in (("alpha", "Awardee", "Trader"), ("beta", "Awardee", "Independent Researcher"), ("criteria", "Criteria", "Trader")):
                source_id, entity_id = self.entities[name]
                version_id = conn.execute("SELECT latest_version_id FROM contents WHERE entity_id=? ORDER BY id LIMIT 1", (entity_id,)).fetchone()[0]
                evidence_id = conn.execute("""INSERT INTO evidence(source_id,entity_id,version_id,pillar,kind,verification_level,observed_at,period_label,value_json,methodology,limitations,dependency_group,public_shareable)
                    VALUES(?,?,?,'recognition','official_program','official','2026-10-18T00:00:00Z','2025',?,'official','content only',?,1)""", (source_id, entity_id, version_id, json.dumps({"program": "Blockchain 100 2025", "category_name": category, "rank": 1}), name)).lastrowid
                conn.execute("""INSERT INTO recognitions(evidence_id,program_name,category_type,category_name,result,known_at,interpretation_limit)
                    VALUES(?,'Blockchain 100 2025','content',?,?,'2026-10-18T00:00:00Z','content only')""", (evidence_id, category, result))
            source_id, entity_id = self.entities["alpha"]
            version_id = conn.execute("SELECT latest_version_id FROM contents WHERE entity_id=? ORDER BY id LIMIT 1", (entity_id,)).fetchone()[0]
            capture = conn.execute("""INSERT INTO evidence(source_id,entity_id,version_id,pillar,kind,verification_level,observed_at,period_label,value_json,methodology,limitations,dependency_group,public_shareable)
                VALUES(?,?,?,'recognition','operator_award','operator_capture','2026-10-18T11:00:00Z','2025',?,'capture','unverified','alpha',1)""",
                (source_id, entity_id, version_id, json.dumps({"program": "Blockchain 100 2025", "category_name": "Trader", "rank": 99}))).lastrowid
            conn.execute("""INSERT INTO recognitions(evidence_id,program_name,category_type,category_name,result,known_at,interpretation_limit)
                VALUES(?,'Blockchain 100 2025','content','Trader','Awardee','2026-10-18T11:00:00Z','unverified capture')""", (capture,))
            conn.commit()
        traders = dossier_listing(self.db, program="Blockchain 100", category="Trader", year="2025", limit=1, now=NOW)
        self.assertEqual(traders["sample_count"], 1)
        self.assertEqual(traders["items"][0]["slug"], "entity-alpha")
        self.assertEqual(traders["items"][0]["contents"], [])
        self.assertEqual(traders["items"][0]["award_rank"], 1)
        self.assertEqual(traders["items"][0]["pillars"]["recognition"]["verification_level"], "official")
        self.assertTrue(all(len(group["items"]) <= 1 for group in traders["items"][0]["pillars"].values()))
        detail = dossier(self.db, "entity-alpha", now=NOW)
        self.assertEqual(len(detail["contents"]), 3)
        self.assertGreater(len(detail["pillars"]["engagement"]["items"]), 1)
        criteria = dossier(self.db, "entity-criteria", now=NOW)
        self.assertNotIn("Awardee", criteria["category_label_vi"])
        summary = overview(self.db)
        self.assertEqual(summary["blockchain100_roster"]["categories"]["Trader"], 1)
        self.assertEqual(summary["blockchain100_roster"]["total_official_awardees"], 2)
        cohort = self.article_page(cohort="trader_awardees", limit=1)
        self.assertEqual(cohort["sample_count"], 3)
        self.assertEqual(cohort["items"][0]["entity_slug"], "entity-alpha")
        with connect(self.db) as conn:
            conn.execute("UPDATE evidence SET public_shareable=0 WHERE pillar='recognition' AND verification_level='official'")
            conn.commit()
        self.assertEqual(self.article_page(cohort="trader_awardees")["sample_count"], 0)

    def test_entity_content_and_evidence_revocation_are_rechecked(self):
        self.seed_window_fixture()
        for table, key, entity_slug in (
            ("entities", "slug", "entity-alpha"),
            ("contents", "external_id", "recent-complete"),
            ("evidence", "dependency_group", "post:recent-complete"),
        ):
            with self.subTest(table=table):
                with connect(self.db) as conn:
                    conn.execute(f"UPDATE {table} SET public_shareable=0 WHERE {key}=?", (entity_slug,))
                    conn.commit()
                ledger = evidence_listing(self.db, q="recent-complete", limit=1, offset=0)
                self.assertEqual(ledger["sample_count"], 0)
                self.assertEqual(evidence_listing(self.db, q="recent-complete", limit=1, offset=1)["items"], [])
                with connect(self.db) as conn:
                    conn.execute(f"UPDATE {table} SET public_shareable=1 WHERE {key}=?", (entity_slug,))
                    conn.commit()

    def add_body_claim(self, post_id, text):
        with connect(self.db) as conn:
            row = conn.execute(
                "SELECT entity_id,source_id,latest_version_id,title FROM contents WHERE external_id=?",
                (post_id,),
            ).fetchone()
            conn.execute("UPDATE content_versions SET normalized_text=? WHERE id=?",
                         (text, row["latest_version_id"]))
            claim_id = store_source_claim(
                conn, entity_id=row["entity_id"], source_id=row["source_id"],
                version_id=row["latest_version_id"], brief=research_brief(row["title"], text),
                claim_type="opinion", public=1, created_at="2026-10-18T00:00:00Z",
            )
            conn.commit()
        return claim_id

    def seed_research(self):
        self.seed_window_fixture()
        self.add_body_claim("older-complete", "If BTC breaks above 70000 then buy BTC today unless invalidated below 68000.")
        self.add_body_claim("recent-partial", "SOL liquidity deserves careful observation during the next week.\nPRIVATE_RAW_BODY_MARKER")

    def test_research_sort_basis_counts_and_search_before_pagination(self):
        self.seed_research()
        complete = self.article_page(sort="research")
        self.assertEqual([item["post_id"] for item in complete["items"]],
                         ["older-complete", "recent-partial", "recent-complete", "recent-unknown"])
        summary = {"scope": "before_basis_filter", "total": 4, "quoted": 2, "condition": 1}
        self.assertEqual(complete["research_summary"], summary)
        quoted = self.article_page(sort="research", basis="quoted", limit=1)
        self.assertEqual(quoted["sample_count"], 2)
        self.assertEqual(quoted["research_summary"], summary)
        self.assertEqual(quoted["items"] + self.article_page(sort="research", basis="quoted", limit=1, offset=1)["items"],
                         self.article_page(sort="research", basis="quoted")["items"])
        condition = self.article_page(basis="condition")
        self.assertEqual([item["post_id"] for item in condition["items"]], ["older-complete"])
        self.assertEqual(condition["research_summary"], summary)
        sol = self.article_page(asset="SOL", q="liquidity", basis="quoted", limit=1)
        self.assertEqual(sol["sample_count"], 1)
        self.assertEqual(sol["research_summary"], {"scope": "before_basis_filter", "total": 1, "quoted": 1, "condition": 0})
        self.assertEqual(self.article_page(basis="condition", asset="SOL")["sample_count"], 0)
        self.assertEqual(self.article_page()["items"][0]["post_id"], "recent-complete")
        for item in complete["items"]:
            self.assertEqual(item["research"], article_detail(self.db, item["post_id"])["research"])
            self.assertNotIn("normalized_text", item)
        unknown = next(item for item in complete["items"] if item["post_id"] == "recent-unknown")
        self.assertIsNone(unknown["engagement"]["likes"])
        zero = next(item for item in complete["items"] if item["post_id"] == "older-complete")
        self.assertEqual(zero["engagement"]["comments"], 0)
        sol_research = sol["items"][0]["research"]
        self.assertIsNone(sol_research["trigger"])
        self.assertIsNone(sol_research["invalidation"])
        self.assertFalse(sol_research["market_eligible"])
        self.assertNotIn("PRIVATE_RAW_BODY_MARKER", json.dumps(complete))

    def test_research_counts_use_canonical_dedupe_before_basis_and_page(self):
        self.seed_research()
        self.add_article("beta", "duplicate", published_at="2026-10-15T10:00:00Z",
                         observed_at="2026-10-18T11:00:00Z", likes=0, comments=0, shares=0, views=None)
        with connect(self.db) as conn:
            conn.execute("UPDATE contents SET canonical_url=(SELECT canonical_url FROM contents WHERE external_id='recent-partial') WHERE external_id='duplicate'")
            conn.commit()
        payload = self.article_page(basis="quoted", limit=1)
        self.assertEqual(payload["research_summary"], {"scope": "before_basis_filter", "total": 4, "quoted": 1, "condition": 1})
        self.assertEqual(payload["sample_count"], 1)
        self.assertEqual(payload["items"][0]["post_id"], "older-complete")
        searched = self.article_page(q="liquidity", basis="quoted")
        self.assertEqual(searched["sample_count"], 0)
        self.assertEqual(searched["research_summary"]["quoted"], 0)

    def test_body_asset_names_use_existing_ticker_mapping(self):
        self.seed_window_fixture()
        self.add_body_claim("recent-partial", "Bitcoin liquidity deserves careful observation during the next week.")
        self.add_body_claim("older-complete", "Ethereum liquidity deserves careful observation during the next week.")
        for ticker, name, post_id in (("BTC", "Bitcoin", "recent-partial"), ("ETH", "Ethereum", "older-complete")):
            with self.subTest(ticker=ticker):
                payload = self.article_page(asset=ticker, basis="quoted")
                self.assertEqual(payload["sample_count"], 1)
                self.assertEqual(payload["items"][0]["post_id"], post_id)
                self.assertEqual(payload["items"][0]["research"]["asset"], name)
                self.assertEqual(payload["research_summary"], {"scope": "before_basis_filter", "total": 1, "quoted": 1, "condition": 0})


    def test_other_source_and_evidence_dependencies_revoke_only_research(self):
        self.seed_research()
        with connect(self.db) as conn:
            claim_id = conn.execute("SELECT id FROM claims WHERE statement LIKE 'SOL%'").fetchone()[0]
            version_id = conn.execute("SELECT latest_version_id FROM contents WHERE external_id='recent-complete'").fetchone()[0]
            evidence_id = conn.execute("SELECT id FROM evidence WHERE version_id=?", (version_id,)).fetchone()[0]
            conn.execute("INSERT INTO claim_evidence(claim_id,evidence_id,dependency_group) VALUES(?,?,'extra')",
                         (claim_id, evidence_id))
            conn.commit()
        with connect(self.db) as conn:
            conn.execute("UPDATE evidence SET public_shareable=0 WHERE id=?", (evidence_id,))
            conn.commit()
        self.assertEqual(self.article_page(q="liquidity")["sample_count"], 0)
        self.assertEqual(article_detail(self.db, "recent-partial")["research"]["missing_fields"], ["public_claim"])
        with connect(self.db) as conn:
            conn.execute("UPDATE evidence SET public_shareable=1 WHERE id=?", (evidence_id,))
            conn.execute("DELETE FROM claim_evidence WHERE claim_id=? AND evidence_id=?", (claim_id, evidence_id))
            conn.execute("INSERT INTO claim_versions(claim_id,version_id) VALUES(?,?)", (claim_id, version_id))
            conn.execute("UPDATE contents SET source_id=? WHERE external_id='recent-complete'", (self.entities["beta"][0],))
            conn.commit()
        self.assertEqual(self.article_page(q="liquidity")["sample_count"], 1)
        with connect(self.db) as conn:
            conn.execute("UPDATE sources SET access_level='private' WHERE id=?", (self.entities["beta"][0],))
            conn.commit()
        self.assertEqual(self.article_page(q="liquidity")["sample_count"], 0)
        self.assertEqual(article_detail(self.db, "recent-partial")["research"]["missing_fields"], ["public_claim"])

    def test_exact_preferred_quote_legacy_exclusion_and_no_private_search_oracle(self):
        self.seed_window_fixture()
        quote = "SOL liquidity deserves careful observation during the next week."
        claim_id = self.add_body_claim("recent-partial", quote)
        with connect(self.db) as conn:
            conn.execute(
                """UPDATE claims SET confidence_label='source_text_only',
                   statement=(SELECT title FROM contents WHERE external_id='recent-partial') WHERE id=?""",
                (claim_id,),
            )
            conn.commit()
        self.assertEqual(self.article_page(basis="quoted")["sample_count"], 0)
        self.assertEqual(article_detail(self.db, "recent-partial")["claims"], [])
        preferred = "If BTC breaks above 70000 then buy BTC today unless invalidated below 68000."
        claim_id = self.add_body_claim("recent-partial", preferred + "\n" + quote + "\nPRIVATE_RAW_BODY_MARKER")
        self.add_body_claim("recent-partial", quote)
        with connect(self.db) as conn:
            conn.execute("UPDATE claims SET confidence_label='deterministic_source_excerpt_v1' WHERE statement=?", (quote,))
            conn.execute("UPDATE content_versions SET normalized_text=? WHERE content_id=(SELECT id FROM contents WHERE external_id='recent-partial')",
                         (preferred + "\n" + quote + "\nPRIVATE_RAW_BODY_MARKER",))
            conn.execute("UPDATE claims SET public_shareable=0 WHERE id=?", (claim_id,))
            conn.commit()
        self.assertEqual(self.article_page(q="70000")["sample_count"], 0)
        self.assertEqual(self.article_page(asset="BTC")["sample_count"], 0)
        self.assertEqual(self.article_page(basis="quoted")["sample_count"], 0)
        detail = article_detail(self.db, "recent-partial")
        self.assertEqual(detail["research"]["missing_fields"], ["public_claim"])
        self.assertNotIn("PRIVATE_RAW_BODY_MARKER", json.dumps(detail))
        self.assertNotIn("70000", json.dumps(self.article_page()))

    def test_research_dependency_revocations_are_immediate_in_list_and_detail(self):
        self.seed_research()
        with connect(self.db) as conn:
            claim_id = conn.execute("SELECT id FROM claims WHERE statement LIKE 'SOL%'").fetchone()[0]
            dependency = conn.execute("SELECT latest_version_id FROM contents WHERE external_id='recent-complete'").fetchone()[0]
            conn.execute("INSERT INTO claim_versions(claim_id,version_id) VALUES(?,?)", (claim_id, dependency))
            conn.commit()
        mutations = [
            ("UPDATE claims SET public_shareable=? WHERE id=?", claim_id),
            ("UPDATE contents SET public_shareable=? WHERE external_id=?", "recent-complete"),
            ("UPDATE contents SET public_shareable=? WHERE external_id=?", "recent-partial"),
            ("UPDATE entities SET public_shareable=? WHERE slug=?", "entity-alpha"),
            ("UPDATE evidence SET public_shareable=? WHERE dependency_group=?", "post:recent-partial"),
        ]
        for sql, key in mutations:
            with self.subTest(sql=sql, key=key):
                with connect(self.db) as conn:
                    conn.execute(sql, (0, key))
                    conn.commit()
                self.assertEqual(self.article_page(q="liquidity")["sample_count"], 0)
                self.assertEqual(self.article_page(asset="SOL")["sample_count"], 0)
                detail = article_detail(self.db, "recent-partial")
                if detail is not None:
                    self.assertEqual(detail["research"]["missing_fields"], ["public_claim"])
                    self.assertEqual(detail["claims"], [])
                with connect(self.db) as conn:
                    conn.execute(sql, (1, key))
                    conn.commit()
        with connect(self.db) as conn:
            conn.execute("UPDATE sources SET distribution='internal_only' WHERE id=?", (self.entities["alpha"][0],))
            conn.commit()
        self.assertEqual(self.article_page(q="liquidity")["sample_count"], 0)
        self.assertIsNone(article_detail(self.db, "recent-partial"))

    def test_research_http_filters_and_invalid_values(self):
        self.seed_research()
        server = create_server(self.db, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, payload = engagement.EngagementWindowTests.get(
                server.server_port,
                "/api/articles?sort=research&basis=quoted&cohort=all&date_from=2026-10-01&date_to=2026-10-18&limit=1",
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["sample_count"], 2)
            self.assertEqual(payload["items"][0]["post_id"], "older-complete")
            for query in ("basis=private", "cohort=Trader", "sort=profitability"):
                status, _ = engagement.EngagementWindowTests.get(server.server_port, "/api/articles?" + query)
                self.assertEqual(status, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)



if __name__ == "__main__":
    unittest.main()
