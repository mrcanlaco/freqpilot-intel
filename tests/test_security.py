from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bti.db import connect, init_db
from bti.public import dossiers, drafts, evidence_ledger, health, public_export


class ProjectionSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "security.sqlite3"
        init_db(self.db)
        self.chain_number = 0

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_chain(self, *, source_access: str = "public", source_distribution: str = "public_shareable",
                   entity_public: int = 1, content_public: int = 1, evidence_public: int = 1,
                   claim_public: int = 1, secret: str = "PUBLIC_TEXT", link_evidence: bool = True) -> dict[str, int]:
        now = "2026-10-18T00:00:00Z"
        self.chain_number += 1
        key = f"chain-{self.chain_number}"
        with connect(self.db) as conn:
            source = conn.execute(
                """INSERT INTO sources(slug,name,url,fetch_url,source_type,adapter,access_level,distribution,
                   capability_detail,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (f"source-{key}", f"Test source {key}", "https://www.binance.com/" + key,
                 "https://www.binance.com/" + key, "test", "html", source_access,
                 source_distribution, "PRIVATE_DIAGNOSTIC_SECRET", now, now),
            ).lastrowid
            entity = conn.execute(
                """INSERT INTO entities(slug,display_name,category,public_shareable,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (f"entity-{secret}", f"Entity {secret}", "square_creator_candidate", entity_public, now, now),
            ).lastrowid
            content = conn.execute(
                """INSERT INTO contents(source_id,entity_id,external_id,canonical_url,title,first_seen_at,published_at,
                   public_shareable,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (source, entity, f"post-{secret}", "https://www.binance.com/post/" + secret,
                 secret, now, now, content_public, now, now),
            ).lastrowid
            version = conn.execute(
                """INSERT INTO content_versions(content_id,version_no,fetched_at,checksum,normalized_text,excerpt,metadata_json)
                   VALUES(?,?,?,?,?,?,?)""", (content, 1, now, f"hash-{secret}", secret, secret, "{}")
            ).lastrowid
            conn.execute("UPDATE contents SET latest_version_id=? WHERE id=?", (version, content))
            evidence = conn.execute(
                """INSERT INTO evidence(source_id,entity_id,version_id,pillar,kind,verification_level,observed_at,
                   value_json,quote,methodology,limitations,dependency_group,public_shareable)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (source, entity, version, "engagement", "post_metrics", "platform", now,
                 json.dumps({"views": 1, "secret": secret}), secret, "method", "limit", f"dep-{secret}", evidence_public),
            ).lastrowid
            claim = conn.execute(
                """INSERT INTO claims(entity_id,claim_type,statement,confidence_label,public_shareable,created_at)
                   VALUES(?,?,?,?,?,?)""", (entity, "fact", secret, "source_text_only", claim_public, now)
            ).lastrowid
            conn.execute("INSERT INTO claim_versions(claim_id,version_id) VALUES(?,?)", (claim, version))
            if link_evidence:
                conn.execute("INSERT INTO claim_evidence(claim_id,evidence_id,dependency_group) VALUES(?,?,?)", (claim, evidence, f"dep-{secret}"))
            artifact = conn.execute(
                """INSERT INTO output_artifacts(artifact_type,title,summary,body_json,status,distribution,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                ("content", f"Draft {secret}", secret, json.dumps({
                    "secret": secret, "generator": "grounded_research_v1", "generation_active": True, "output_kind": "source_excerpt",
                    "citations": [{"content_id": content, "version_id": version, "checksum": f"hash-{secret}",
                                   "url": "https://www.binance.com/post/" + secret, "version": 1}],
                }), "draft", "public_shareable", now, now),
            ).lastrowid
            conn.execute("INSERT INTO artifact_claims(artifact_id,claim_id) VALUES(?,?)", (artifact, claim))
            conn.execute("INSERT INTO ingest_runs(started_at,finished_at,status,notes) VALUES(?,?,?,?)", (now, now, "failed", "PRIVATE_RUN_SECRET"))
            conn.commit()
        return {"source": source, "entity": entity, "content": content, "version": version, "evidence": evidence, "claim": claim, "artifact": artifact}

    def serialized_public(self) -> str:
        return json.dumps(public_export(self.db), ensure_ascii=False)

    def test_private_content_cannot_leak_through_public_evidence(self) -> None:
        self.make_chain(content_public=0, secret="PRIVATE_CONTENT_SECRET")
        payload = self.serialized_public()
        self.assertNotIn("PRIVATE_CONTENT_SECRET", payload)
        self.assertEqual(evidence_ledger(self.db), [])

    def test_revocation_hides_evidence_at_read_time(self) -> None:
        ids = self.make_chain(secret="REVOKED_EVIDENCE_SECRET")
        self.assertIn("REVOKED_EVIDENCE_SECRET", self.serialized_public())
        for table, column, row_id in (
            ("contents", "public_shareable", ids["content"]),
            ("entities", "public_shareable", ids["entity"]),
        ):
            with self.subTest(table=table):
                with connect(self.db) as conn:
                    conn.execute(f"UPDATE {table} SET {column}=0 WHERE id=?", (row_id,)); conn.commit()
                self.assertNotIn("REVOKED_EVIDENCE_SECRET", self.serialized_public())
                with connect(self.db) as conn:
                    conn.execute(f"UPDATE {table} SET {column}=1 WHERE id=?", (row_id,)); conn.commit()
        with connect(self.db) as conn:
            conn.execute("UPDATE sources SET distribution='internal_only' WHERE id=?", (ids["source"],)); conn.commit()
        self.assertNotIn("REVOKED_EVIDENCE_SECRET", self.serialized_public())

    def test_private_or_revoked_claim_hides_stale_draft(self) -> None:
        ids = self.make_chain(secret="PRIVATE_CLAIM_SECRET", claim_public=0)
        self.assertNotIn("PRIVATE_CLAIM_SECRET", json.dumps(drafts(self.db)))
        with connect(self.db) as conn:
            conn.execute("UPDATE claims SET public_shareable=1 WHERE id=?", (ids["claim"],)); conn.commit()
        self.assertIn("PRIVATE_CLAIM_SECRET", json.dumps(drafts(self.db)))
        with connect(self.db) as conn:
            conn.execute("UPDATE claims SET public_shareable=0 WHERE id=?", (ids["claim"],)); conn.commit()
        self.assertNotIn("PRIVATE_CLAIM_SECRET", json.dumps(drafts(self.db)))

    def test_mixed_public_private_artifact_dependencies_are_denied(self) -> None:
        public_ids = self.make_chain(secret="SAFE_DEPENDENCY")
        private_ids = self.make_chain(secret="MIXED_PRIVATE_SECRET", evidence_public=0)
        with connect(self.db) as conn:
            conn.execute("INSERT INTO artifact_claims(artifact_id,claim_id) VALUES(?,?)", (public_ids["artifact"], private_ids["claim"])); conn.commit()
        payload = json.dumps(drafts(self.db))
        self.assertNotIn("SAFE_DEPENDENCY", payload)
        self.assertNotIn("MIXED_PRIVATE_SECRET", payload)

    def test_mismatched_evidence_provenance_is_denied(self) -> None:
        first = self.make_chain(secret="MISMATCH_SECRET_A")
        second = self.make_chain(secret="MISMATCH_SECRET_B")
        with connect(self.db) as conn:
            conn.execute("UPDATE evidence SET source_id=? WHERE id=?", (second["source"], first["evidence"])); conn.commit()
        payload = self.serialized_public()
        self.assertNotIn("MISMATCH_SECRET_A", json.dumps(evidence_ledger(self.db)))
        self.assertNotIn("Draft MISMATCH_SECRET_A", payload)

    def test_public_health_omits_private_diagnostics_and_private_counts(self) -> None:
        self.make_chain(source_access="private", source_distribution="internal_only", entity_public=0,
                        content_public=0, evidence_public=0, claim_public=0, secret="PRIVATE_HEALTH_SECRET")
        payload = json.dumps(health(self.db))
        self.assertNotIn("PRIVATE_DIAGNOSTIC_SECRET", payload)
        self.assertNotIn("PRIVATE_RUN_SECRET", payload)
        self.assertNotIn("PRIVATE_HEALTH_SECRET", payload)
        self.assertEqual(health(self.db)["counts"]["sources"], 0)


if __name__ == "__main__":
    unittest.main()
