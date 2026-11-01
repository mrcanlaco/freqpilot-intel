"""Source-faithful analysis of operator-captured public portfolio UI evidence.

History rows are provider-aggregated observations, not fills or a reconstruction
of portfolio equity. CLI: python -m bti.portfolio_research --input CAPTURE
--output REPORT --csv-output SUMMARY (all files are UTF-8).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from statistics import median

CAPTURE_SCHEMA = "bti-public-portfolio-ui-capture-v1"
REPORT_SCHEMA = "bti-portfolio-profit-research-v1"
_NUMBER = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_UNKNOWN = {"--", "-", "N/A", ""}


def _decimal(value: str) -> Decimal | None:
    if value in _UNKNOWN:
        return None
    if not re.fullmatch(_NUMBER, value):
        raise ValueError(f"Invalid displayed decimal: {value!r}")
    return Decimal(value.replace(",", ""))


def _string(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _money(value: str, *, currency_required: bool = False) -> tuple[str | None, str | None]:
    if value in _UNKNOWN:
        return None, None
    match = re.fullmatch(rf"({_NUMBER})(?: ([A-Za-z0-9]+))?", value)
    if not match or (currency_required and not match[2]):
        raise ValueError(f"Invalid displayed amount/currency: {value!r}")
    return _string(_decimal(match[1])), match[2]


def _date(value: str) -> datetime | None:
    if value in _UNKNOWN:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise ValueError(f"Invalid displayed wall-clock timestamp: {value!r}") from exc


def _performance(text: str) -> dict:
    result = dict.fromkeys(("period_label", "roi_percent", "leader_pnl", "leader_pnl_currency",
                          "copier_pnl", "copier_pnl_currency", "mdd_percent", "win_rate_percent",
                          "sharpe_ratio", "win_positions", "total_positions"))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return result
    # The UI places the two chart labels before the two corresponding values.
    if lines[0] != "Performance" or len(lines) < 2:
        raise ValueError("Unrecognized performance projection: expected Performance and period label")
    result["period_label"] = lines[1]
    index = 2
    while index < len(lines):
        label = lines[index]
        if label == "ROI" and lines[index:index + 2] == ["ROI", "PnL"]:
            if index + 3 >= len(lines):
                raise ValueError("Incomplete ROI/PnL display")
            value = lines[index + 2]
            if value not in _UNKNOWN and not value.endswith("%"):
                raise ValueError("ROI display lacks percent sign")
            result["roi_percent"] = _string(_decimal(value.removesuffix("%")))
            result["leader_pnl"], result["leader_pnl_currency"] = _money(lines[index + 3])
            index += 4
            continue
        fields = {"Copier PnL": "copier_pnl", "Sharpe Ratio": "sharpe_ratio", "MDD": "mdd_percent",
                  "Win Rate": "win_rate_percent", "Win Positions": "win_positions", "Total Positions": "total_positions"}
        if label not in fields or index + 1 >= len(lines):
            raise ValueError(f"Unrecognized/incomplete performance field: {label!r}")
        field, value = fields[label], lines[index + 1]
        if field == "copier_pnl":
            result[field], result["copier_pnl_currency"] = _money(value)
        elif field.endswith("percent"):
            if value not in _UNKNOWN and not value.endswith("%"):
                raise ValueError(f"{label} display lacks percent sign")
            result[field] = _string(_decimal(value.removesuffix("%")))
        elif field.endswith("positions"):
            number = _decimal(value)
            if number is not None and (number < 0 or number != number.to_integral_value()):
                raise ValueError(f"Invalid position count: {value!r}")
            result[field] = None if number is None else int(number)
        else:
            result[field] = _string(_decimal(value))
        index += 2
    return result


def _position(row: str) -> dict:
    if not isinstance(row, str):
        raise ValueError("Position History rows must be displayed text strings")
    lines = [line.strip() for line in row.splitlines() if line.strip()]
    labels = {6: "Opened", 8: "Entry Price", 10: "Max. Open Interest", 12: "Closing PNL",
              14: "Closed", 16: "Avg. Close Price", 18: "Closed Vol."}
    if len(lines) != 20 or any(lines[i] != label for i, label in labels.items()):
        raise ValueError("Unrecognized Position History row layout")
    if (lines[1] != "Perp" or not re.fullmatch(r"[1-9]\d*x", lines[2])
            or lines[3] not in {"Cross", "Isolated"} or lines[4] not in {"Long", "Short"}
            or lines[5] not in {"Closed", "Partially Closed"}):
        raise ValueError("Unrecognized position instrument/leverage/margin/side/status")
    opened, closed = _date(lines[7]), _date(lines[15])
    if opened is not None and closed is not None and closed < opened:
        raise ValueError("Position close wall-clock time precedes open time")
    result = {"symbol": lines[0], "side": lines[4], "leverage": int(lines[2][:-1]),
              "margin_mode": lines[3], "position_status": lines[5],
              "opened_at_display": lines[7], "closed_at_display": lines[15], "timezone": None,
              "holding_hours_wall_clock": None,
              "record_key": hashlib.sha256(row.encode("utf-8")).hexdigest()}
    if opened is not None and closed is not None:
        seconds = (closed - opened).days * 86400 + (closed - opened).seconds
        result["holding_hours_wall_clock"] = _string(Decimal(seconds) / Decimal(3600))
    for field, unit, index in (("entry_price", "entry_price_currency", 9),
                               ("max_open_interest", "max_open_interest_asset", 11),
                               ("closing_pnl", "closing_pnl_currency", 13),
                               ("close_price", "close_price_currency", 17),
                               ("closed_volume", "closed_volume_asset", 19)):
        result[field], result[unit] = _money(lines[index], currency_required=True)
    return result


def _transfer(row: list, headers: list) -> dict:
    if headers != ["Time", "Coin", "Amount", "From", "To"]:
        raise ValueError("Unrecognized Transfer History headers")
    if not isinstance(row, list) or len(row) != 5 or not all(isinstance(v, str) for v in row):
        raise ValueError("Transfer History rows must contain five displayed strings")
    time, asset, amount, origin, destination = row
    _date(time)
    value = _decimal(amount)
    if not asset or (value is not None and value < 0):
        raise ValueError("Invalid transfer asset or unsigned amount")
    direction = "unknown"
    if origin == "Lead Trading" and destination != "Lead Trading":
        direction = "out"
    elif destination == "Lead Trading" and origin != "Lead Trading":
        direction = "in"
    return {"time_display": time, "timezone": None, "asset": asset, "amount": _string(value),
            "from_wallet": origin, "to_wallet": destination, "direction": direction}


def _history(tab: dict, kind: str) -> tuple[list, dict]:
    records, fingerprints = [], set()
    metadata = {"pages": len(tab.get("pages", [])), "duplicate_pages": 0,
                "loading": False, "private": False, "row_count_mismatches": [],
                "ui_pagination_exhausted": tab.get("ui_pagination_exhausted")}
    for page in tab.get("pages", []):
        rows = page.get("rows", [])
        if not isinstance(rows, list):
            raise ValueError(f"{kind}: rows must be a list")
        metadata["loading"] |= page.get("loading") is True
        metadata["private"] |= page.get("private_notice") is True
        if page.get("private_notice") is True:
            if rows:
                raise ValueError(f"{kind}: private history rows cannot be published")
            continue
        if page.get("visible_row_count") is not None and page["visible_row_count"] != len(rows):
            metadata["row_count_mismatches"].append({"page_index": page.get("page_index"),
                "visible_row_count": page["visible_row_count"], "rows_collected": len(rows)})
        fingerprint = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        if rows and fingerprint in fingerprints:
            metadata["duplicate_pages"] += 1
            continue
        fingerprints.add(fingerprint)
        for index, row in enumerate(rows, 1):
            try:
                record = _position(row) if kind == "Position History" else _transfer(row, page.get("headers", []))
            except ValueError as exc:
                raise ValueError(f"{kind} page {page.get('page_index')} row {index}: {exc}") from exc
            record.update(source_page_index=page.get("page_index"), source_row_index=index,
                          observed_at=page.get("observed_at"))
            records.append(record)
    return records, metadata


def _closed_summary(rows: list) -> dict:
    currencies, symbols, sides = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in rows:
        if row["closing_pnl"] is not None:
            value = Decimal(row["closing_pnl"])
            currencies[row["closing_pnl_currency"]].append(value)
            symbols[(row["symbol"], row["closing_pnl_currency"])].append(value)
            sides[(row["side"], row["closing_pnl_currency"])].append(value)
    summaries = []
    for currency, values in sorted(currencies.items()):
        positives = sorted((v for v in values if v > 0), reverse=True)
        profit = sum(positives, Decimal(0))
        loss = -sum((v for v in values if v < 0), Decimal(0))
        summaries.append({"currency": currency, "net_pnl": _string(sum(values, Decimal(0))),
            "gross_profit": _string(profit), "gross_loss": _string(loss),
            "wins": len(positives), "losses": sum(v < 0 for v in values), "flat": values.count(Decimal(0)),
            "win_rate_percent": _string(Decimal(len(positives)) * 100 / len(values)),
            "profit_factor": _string(profit / loss if loss else None),
            "top3_positive_pnl_share_percent": _string(sum(positives[:3], Decimal(0)) * 100 / profit if profit else None)})
    durations = [Decimal(r["holding_hours_wall_clock"]) for r in rows if r["holding_hours_wall_clock"] is not None]
    leverages = [r["leverage"] for r in rows]
    def grouped(groups: dict, label: str) -> list:
        return [{label: key[0], "currency": key[1], "count": len(values),
                 "net_pnl": _string(sum(values, Decimal(0)))} for key, values in sorted(groups.items())]
    # Different currencies have no comparable ordering without a conversion rate.
    rankable = rows if len(currencies) == 1 else []
    return {"count": len(rows), "currencies": summaries, "by_symbol": grouped(symbols, "symbol"),
            "by_side": grouped(sides, "side"),
            "holding_median_hours_wall_clock": _string(median(durations) if durations else None),
            "holding_duration_observed_count": len(durations),
            "unknown_pnl_count": sum(r["closing_pnl"] is None for r in rows),
            "partially_closed_count": sum(r["position_status"] == "Partially Closed" for r in rows),
            "leverage_min": min(leverages, default=None), "leverage_max": max(leverages, default=None),
            "top_winners": sorted((r for r in rankable if r["closing_pnl"] is not None and Decimal(r["closing_pnl"]) > 0), key=lambda r: Decimal(r["closing_pnl"]), reverse=True)[:3],
            "top_losers": sorted((r for r in rankable if r["closing_pnl"] is not None and Decimal(r["closing_pnl"]) < 0), key=lambda r: Decimal(r["closing_pnl"]))[:3]}


def _transfer_summary(rows: list) -> dict:
    assets = defaultdict(list)
    for row in rows:
        assets[row["asset"]].append(row)
    result = []
    for asset, items in sorted(assets.items()):
        totals = {}
        for direction, field in (("in", "inflow"), ("out", "outflow")):
            matching = [r for r in items if r["direction"] == direction]
            totals[field] = None if any(r["amount"] is None for r in matching) else sum((Decimal(r["amount"]) for r in matching), Decimal(0))
        unknown = sum(r["direction"] == "unknown" for r in items)
        net = None if unknown or any(v is None for v in totals.values()) else totals["inflow"] - totals["outflow"]
        result.append({"asset": asset, **{k: _string(v) for k, v in totals.items()},
                       "net_flow": _string(net), "unknown_direction_count": unknown})
    return {"count": len(rows), "assets": result}


def _profile(portfolio: dict, capture: dict) -> dict:
    identifier = portfolio.get("portfolio_id")
    if not isinstance(identifier, str) or not re.fullmatch(r"\d+", identifier):
        raise ValueError("portfolio_id must be an exact decimal string")
    access, performance = portfolio.get("access", {}), portfolio.get("performance", {})
    tabs = portfolio.get("tabs", {})
    history, cm = _history(tabs.get("Position History", {}), "Position History")
    closed = [row for row in history if row["position_status"] == "Closed"]
    partial = [row for row in history if row["position_status"] == "Partially Closed"]
    transfers, tm = _history(tabs.get("Transfer History", {}), "Transfer History")
    current = portfolio.get("current_positions", {})
    summary = _closed_summary(closed)
    partial_summary = _closed_summary(partial)
    limitations = [
        "Số liệu do nền tảng hiển thị, chưa được kiểm toán hay xác minh độc lập.",
        "Lịch sử quan sát là dữ liệu tổng hợp của nhà cung cấp; hết trang UI không chứng minh toàn bộ lịch sử nguồn.",
        "Mẫu Position History không cùng cửa sổ với chỉ số 30D; không dùng mẫu để quy kết ROI/PnL 30 ngày.",
        "Không biết múi giờ nguồn; thời gian nắm giữ chỉ là chênh lệch đồng hồ hiển thị, không phải thời lượng UTC đã xác minh.",
        "Chưa có dữ liệu đầy đủ về phí, funding, từng lần khớp lệnh và đường vốn/equity; không tái tính PnL từ giá trung bình.",
        "Chuyển tiền là dòng vốn theo từng tài sản, không phải lợi nhuận hoặc tài sản toàn tài khoản.",
        "Không có bằng chứng quy tắc vào lệnh, kỹ năng bền vững hoặc liên kết danh tính Square."]
    private = current.get("private_notice")
    if private is True:
        limitations.append("Vị thế hiện tại bị ẩn; không cố truy cập bằng quyền sao chép/giao dịch.")
    else:
        limitations.append("Vị thế hiện tại chỉ phản ánh số dòng công khai đã quan sát, không chứng minh danh mục đầy đủ.")
    if partial:
        limitations.append("Vị thế đóng một phần được tách riêng; Closing PNL vẫn được giữ, ngày đóng '--' không được suy diễn và không có thời lượng nắm giữ.")
    if summary["unknown_pnl_count"]:
        limitations.append("Một số Closing PNL không hiển thị; tổng tiền chỉ gồm giá trị có đơn vị tiền tệ rõ ràng.")
    if len(summary["currencies"]) > 1:
        limitations.append("Không xếp hạng top winner/loser xuyên tiền tệ; không có tỷ giá quy đổi.")
    if capture.get("stop") is not None:
        limitations.append("Bộ thu thập đã dừng trước khi hoàn tất; giữ dữ liệu hợp lệ nhưng không tuyên bố bao phủ đầy đủ.")
    if cm["row_count_mismatches"] or tm["row_count_mismatches"]:
        limitations.append("Bộ đếm dòng UI khác số dòng thu thập; chỉ phân tích các dòng thực sự có trong bản chụp.")
    if cm["loading"] or tm["loading"]:
        limitations.append("Có trang đang tải tại thời điểm chụp; phạm vi quan sát có thể thiếu.")
    findings = [f"Quan sát {len(history)} dòng Position History: {len(closed)} đóng hoàn toàn và {len(partial)} đóng một phần; cùng {len(transfers)} dòng chuyển tiền. Không đồng nhất với số giao dịch 30D."]
    for item in partial_summary["currencies"]:
        findings.append(f"Mẫu đóng một phần: tổng Closing PNL {item['net_pnl']} {item['currency']}; không cộng vào thống kê vị thế đóng hoàn toàn hoặc PnL 30D.")
    for item in summary["currencies"]:
        currency = item["currency"]
        findings.append(f"Trong mẫu {currency}: tổng Closing PNL {item['net_pnl']}; {item['wins']} dòng lãi, {item['losses']} dòng lỗ, {item['flat']} dòng hòa vốn.")
        if item["top3_positive_pnl_share_percent"] is not None:
            findings.append(f"Ba dòng lãi lớn nhất chiếm {item['top3_positive_pnl_share_percent']}% tổng Closing PNL dương ({currency}); đây là mức tập trung của mẫu, không phải nguyên nhân ROI toàn danh mục.")
        for group, label, key in ((summary["by_symbol"], "mã", "symbol"), (summary["by_side"], "hướng", "side")):
            relevant = [g for g in group if g["currency"] == currency]
            largest = max(relevant, key=lambda g: abs(Decimal(g["net_pnl"])))
            findings.append(f"Theo {label}, {largest[key]} có đóng góp ròng tuyệt đối lớn nhất trong mẫu: {largest['net_pnl']} {currency}, {largest['count']} dòng.")
    receipts = {"source_url": portfolio.get("source_url"), "observed_at": performance.get("observed_at"),
                "performance_text": performance.get("text", ""), "projection_sha256": portfolio.get("projection_sha256"),
                "capture_script_sha256": capture.get("capture_script_sha256"),
                "capture_method": capture.get("capture_method"), "collector_stop": capture.get("stop")}
    for key in ("previous_portfolio_name", "previous_name_observed_at"):
        if key in access:
            receipts[key] = access[key]
    result = {"portfolio_id": identifier, "slug": "copy-portfolio-" + identifier,
            "display_name": access.get("portfolio_name", access.get("name")),
            "source_url": portfolio.get("source_url"), "observed_at": performance.get("observed_at", access.get("observed_at")),
            "verification_level": "operator_capture", "identity_note_vi": "Chỉ xác định portfolio theo ID; chưa có liên kết danh tính Square.",
            "snapshot": _performance(performance.get("text", "")), "closed_positions": closed,
            "closed_summary": summary, "transfers": transfers, "transfer_summary": _transfer_summary(transfers),
            "partially_closed_positions": partial, "partial_closing_summary": partial_summary,
            "coverage": {"closed_pages": cm["pages"], "transfer_pages": tm["pages"],
                "closed_ui_pagination_exhausted": cm["ui_pagination_exhausted"],
                "transfer_ui_pagination_exhausted": tm["ui_pagination_exhausted"],
                "history_complete": False, "current_positions_private": private,
                "current_position_rows_observed": current.get("rows_collected", len(current["rows"]) if "rows" in current else None),
                "position_history_rows_observed": len(history),
                "fully_closed_rows_observed": len(closed),
                "partially_closed_rows_observed": len(partial),
                "timezone": None, "full_portfolio_pnl_attribution": False,
                "closed_history_capture": cm, "transfer_history_capture": tm},
            "three_pillars": {"engagement": {"status": "unknown"},
                "performance": {"status": "platform_displayed_not_independently_audited"},
                "recognition": {"status": "unknown"}},
            "findings_vi": findings, "limitations_vi": limitations, "source_receipts": receipts}
    if private is False:
        result["public_current_positions"] = {
            "observed_at": current.get("observed_at"),
            "headers": current.get("headers", []),
            "rows": current.get("rows", []),
            "coverage": current.get("coverage", "visible public rows only, not guaranteed complete"),
        }
    return result


def build_report(capture: dict) -> dict:
    """Build bounded evidence, rejecting unrecognized populated financial layouts."""
    if capture.get("schema") != CAPTURE_SCHEMA:
        raise ValueError(f"Expected input schema {CAPTURE_SCHEMA}")
    portfolios = capture.get("portfolios")
    if not isinstance(portfolios, list):
        raise ValueError("Capture portfolios must be a list")
    with localcontext() as context:
        context.prec = 50
        profiles = [_profile(portfolio, capture) for portfolio in portfolios]
    return {"schema": REPORT_SCHEMA, "generated_at": datetime.now(timezone.utc).isoformat(),
            "capture_window": {"started_at": capture.get("started_at"), "completed_at": capture.get("completed_at")},
            "cohort": {"selection_vi": "Ba portfolio được chọn từ trước theo bằng chứng lịch sử; không phải bảng xếp hạng toàn sàn hoặc cam kết đang có lãi.", "not_global_top": True},
            "profiles": profiles,
            "limitations": ["Nguồn UI công khai được người vận hành chụp; không phải kiểm toán độc lập.",
                            "Không suy diễn phiếu bầu, tự khai hoặc lịch sử thiếu thành kỹ năng đầu tư đã xác minh."]}


def profile_csv(report: dict) -> str:
    """Export exact IDs and separate currency summaries, preserving nulls as JSON."""
    output = io.StringIO(newline="")
    fields = ["portfolio_id", "display_name", "source_url", "observed_at", "period_label", "roi_percent",
              "leader_pnl", "leader_pnl_currency", "copier_pnl", "copier_pnl_currency", "closed_count",
              "closed_currencies_json", "partial_count", "partial_currencies_json",
              "transfer_count", "transfer_assets_json", "current_positions_private"]
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for profile in report["profiles"]:
        row = {key: profile.get(key) for key in fields[:4]}
        row.update({key: profile["snapshot"].get(key) for key in fields[4:10]})
        row.update(closed_count=profile["closed_summary"]["count"],
                   closed_currencies_json=json.dumps(profile["closed_summary"]["currencies"], ensure_ascii=False),
                   partial_count=profile["partial_closing_summary"]["count"],
                   partial_currencies_json=json.dumps(profile["partial_closing_summary"]["currencies"], ensure_ascii=False),
                   transfer_count=profile["transfer_summary"]["count"],
                   transfer_assets_json=json.dumps(profile["transfer_summary"]["assets"], ensure_ascii=False),
                   current_positions_private=json.dumps(profile["coverage"]["current_positions_private"]))
        writer.writerow(row)
    return output.getvalue()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--csv-output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = build_report(json.loads(args.input.read_text(encoding="utf-8")))
    except (ValueError, TypeError, KeyError) as exc:
        parser.error(str(exc))
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with args.csv_output.open("w", encoding="utf-8", newline="") as handle:
        handle.write(profile_csv(report))


if __name__ == "__main__":
    main()
