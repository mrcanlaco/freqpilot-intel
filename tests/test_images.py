from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from bti.cli import main
from bti.content_volume import parse_archive_profile
from bti.db import connect, init_db
from bti.images import enrich_images, extract_post_images, image_urls, safe_image_url
from bti.ingest import (
    FetchResult, ParsedItem, _store_item, parse_square_detail,
    parse_square_feed, parse_square_user_posts, seed_source_configs,
)
from bti.public import article_detail, articles
from bti.worker import ProcessLock, default_lock_path

NOW = "2026-10-18T00:00:00Z"
A = "https://public.bnbstatic.com/static/content/square/images/a.png"
B = "https://public.bnbstatic.com/image/cms/b.JPEG?width=100&quality=80"
C = "https://public.bnbstatic.com/image/cms/c.webp"
TEXT = (
    "Bitcoin market structure reflects liquidity conditions rather than a guaranteed outcome. "
    "Observed volume increased while funding remained neutral across several sessions. "
    "A sustained breakout requires confirmation from spot demand and broader risk appetite. "
    "Failure to retain support invalidates this scenario; position sizing should account for volatility."
)
SOURCE = {
    "slug": "synthetic-images", "name": "Synthetic image fixture",
    "url": "https://www.binance.com/en/square/profile/synthetic",
    "fetch_url": "https://www.binance.com/bapi/synthetic",
    "source_type": "binance_square_creator_posts", "adapter": "square_user_posts",
    "access_level": "public", "distribution": "public_shareable",
}


def record(post_id: str = "12345678", uid: str = "synthetic-uid") -> dict:
    return {"id": post_id, "squareUid": uid, "bodyTextOnly": TEXT,
            "body": '<p>Article body</p><img src="' + B + '">',
            "cover": A, "title": "BTC liquidity scenarios", "displayName": "Synthetic Analyst",
            "username": "synthetic", "webLink": "https://www.binance.com/en/square/post/" + post_id,
            "firstReleaseTime": 1767225600000, "contentType": 2, "language": "en"}


def capture(rows: list[dict]) -> bytes:
    return ('<script id="__APP_DATA">' + json.dumps({"posts": rows}) + '</script>').encode()


class ImageExtractionTests(unittest.TestCase):
    def test_strict_url_validation(self) -> None:
        for value in (A, B, C, "HTTPS://PUBLIC.BNBSTATIC.COM:443/image/test.GIF?q=1"):
            self.assertEqual(safe_image_url(value), value)
        for value in (
            None, 42, "", "http://public.bnbstatic.com/image/a.png",
            "https://evil.test/image/a.png", "https://public.bnbstatic.com.evil.test/image/a.png",
            "https://user@public.bnbstatic.com/image/a.png", A + "#fragment", A + "#",
            "https://public.bnbstatic.com:444/image/a.png", "https://public.bnbstatic.com:0443/image/a.png",
            "https://public.bnbstatic.com/image/a.svg", "data:image/png;base64,a",
            "javascript:alert(1)", "https://public.bnbstatic.com/avatar/a.png",
            "https://public.bnbstatic.com/image/a.png\n", A + "?x=\\bad", A + "?x=\x00",
            "https://public.bnbstatic.com/image/../avatar/a.png",
            "https://public.bnbstatic.com/image/%2e%2E/a.png",
            "https://public.bnbstatic.com/image/a%2fb.png", "https://public.bnbstatic.com/image/a%5Cb.png",
            A + "?x=" + "a" * 2048,
        ):
            with self.subTest(value=value):
                self.assertIsNone(safe_image_url(value))

    def test_cover_attachments_body_order_dedup_and_bounded(self) -> None:
        row = record()
        row.update(coverMeta={"url": A}, imageList=[A, {"url": C}], imageMetaList=[{"url": C}])
        self.assertEqual(extract_post_images(row), [A, C, B])
        self.assertEqual(image_urls([A, A, "https://evil.test/a.png", B]), [A, B])
        row["imageList"] = [f"https://public.bnbstatic.com/image/{n}.png" for n in range(20)]
        self.assertEqual(len(extract_post_images(row)), 9)

    def test_rich_body_respects_layout_and_excludes_cards_avatars_share_banners(self) -> None:
        row = {"avatar": A, "shareImage": A, "author": {"avatar": A}, "quotedPost": record()}
        row["body"] = json.dumps({
            "hash": {
                "last": {"id": "RichTextImage", "config": {"src": C}},
                "first": {"id": "RichTextImage", "config": {"src": B}},
                "card": {"id": "RichTextQuote", "config": {"content": [
                    {"id": "RichTextImage", "config": {"src": A}}]}},
                "unused": {"id": "RichTextImage", "config": {"src": A}},
            },
            "layout": {"root": ["ViewInstance0"], "ViewInstance0": ["first", "card", "last"]},
        })
        self.assertEqual(extract_post_images(row), [B, C])
        self.assertEqual(extract_post_images({"avatar": A, "shareBanner": B, "quotedPost": record()}), [])

    def test_all_square_and_archive_parsers_capture_images(self) -> None:
        row = record()
        source = dict(SOURCE, max_items=1, minimum_text_chars=200)
        for parse, data in ((parse_square_feed, {"vos": [dict(row, content=TEXT)]}),
                            (parse_square_detail, row), (parse_square_user_posts, {"contents": [row]})):
            result = FetchResult(SOURCE["fetch_url"], 200, "application/json",
                                 json.dumps({"code": "000000", "data": data}).encode(), 0, NOW)
            self.assertEqual(parse(result, source)[0].metadata["image_urls"], [A, B])
        result = FetchResult(
            "https://web.archive.org/web/20260102000000id_/https://www.binance.com/en/square/profile/synthetic",
            200, "text/html", capture([row]), 0, NOW,
        )
        items, rejects = parse_archive_profile(result, dict(source, original_url=SOURCE["url"]),
                                              current=datetime(2026, 10, 18, tzinfo=timezone.utc))
        self.assertEqual(rejects, {})
        self.assertEqual(items[0].metadata["image_urls"], [A, B])


class ImagePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "images.sqlite3"
        self.captures = self.root / "captures"
        self.captures.mkdir()
        init_db(self.db)
        seed_source_configs(self.db, [SOURCE])
        self.no_network = patch("urllib.request.urlopen", side_effect=AssertionError("No network permitted"))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)
        self.addCleanup(self.temp.cleanup)

    def store(self, metadata: dict, post_id: str = "12345678", text: str = TEXT) -> None:
        item = ParsedItem(post_id, "https://www.binance.com/en/square/post/" + post_id,
                          "BTC liquidity scenarios", text, "Synthetic Analyst", "synthetic",
                          "2026-01-01T00:00:00Z", "en", dict(source_uid="synthetic-uid", **metadata),
                          "square_creator_candidate")
        with connect(self.db) as conn:
            source = conn.execute("SELECT * FROM sources").fetchone()
            _store_item(conn, source, SOURCE, item, NOW)
            conn.commit()

    def snapshot(self) -> dict:
        with connect(self.db) as conn:
            return {table: [dict(row) for row in conn.execute("SELECT * FROM " + table)]
                    for table in ("contents", "content_versions", "sources", "entities", "evidence", "schema_meta")}

    def seed_capture(self, *, rows: list[dict] | None = None, digest: str | None = None) -> bytes:
        body = capture(rows if rows is not None else [record()])
        (self.captures / "fixture.body").write_bytes(body)
        self.store({"archive_response_sha256": digest or hashlib.sha256(body).hexdigest(),
                    "provenance_type": "archive_snapshot", "archive_observed_at": "2026-01-02T00:00:00Z"})
        return body

    def test_dry_run_index_idempotence_and_exact_provenance_preservation(self) -> None:
        self.seed_capture()
        before = self.snapshot()
        report = enrich_images(self.db, self.captures)
        self.assertEqual((report["mode"], report["eligible"], report["index_written"]), ("dry_run", 1, False))
        self.assertFalse(self.db.with_suffix(".images.json").exists())
        self.assertEqual(before, self.snapshot())
        report = enrich_images(self.db, self.captures, write_index=True)
        self.assertEqual((report["indexed"], report["index_written"]), (1, True))
        index_path = self.db.with_suffix(".images.json")
        index_bytes = index_path.read_bytes()
        modified = index_path.stat().st_mtime_ns
        self.assertEqual(self.snapshot(), before)
        report = enrich_images(self.db, self.captures, write_index=True)
        self.assertEqual((report["indexed"], report["index_written"]), (1, False))
        self.assertEqual(index_path.read_bytes(), index_bytes)
        self.assertEqual(index_path.stat().st_mtime_ns, modified)
        self.assertEqual(article_detail(self.db, "12345678")["images"], [A, B])
        self.assertEqual(articles(self.db)["items"][0]["images"], [A, B])

    def test_missing_hash_wrong_capture_and_identity_never_enrich(self) -> None:
        for case in ("missing_hash", "wrong_hash", "wrong_post", "wrong_uid", "wrong_entity_uid"):
            with self.subTest(case=case):
                body = capture([record("99999999" if case == "wrong_post" else "12345678",
                                       "wrong-uid" if case == "wrong_uid" else "synthetic-uid")])
                (self.captures / "fixture.body").write_bytes(body)
                metadata = {} if case == "missing_hash" else {
                    "archive_response_sha256": "0" * 64 if case == "wrong_hash" else hashlib.sha256(body).hexdigest()}
                self.store(metadata)
                with connect(self.db) as conn:
                    conn.execute("UPDATE content_versions SET metadata_json=?", (json.dumps(dict(source_uid="synthetic-uid", **metadata)),))
                    conn.execute("UPDATE entities SET source_uid=?", ("wrong-uid" if case == "wrong_entity_uid" else "synthetic-uid",))
                    conn.commit()
                before = self.snapshot()
                report = enrich_images(self.db, self.captures, write_index=True)
                self.assertEqual(report["indexed"], 0)
                self.assertEqual(self.snapshot(), before)

    def test_latest_version_only_and_retained_response_hash(self) -> None:
        body = self.seed_capture()
        self.store({"retained_capture_sha256": hashlib.sha256(body).hexdigest()}, text=TEXT + " New revision.")
        before = self.snapshot()
        report = enrich_images(self.db, self.captures, write_index=True)
        self.assertEqual(report["indexed"], 1)
        self.assertEqual(self.snapshot(), before)
        index = json.loads(self.db.with_suffix(".images.json").read_text())
        self.assertEqual(list(index["entries"]), [str(before["content_versions"][1]["id"])])
        self.assertEqual(article_detail(self.db, "12345678")["images"], [A, B])

    def test_public_projection_filters_unsafe_urls_and_revocations(self) -> None:
        self.seed_capture()
        enrich_images(self.db, self.captures, write_index=True)
        self.assertEqual(articles(self.db, now=datetime(2026, 10, 18, tzinfo=timezone.utc))["items"][0]["images"], [A, B])
        self.assertEqual(article_detail(self.db, "12345678")["images"], [A, B])
        for table, field, revoked, permitted in (
            ("contents", "public_shareable", 0, 1), ("entities", "public_shareable", 0, 1),
            ("sources", "access_level", "private", "public"),
            ("sources", "distribution", "internal_only", "public_shareable"),
        ):
            with self.subTest(table=table, field=field):
                with connect(self.db) as conn:
                    conn.execute(f"UPDATE {table} SET {field}=?", (revoked,))
                    conn.commit()
                self.assertIsNone(article_detail(self.db, "12345678"))
                self.assertEqual(articles(self.db)["items"], [])
                with connect(self.db) as conn:
                    conn.execute(f"UPDATE {table} SET {field}=?", (permitted,))
                    conn.commit()

    def test_index_build_rechecks_permissions(self) -> None:
        self.seed_capture()
        def revoke(row: dict) -> list[str]:
            with connect(self.db) as conn:
                conn.execute("UPDATE sources SET distribution='internal_only'")
                conn.commit()
            return extract_post_images(row)
        with patch("bti.images.extract_post_images", side_effect=revoke):
            report = enrich_images(self.db, self.captures, write_index=True)
        self.assertEqual((report["indexed"], report["changed_or_revoked"]), (0, 1))
        self.assertNotIn("image_urls", json.loads(self.snapshot()["content_versions"][0]["metadata_json"]))

    def test_image_filter_uses_latest_permitted_post_without_hiding_plain_sources_by_default(self) -> None:
        now = datetime(2026, 10, 18, tzinfo=timezone.utc)
        self.store({"image_urls": [A]})
        self.store({}, post_id="87654321", text=TEXT + " A different plain source.")
        self.assertEqual({item["post_id"] for item in articles(self.db, now=now)["items"]}, {"12345678", "87654321"})
        self.assertEqual([item["post_id"] for item in articles(self.db, now=now, has_images=True)["items"]], ["12345678"])
        self.store({"image_urls": []})
        self.assertEqual(articles(self.db, now=now, has_images=True)["items"], [])
        self.store({"image_urls": [A]})
        with connect(self.db) as conn:
            conn.execute("UPDATE contents SET public_shareable=0 WHERE external_id='12345678'")
            conn.commit()
        self.assertEqual(articles(self.db, now=now, has_images=True)["items"], [])

    def test_image_changes_and_removal_create_exact_revisions(self) -> None:
        self.store({})
        self.assertEqual(self.snapshot()["content_versions"][0]["checksum"], hashlib.sha256(TEXT.encode()).hexdigest())
        self.store({"image_urls": [A]})
        self.assertEqual(article_detail(self.db, "12345678")["images"], [A])
        self.store({"image_urls": [B]})
        self.assertEqual(article_detail(self.db, "12345678")["images"], [B])
        self.store({"image_urls": []})
        self.assertEqual(article_detail(self.db, "12345678")["images"], [])
        self.assertEqual(len(self.snapshot()["content_versions"]), 3)
        self.store({"image_urls": [B]})
        self.assertEqual(article_detail(self.db, "12345678")["images"], [B])
        self.assertEqual(len(self.snapshot()["content_versions"]), 3)

    def test_indexed_legacy_checksum_does_not_reuse_stale_images(self) -> None:
        self.seed_capture()
        enrich_images(self.db, self.captures, write_index=True)
        self.store({"image_urls": []})
        self.assertEqual(article_detail(self.db, "12345678")["images"], [])
        self.assertEqual(len(self.snapshot()["content_versions"]), 2)

    def test_unavailable_images_and_initial_permission_revocation_are_noops(self) -> None:
        self.seed_capture(rows=[dict(record(), cover=None, body="<p>No captured image</p>")])
        before = self.snapshot()
        report = enrich_images(self.db, self.captures, write_index=True)
        self.assertEqual((report["no_images"], report["indexed"]), (1, 0))
        self.assertEqual(self.snapshot(), before)
        for table, field, value in (("contents", "public_shareable", 0),
                                    ("entities", "public_shareable", 0),
                                    ("sources", "access_level", "private")):
            with connect(self.db) as conn:
                conn.execute(f"UPDATE {table} SET {field}=?", (value,))
                conn.commit()
            self.assertEqual(enrich_images(self.db, self.captures, write_index=True)["scanned"], 0)

    def test_no_image_legacy_duplicates_keep_existing_latest_selection(self) -> None:
        self.store({})
        self.store({}, text=TEXT + " Another captured text revision.")
        self.store({})
        detail = article_detail(self.db, "12345678")
        self.assertEqual(detail["version"], 2)
        self.assertEqual(detail["images"], [])

    def test_sidecar_bindings_fail_closed_at_read_time(self) -> None:
        self.seed_capture()
        enrich_images(self.db, self.captures, write_index=True)
        before = self.snapshot()
        for table, field, replacement in (
            ("contents", "external_id", "99999999"),
            ("content_versions", "checksum", "0" * 64),
            ("entities", "source_uid", "other-author"),
        ):
            with self.subTest(table=table, field=field):
                old = before[table][0][field]
                with connect(self.db) as conn:
                    conn.execute(f"UPDATE {table} SET {field}=?", (replacement,))
                    conn.commit()
                self.assertEqual(article_detail(self.db, "1")["images"], [])
                self.assertEqual(articles(self.db)["items"][0]["images"], [])
                with connect(self.db) as conn:
                    conn.execute(f"UPDATE {table} SET {field}=?", (old,))
                    conn.commit()
        original_metadata = json.loads(before["content_versions"][0]["metadata_json"])
        for field, value in (("source_uid", "other-author"), ("archive_response_sha256", "0" * 64),
                             ("image_urls", []), ("image_urls", [C])):
            with self.subTest(field=field, value=value):
                with connect(self.db) as conn:
                    conn.execute("UPDATE content_versions SET metadata_json=?",
                                 (json.dumps(dict(original_metadata, **{field: value})),))
                    conn.commit()
                self.assertEqual(article_detail(self.db, "12345678")["images"],
                                 [C] if field == "image_urls" and value else [])
        with connect(self.db) as conn:
            conn.execute("UPDATE content_versions SET metadata_json=?", (json.dumps(original_metadata),))
            conn.commit()
        self.store({}, text=TEXT + " Later captured revision without image proof.")
        self.assertEqual(article_detail(self.db, "12345678")["images"], [])

    def test_malformed_or_unsafe_sidecar_never_exposes_urls(self) -> None:
        self.seed_capture()
        enrich_images(self.db, self.captures, write_index=True)
        path = self.db.with_suffix(".images.json")
        document = json.loads(path.read_bytes())
        key = next(iter(document["entries"]))
        for field, value in (("urls", ["https://evil.test/image/a.png"]),
                             ("post_id", "99999999"), ("version_id", 999999),
                             ("checksum", "0" * 64), ("source_uid", "unrelated-uid"),
                             ("capture_sha256", "0" * 64)):
            altered = json.loads(json.dumps(document))
            altered["entries"][key][field] = value
            path.write_text(json.dumps(altered), encoding="utf-8")
            self.assertEqual(article_detail(self.db, "12345678")["images"], [])
        for raw in ('not json', '{"schema":"other","entries":{}}', '{"schema":"bti-source-images-v1","entries":[]}'):
            path.write_text(raw, encoding="utf-8")
            self.assertEqual(article_detail(self.db, "12345678")["images"], [])
        path.write_text(json.dumps(document), encoding="utf-8")
        with patch("bti.images.MAX_INDEX_BYTES", 32):
            self.assertEqual(article_detail(self.db, "12345678")["images"], [])

    def test_readonly_cli_never_takes_canonical_writer_lock(self) -> None:
        self.seed_capture()
        before = self.snapshot()
        with ProcessLock(default_lock_path(self.db)):
            with patch("bti.cli._json_print"):
                self.assertEqual(main(["--db", str(self.db), "enrich-images", "--captures", str(self.captures)]), 0)
                self.assertFalse(self.db.with_suffix(".images.json").exists())
                self.assertEqual(main(["--db", str(self.db), "enrich-images", "--captures", str(self.captures),
                                       "--write-index"]), 0)
        self.assertEqual([entry["urls"] for entry in json.loads(self.db.with_suffix(".images.json").read_text())["entries"].values()], [[A, B]])
        self.assertEqual(self.snapshot(), before)



if __name__ == "__main__":
    unittest.main()
