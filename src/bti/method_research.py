"""Join candidate-supplied public portfolio links to bounded, authored evidence.

Only exact source quotes become method cards; neither the association nor a
platform performance snapshot verifies account ownership or profit causality.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from bti.portfolio_research import build_report

REPORT_SCHEMA = "bti-linked-trader-method-research-v1"
CATALOG_PATH = "artifacts/blockchain100-2026-dossiers.json"
_LOCALE = r"(?:[a-z]{2}(?:-[A-Z]{2})?/)?"
_PORTFOLIO = re.compile(r"/" + _LOCALE + r"copy-trading/lead-details/([0-9]{19})")
_PROFILE = re.compile(r"/" + _LOCALE + r"square/profile/[A-Za-z0-9_-]+")
_POST = re.compile(r"/" + _LOCALE + r"square/post/([0-9]+)")
_CAMPAIGN = re.compile(r"/" + _LOCALE + r"square/blockchain-100-2026")
_MEMBERSHIP_KEYS = ("category", "rank", "votes", "candidate_id", "observed_at", "source_url")
_SOURCE_KEYS = ("url", "observed_at", "verification_level", "public_fields_reviewed")
_FOLLOWER_KEYS = ("field", "provider", "platform", "platform_status", "observed_at")
_OVERVIEW_KEYS = ("observed_at", "text", "tags", "original_toggle_used", "rendered_before_original_toggle")


def _pick(record: dict, keys: tuple) -> dict:
    return {key: record[key] for key in keys if key in record}


_PRIVATE_INVITE = re.compile(r"(私域[^\n]{0,40}?跟单码[：:]\s*)[A-Za-z0-9_-]+")


def _public_overview(overview: dict) -> dict:
    """Do not republish private-service invitation codes from public promotions."""
    result = _pick(overview, _OVERVIEW_KEYS)
    for field in ("text", "rendered_before_original_toggle"):
        if isinstance(result.get(field), str):
            result[field], count = _PRIVATE_INVITE.subn(r"\1[mã tham gia riêng tư đã lược]", result[field])
            if count:
                result["publication_note_vi"] = "Đã lược mã tham gia dịch vụ riêng tư; source receipt vẫn nhận diện bản chụp gốc. Trích đoạn phương pháp được đối chiếu trước khi lược."
    return result


def _digest(record: dict) -> str:
    return hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _url(value: str, pattern: re.Pattern, *, query: bool = False) -> re.Match:
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("Expected a public canonical Binance URL")
    parts = urlsplit(value)
    match = pattern.fullmatch(parts.path)
    if (parts.scheme != "https" or parts.netloc not in {"www.binance.com", "binance.com"}
            or parts.fragment or not match or (parts.query and not query)):
        raise ValueError(f"Invalid public source URL: {value!r}")
    if query and parts.query:
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        params = dict(pairs)
        if (len(params) != len(pairs) or not params.keys() <= {"timeRange", "source_type", "contentId"}
                or ("timeRange" in params and params["timeRange"] not in {"7D", "30D", "90D", "180D", "365D"})
                or ("source_type" in params and params["source_type"] != "square")
                or ("contentId" in params and not re.fullmatch(r"[0-9]+", params["contentId"]))):
            raise ValueError("Portfolio URL has non-public or unsupported query parameters")
    return match


def _identifier(value, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be an exact nonempty string")
    return value


def _public_input(value) -> None:
    """Reject credential/private payloads, even when nested outside the projection."""
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = key.lower().replace("-", "_")
            if normalized in {"cookies", "cookie", "headers", "authorization", "access_token",
                              "refresh_token", "password", "private_key", "account_state", "owner"} and item:
                raise ValueError("Private/credential data is not a public overview input")
            _public_input(item)
    elif isinstance(value, list):
        for item in value:
            _public_input(item)


def _capture_records(capture: dict, linked: set[str]) -> dict:
    if capture.get("schema") != "bti-public-portfolio-ui-capture-v1" or capture.get("mode") != "public_overviews_only":
        raise ValueError("Expected public_overviews_only portfolio capture")
    _public_input(capture)
    records = capture.get("portfolios")
    if not isinstance(records, list):
        raise ValueError("Capture portfolios must be a list")
    result = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Captured portfolio must be an object")
        identifier = record.get("portfolio_id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9]{19}", identifier):
            raise ValueError("portfolio_id must be an exact 19-digit string")
        if identifier not in linked or identifier in result:
            raise ValueError("Unlinked or duplicate captured portfolio")
        if record.get("tabs") != {} or record.get("current_positions") != {"private_notice": None, "capture_status": "not_read"}:
            raise ValueError("Only public overview captures are accepted; financial history/current positions are prohibited")
        if _url(record.get("source_url"), _PORTFOLIO, query=True)[1] != identifier:
            raise ValueError("Captured source URL and portfolio ID disagree")
        access = record.get("access", {})
        if not isinstance(access, dict):
            raise ValueError("Portfolio access evidence must be an object")
        if access.get("blocked_indicator") is True or access.get("navigation_status") in {401, 403, 429}:
            raise ValueError("Blocked or unauthorized navigation cannot supply public performance evidence")
        if access.get("url") is not None and _url(access["url"], _PORTFOLIO, query=True)[1] != identifier:
            raise ValueError("Navigation resolved to a different portfolio")
        performance = record.get("performance", {})
        if not isinstance(performance, dict) or not isinstance(performance.get("text", ""), str):
            raise ValueError("Performance text must be a string")
        overview = record.get("public_overview")
        if overview is not None:
            if (not isinstance(overview, dict) or not isinstance(overview.get("text"), str)
                    or not isinstance(overview.get("tags", []), list)
                    or any(not isinstance(tag, str) for tag in overview.get("tags", []))
                    or not isinstance(overview.get("original_toggle_used", False), bool)
                    or not isinstance(overview.get("rendered_before_original_toggle"), (str, type(None)))):
                raise ValueError("Malformed public overview")
        expected_hash = record.get("projection_sha256")
        if expected_hash is not None and expected_hash != _digest({k: v for k, v in record.items() if k != "projection_sha256"}):
            raise ValueError("Portfolio projection digest does not match evidence")
        result[identifier] = record
    selection = capture.get("selection", {})
    if not isinstance(selection, dict):
        raise ValueError("Capture selection must be an object")
    selected = selection.get("portfolio_ids")
    if selected is not None and (not isinstance(selected, list)
                                 or any(not isinstance(x, str) or x not in linked for x in selected)
                                 or len(set(selected)) != len(selected)
                                 or not set(result).issubset(selected)):
        raise ValueError("Capture selection must contain unique candidate-linked portfolio IDs")
    return result


def _posts(reviews: dict) -> dict:
    posts = {}
    if not isinstance(reviews.get("public_posts", []), list):
        raise ValueError("Public posts must be a list")
    for post in reviews.get("public_posts", []):
        if not isinstance(post, dict):
            raise ValueError("Public post must be an object")
        identifier = _identifier(post.get("post_id"), "post_id")
        if identifier in posts:
            raise ValueError("Duplicate public post ID")
        if (_url(post.get("source_url"), _POST)[1] != identifier
                or post.get("access_label") != "public"
                or post.get("author_identity_basis") not in {"exact_author_profile_link", "exact_author_square_uid"}
                or not isinstance(post.get("text"), str)):
            raise ValueError("Post must carry public, exact author/source evidence")
        _url(post.get("author_profile_url"), _PROFILE)
        if post["author_identity_basis"] == "exact_author_square_uid":
            _identifier(post.get("author_square_uid"), "author_square_uid")
            _url(post.get("expected_profile_url"), _PROFILE)
            if post.get("dom_post_id_verified") is not True:
                raise ValueError("Public author UID requires an exact rendered post ID match")
        if not isinstance(post.get("published_at"), (str, type(None))):
            raise ValueError("Post publication time must be a string or null")
        posts[identifier] = post
    return posts


def _post_matches(candidate: dict, post: dict) -> bool:
    if post["author_identity_basis"] == "exact_author_square_uid":
        return (candidate.get("square_uid") is not None
                and post.get("author_square_uid") == candidate["square_uid"]
                and post.get("expected_profile_url") == candidate.get("square_profile_url"))
    return post.get("author_profile_url") == candidate.get("square_profile_url")


def _cards(candidate: dict, record: dict | None, reviews: list, posts: dict) -> list:
    cards = []
    for review in reviews:
        kind, source_url = review.get("source_kind"), review.get("source_url")
        published_at = None
        attribution = None
        if kind == "campaign_biography":
            if source_url != candidate["source"]["url"] or review.get("post_id") is not None:
                raise ValueError("Campaign review must cite this candidate's source")
            text, observed_at = candidate.get("biography") or "", candidate["source"].get("observed_at")
        elif kind == "portfolio_biography":
            if (record is None or not record.get("public_overview") or review.get("post_id") is not None
                    or source_url not in {record["source_url"], record.get("access", {}).get("url"), candidate["source_link_value"]}
                    or _url(source_url, _PORTFOLIO, query=True)[1] != record["portfolio_id"]):
                raise ValueError("Portfolio review lacks matching captured overview")
            text = record["public_overview"]["text"]
            observed_at = record["public_overview"].get("observed_at")
        elif kind == "square_post":
            post = posts.get(review.get("post_id"))
            if (post is None or post["source_url"] != source_url
                    or not _post_matches(candidate, post)):
                raise ValueError("Post review lacks exact candidate author association")
            text, observed_at, published_at = post["text"], post.get("observed_at"), post.get("published_at")
            attribution = _pick(post, ("author_identity_basis", "author_profile_url", "author_square_uid",
                                      "expected_profile_url", "dom_post_id_verified", "text_basis"))
        else:
            raise ValueError("Unknown review source kind")
        quote = review.get("quote")
        if not isinstance(quote, str) or not quote.strip() or quote not in text or _PRIVATE_INVITE.search(quote):
            raise ValueError("Method quote is not an exact nonempty source substring")
        rules = review.get("rules")
        if not isinstance(rules, list):
            raise ValueError("Method rules must be a list")
        clean_rules = []
        for rule in rules:
            if (not isinstance(rule, dict)
                    or rule.get("kind") not in {"entry", "exit", "sizing", "risk", "market_context"}
                    or not isinstance(rule.get("quote"), str) or not rule["quote"].strip()
                    or rule["quote"] not in text or _PRIVATE_INVITE.search(rule["quote"]) or not isinstance(rule.get("interpretation_vi"), str)
                    or not rule["interpretation_vi"].strip()):
                raise ValueError("Rule needs an allowed kind, exact source quote and authored interpretation")
            clean_rules.append(_pick(rule, ("kind", "quote", "interpretation_vi")))
        if (not isinstance(review.get("interpretation_vi"), str) or not review["interpretation_vi"].strip()
                or not isinstance(review.get("limitations_vi"), list)
                or any(not isinstance(x, str) for x in review["limitations_vi"])):
            raise ValueError("Malformed authored method interpretation/limitations")
        cards.append({"source_kind": kind, "source_url": source_url, "post_id": review.get("post_id"),
                      "quote": quote, "interpretation_vi": review["interpretation_vi"], "rules": clean_rules,
                      "limitations_vi": list(review["limitations_vi"]), "observed_at": observed_at,
                      "published_at": published_at, "source_attribution": attribution, "profit_cause_verified": False})
    return cards


def build_method_report(catalog: dict, capture: dict, reviews: dict) -> dict:
    """Build catalog-ordered evidence; absent captures never remove linked candidates."""
    if catalog.get("schema") != "bti-blockchain100-2026-dossiers-v1":
        raise ValueError("Expected Blockchain100 2026 dossier catalog")
    if reviews.get("schema") != "bti-authored-method-review-v1":
        raise ValueError("Expected authored method reviews")
    _public_input(reviews)
    if not isinstance(catalog.get("profiles"), list) or not isinstance(reviews.get("reviews", []), list):
        raise ValueError("Catalog profiles and authored reviews must be lists")
    candidates, dependencies, identifiers = [], {}, set()
    for candidate in catalog.get("profiles", []):
        if not isinstance(candidate, dict):
            raise ValueError("Catalog candidate must be an object")
        link = candidate.get("source_link_value")
        if not isinstance(link, str) or "copy-trading/lead-details" not in link:
            continue
        portfolio_id = _url(link, _PORTFOLIO, query=True)[1]
        identifier = _identifier(candidate.get("id"), "candidate_id")
        if identifier in identifiers:
            raise ValueError("Duplicate catalog candidate ID")
        identifiers.add(identifier)
        if candidate.get("square_uid") is not None:
            _identifier(candidate["square_uid"], "square_uid")
        if candidate.get("square_profile_url") is not None:
            _url(candidate["square_profile_url"], _PROFILE)
        _url(candidate.get("source", {}).get("url"), _CAMPAIGN)
        for membership in candidate.get("memberships", []):
            _identifier(membership.get("candidate_id"), "membership candidate_id")
            _url(membership.get("source_url"), _CAMPAIGN)
        candidates.append((candidate, portfolio_id))
        dependencies.setdefault(portfolio_id, []).append(identifier)
    records = _capture_records(capture, set(dependencies))
    catalog_hash = _digest(catalog)
    if capture.get("selection", {}).get("catalog_sha256") not in {None, catalog_hash}:
        # CLI verifies the byte-level file receipt before calling this pure API.
        raise ValueError("Capture catalog digest does not match canonical catalog")
    posts = _posts(reviews)
    grouped_reviews = {identifier: [] for identifier in identifiers}
    for review in reviews.get("reviews", []):
        if not isinstance(review, dict):
            raise ValueError("Authored review must be an object")
        if review.get("candidate_id") not in grouped_reviews:
            raise ValueError("Review references an unselected candidate")
        grouped_reviews[review["candidate_id"]].append(review)
    projections = {p["portfolio_id"]: p for p in build_report(capture)["profiles"]}
    profiles = []
    for candidate, portfolio_id in candidates:
        identifier, source = candidate["id"], candidate["source"]
        record, projection = records.get(portfolio_id), projections.get(portfolio_id)
        memberships = [_pick(m, _MEMBERSHIP_KEYS) for m in candidate.get("memberships", [])]
        observed = ((record.get("performance", {}).get("observed_at") or record.get("access", {}).get("observed_at")) if record else None)
        snapshot = projection["snapshot"] if projection and record.get("performance", {}).get("text", "").strip() else None
        post_samples = []
        for post in posts.values():
            if _post_matches(candidate, post):
                sample = _pick(post, ("post_id", "source_url", "published_at", "observed_at",
                                      "author_identity_basis", "author_profile_url", "author_square_uid"))
                sample["engagement"] = _pick(post.get("engagement", {}), ("likes", "comments", "views", "shares"))
                post_samples.append(sample)
        receipt = None
        if record:
            stop = capture.get("stop")
            receipt = {"source_url": record["source_url"], "observed_at": observed,
                       "performance_text": record.get("performance", {}).get("text", ""),
                       "projection_sha256": record.get("projection_sha256"),
                       "capture_script_sha256": capture.get("capture_script_sha256"),
                       "capture_method": capture.get("capture_method"),
                       "collector_stop": _pick(stop, ("reason", "observed_at")) if isinstance(stop, dict) else None}
        profiles.append({"candidate_id": identifier, "display_name": candidate.get("display_name"),
                         "square_uid": candidate.get("square_uid"), "square_profile_url": candidate.get("square_profile_url"),
                         "portfolio_id": portfolio_id, "source_link_url": candidate["source_link_value"],
                         "portfolio_url": record.get("access", {}).get("url", record["source_url"]) if record else f"https://www.binance.com/en/copy-trading/lead-details/{portfolio_id}",
                         "link_evidence": {"basis": "candidate_supplied_link_on_official_campaign", "source_url": source["url"],
                                           "observed_at": source.get("observed_at"), "ownership_verified": False,
                                           "shared_portfolio_candidates": list(dependencies[portfolio_id])},
                         "memberships": memberships, "biography": candidate.get("biography"),
                         "snapshot": snapshot, "observed_at": observed,
                         "public_overview": _public_overview(record["public_overview"]) if record and record.get("public_overview") is not None else None,
                         "source_receipts": {"campaign": _pick(source, _SOURCE_KEYS), "portfolio": receipt},
                         "three_pillars": {
                             "engagement": {"status": "public_post_samples" if post_samples else "follower_snapshot_only" if candidate.get("followers") is not None else "unknown",
                                            "followers": candidate.get("followers"), "followers_source": _pick(candidate.get("followers_source", {}), _FOLLOWER_KEYS),
                                            "observed_at": candidate.get("followers_source", {}).get("observed_at"),
                                            "post_samples": post_samples,
                                            "coverage_note_vi": "Tối đa tám thẻ hiển thị ban đầu mỗi hồ sơ được chọn; không phải lịch sử đầy đủ, không đo người tương tác duy nhất hoặc chất lượng bình luận."},
                             "performance": {"status": "platform_displayed_not_independently_audited" if snapshot else "unavailable", "verification_level": "operator_capture"},
                             "recognition": {"status": "campaign_nomination_not_award", "source_url": source["url"], "memberships": memberships}},
                         "method_cards": _cards(candidate, record, grouped_reviews[identifier], posts),
                         "limitations_vi": ["Liên kết do ứng viên cung cấp; chưa xác minh cùng người hoặc quyền sở hữu tài khoản.",
                                            "Chưa biết hệ thống vào/thoát lệnh, định cỡ và quản trị rủi ro đầy đủ; trích dẫn không chứng minh nguyên nhân lợi nhuận."]})
    return {"schema": REPORT_SCHEMA, "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_catalog": {"path": CATALOG_PATH, "sha256": catalog_hash,
                               "observation_window": _pick(catalog.get("observation_window") or catalog.get("collection_window") or {},
                                                           ("first_observed_at", "last_observed_at", "started_at", "completed_at"))},
            "capture_window": {"started_at": capture.get("started_at"), "completed_at": capture.get("completed_at")},
            "selection_note_vi": "Chỉ liên kết Copy Trading trực tiếp do ứng viên cung cấp trên trang chiến dịch; giữ thứ tự danh mục, không xếp hạng lợi nhuận.",
            "counts": {"candidate_profiles": len(profiles), "distinct_portfolios": len(dependencies), "observed_portfolios": len(records),
                       "method_cards": sum(len(p["method_cards"]) for p in profiles)},
            "profiles": profiles,
            "limitations_vi": ["Số liệu nền tảng hiển thị, không kiểm toán độc lập; kỳ nguồn có thể khác nhau, không trừ ROI cửa sổ trượt.",
                               "Nhãn 365 Days là bộ lọc hiển thị, không chứng minh có đủ 365 ngày lịch sử; thời gian hiệu lực và phương pháp ROI chưa được đối soát.",
                               "Đề cử và phiếu bầu không phải giải thưởng hay kỹ năng giao dịch đã xác minh; người theo dõi không phải tương tác bài viết.",
                               "Văn bản bài là phiên bản quan sát lúc thu, không chứng minh cùng phiên bản đã tồn tại từ ngày đăng; chưa có lịch sử chỉnh sửa để backtest point-in-time.",
                               "Bản chụp công khai cập nhật thủ công; thiếu hồ sơ hoặc bài viết không chứng minh không có phương pháp."]}


def method_csv(report: dict) -> str:
    """One row per linked candidate, with source units and exact identifier strings."""
    fields = ["candidate_id", "display_name", "square_uid", "square_profile_url", "portfolio_id", "source_link_url", "portfolio_url",
              "period_label", "roi_percent", "mdd_percent", "leader_pnl", "leader_pnl_currency", "copier_pnl", "copier_pnl_currency",
              "observed_at", "method_count", "ownership_verified"]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for profile in report["profiles"]:
        row = {key: profile.get(key) for key in fields[:7] + ["observed_at"]}
        row.update({key: (profile.get("snapshot") or {}).get(key) for key in fields[7:14]})
        row.update(method_count=len(profile["method_cards"]), ownership_verified="false")
        writer.writerow(row)
    return output.getvalue()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("catalog", "capture", "reviews", "output", "csv-output"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args(argv)
    catalog_bytes = args.catalog.read_bytes()
    catalog = json.loads(catalog_bytes)
    capture = json.loads(args.capture.read_text(encoding="utf-8"))
    reviews = json.loads(args.reviews.read_text(encoding="utf-8"))
    byte_hash = hashlib.sha256(catalog_bytes).hexdigest()
    declared_hash = capture.get("selection", {}).get("catalog_sha256")
    if declared_hash is not None and declared_hash not in {byte_hash, _digest(catalog)}:
        raise ValueError("Capture does not depend on this catalog file")
    if declared_hash is not None:
        capture = {**capture, "selection": {**capture["selection"], "catalog_sha256": _digest(catalog)}}
    report = build_method_report(catalog, capture, reviews)
    report["source_catalog"].update(path=args.catalog.as_posix(), sha256=byte_hash)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.csv_output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with args.csv_output.open("w", encoding="utf-8", newline="") as handle:
        handle.write(method_csv(report))


if __name__ == "__main__":
    main()
