"""Publish reviewed campaign/research snapshots, never arbitrary runtime files."""
from __future__ import annotations

import base64
import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path

CAMPAIGN_FILES = {
    2025: ("blockchain100-2025-activity.html", "blockchain100-2025-activity.json", "blockchain100-2025-activity.csv"),
    2026: ("blockchain100-2026.html", "blockchain100-2026-dossiers.json", "blockchain100-2026-rankings.csv"),
}
RESEARCH_FILES = ("portfolio-research-20261025.html", "portfolio-research-20261025.json", "portfolio-research-20261025.csv")
METHOD_FILES = ("trader-methods-20261025.html", "trader-methods-20261025.json", "trader-methods-20261025.csv")


class _InlineHashes(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.hashes: dict[str, list[str]] = {"script": [], "style": []}
        self.tag: str | None = None
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "style" or (tag == "script" and "src" not in attributes and attributes.get("type") != "application/json"):
            self.tag = tag
            self.parts = []

    def handle_data(self, data: str) -> None:
        if self.tag:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == self.tag:
            digest = hashlib.sha256("".join(self.parts).encode("utf-8")).digest()
            self.hashes[tag].append("'sha256-" + base64.b64encode(digest).decode("ascii") + "'")
            self.tag = None
            self.parts = []


def _content_policy(html: str) -> str:
    parser = _InlineHashes()
    parser.feed(html)
    scripts = " ".join(parser.hashes["script"]) or "'none'"
    styles = " ".join(parser.hashes["style"]) or "'none'"
    return (
        "default-src 'none'; script-src " + scripts + "; style-src " + styles
        + "; connect-src 'none'; img-src 'none'; font-src 'none'; object-src 'none'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )


def load_campaign_assets(directory: Path) -> dict[str, tuple[bytes, str, str | None]]:
    """Pin each complete reviewed bundle with its own exact inline-code CSP."""
    assets: dict[str, tuple[bytes, str, str | None]] = {}
    navigation = (
        '<nav class="wrap" aria-label="Điều hướng ứng dụng">'
        '<a href="/">Về Future Intelligence</a> · '
        '<a href="/blockchain100/2026">Đề cử 2026</a> · '
        '<a href="/blockchain100/2025">Hồ sơ và hoạt động 2025</a> · '
        '<a href="/research/portfolios">Nghiên cứu portfolio</a> · '
        '<a href="/research/methods">Hồ sơ liên kết và phương pháp</a></nav>'
    )
    publications = [(f"/blockchain100/{year}", files) for year, files in CAMPAIGN_FILES.items()]
    publications.append(("/research/portfolios", RESEARCH_FILES))
    publications.append(("/research/methods", METHOD_FILES))
    for route, (html_name, json_name, csv_name) in publications:
        try:
            html = (directory / html_name).read_text(encoding="utf-8")
            json_body = (directory / json_name).read_bytes()
            csv_body = (directory / csv_name).read_bytes()
        except FileNotFoundError:
            # An incomplete publication is unavailable; never expose a partial bundle.
            continue
        if route.startswith("/blockchain100/"):
            html = html.replace('<body>', '<body>' + navigation, 1)
        html = html.replace(f'href="{json_name}"', f'href="{route}.json"')
        html = html.replace(f'href="{csv_name}"', f'href="{route}.csv"')
        html = re.sub(r'("public_dossier_url"\s*:\s*")https://[^/"]+/', r'\1/', html)
        assets[route] = (html.encode("utf-8"), "text/html; charset=utf-8", _content_policy(html))
        assets[route + ".json"] = (json_body, "application/json; charset=utf-8", None)
        assets[route + ".csv"] = (csv_body, "text/csv; charset=utf-8", None)
    return assets
