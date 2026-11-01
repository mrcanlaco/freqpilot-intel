from __future__ import annotations

import copy
import csv
import io
import json
import unittest
from decimal import Decimal

from bti.portfolio_research import build_report, profile_csv


def position(pnl="10.00 USDT", *, symbol="TESTUSDT", side="Long", leverage=5,
             opened="2025-01-01 00:00:00", closed="2025-01-01 02:00:00", status="Closed"):
    return "\n".join([symbol, "Perp", f"{leverage}x", "Cross", side, status,
                      "Opened", opened, "Entry Price", "100.00 USDT",
                      "Max. Open Interest", "2 TEST", "Closing PNL", pnl,
                      "Closed", closed, "Avg. Close Price", "110.00 USDT", "Closed Vol.", "3 TEST"])


def capture(rows=None, transfers=None, performance=None):
    def tab(items, headers):
        return {"pages": [{"page_index": 1, "observed_at": "2026-10-01T00:00:00Z",
                           "rows": items, "headers": headers, "has_next": False,
                           "private_notice": False, "loading": False}],
                "ui_pagination_exhausted": True, "full_source_history_guaranteed": False}
    return {"schema": "bti-public-portfolio-ui-capture-v1", "started_at": "2026-10-01T00:00:00Z",
            "completed_at": "2026-10-01T00:01:00Z", "stop": None,
            "capture_script_sha256": "capture-hash", "portfolios": [{
                "portfolio_id": "0009223372036854775809", "source_url": "https://www.binance.com/example",
                "access": {"portfolio_name": 'Example, "name"\nsecond line', "observed_at": "2026-10-01T00:00:00Z"},
                "performance": {"text": performance if performance is not None else
                    "Performance\n30 Days\nROI\nPnL\n-2.50%\n-1,234.50\nCopier PnL\n+2.00 USDT\nSharpe Ratio\n--\nMDD\n4.00%\nWin Rate\n0.00%\nWin Positions\n0\nTotal Positions\n1",
                    "observed_at": "2026-10-01T00:00:10Z"},
                "tabs": {"Position History": tab(rows if rows is not None else [], []),
                         "Transfer History": tab(transfers if transfers is not None else [], ["Time", "Coin", "Amount", "From", "To"])},
                "current_positions": {"private_notice": True, "rows_collected": 0},
                "projection_sha256": "projection-hash"}]}


def profile(data):
    return build_report(data)["profiles"][0]


class PortfolioResearchTests(unittest.TestCase):
    def test_snapshot_currency_zero_unknown_and_negative_are_distinct(self):
        p = profile(capture())
        self.assertEqual(p["snapshot"]["leader_pnl"], "-1234.50")
        self.assertIsNone(p["snapshot"]["leader_pnl_currency"])
        self.assertEqual(p["snapshot"]["copier_pnl_currency"], "USDT")
        self.assertEqual(p["snapshot"]["win_rate_percent"], "0.00")
        self.assertEqual(p["snapshot"]["win_positions"], 0)
        self.assertIsNone(p["snapshot"]["sharpe_ratio"])
        self.assertEqual(p["snapshot"]["roi_percent"], "-2.50")
        self.assertEqual(len(build_report(capture())["profiles"]), 1)

    def test_explicit_leader_currency_is_not_discarded(self):
        data = capture()
        perf = data["portfolios"][0]["performance"]
        perf["text"] = perf["text"].replace("-1,234.50", "+1,234.50 BTC")
        self.assertEqual(profile(data)["snapshot"]["leader_pnl_currency"], "BTC")

    def test_source_pnl_not_prices_and_old_rows_not_filtered_to_snapshot(self):
        p = profile(capture([position("-3.75 USDT")]))
        row = p["closed_positions"][0]
        self.assertEqual(row["closing_pnl"], "-3.75")
        self.assertEqual(row["closed_volume"], "3")
        self.assertEqual(row["max_open_interest"], "2")
        self.assertEqual(row["holding_hours_wall_clock"], "2")
        self.assertIsNone(row["timezone"])
        self.assertEqual(p["snapshot"]["total_positions"], 1)
        self.assertEqual(p["closed_summary"]["currencies"][0]["net_pnl"], "-3.75")
        self.assertFalse(p["coverage"]["full_portfolio_pnl_attribution"])
        self.assertEqual(row["opened_at_display"], "2025-01-01 00:00:00")
        self.assertNotIn("period_start", p["snapshot"])

    def test_currency_buckets_profit_factor_flat_and_concentration(self):
        rows = [position("10 USDT"), position("20 USDT", side="Short"),
                position("30 USDT"), position("40 USDT"), position("-25 USDT"),
                position("0 USDT"), position("-2 BTC", symbol="OTHER")]
        p = profile(capture(rows))
        buckets = {b["currency"]: b for b in p["closed_summary"]["currencies"]}
        usdt = buckets["USDT"]
        self.assertEqual(Decimal(usdt["net_pnl"]), Decimal(75))
        self.assertEqual(Decimal(usdt["gross_profit"]), Decimal(100))
        self.assertEqual(Decimal(usdt["gross_loss"]), Decimal(25))
        self.assertEqual(Decimal(usdt["profit_factor"]), Decimal(4))
        self.assertEqual(Decimal(usdt["top3_positive_pnl_share_percent"]), Decimal(90))
        self.assertEqual((usdt["wins"], usdt["losses"], usdt["flat"]), (4, 1, 1))
        self.assertEqual(Decimal(buckets["BTC"]["net_pnl"]), Decimal(-2))
        self.assertEqual(p["closed_summary"]["top_winners"], [])
        by_side = {(r["side"], r["currency"]): r for r in p["closed_summary"]["by_side"]}
        self.assertEqual(Decimal(by_side[("Short", "USDT")]["net_pnl"]), Decimal(20))
        self.assertEqual(len(p["closed_summary"]["by_symbol"]), 2)

    def test_no_loss_or_no_profit_denominators_remain_unknown(self):
        winners = profile(capture([position("1 USDT")]))["closed_summary"]["currencies"][0]
        self.assertIsNone(winners["profit_factor"])
        self.assertEqual(Decimal(winners["top3_positive_pnl_share_percent"]), Decimal(100))
        flat = profile(capture([position("0 USDT")]))["closed_summary"]["currencies"][0]
        self.assertIsNone(flat["profit_factor"])
        self.assertIsNone(flat["top3_positive_pnl_share_percent"])
        self.assertEqual(Decimal(flat["win_rate_percent"]), Decimal(0))

    def test_partial_positions_and_duration_median(self):
        rows = [position("2 USDT", closed="--", status="Partially Closed", leverage=20),
                position("1 USDT", leverage=3),
                position("-1 USDT", closed="2025-01-01 06:00:00", leverage=10)]
        p = profile(capture(rows))
        summary = p["closed_summary"]
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["partially_closed_count"], 0)
        self.assertEqual(p["partial_closing_summary"]["count"], 1)
        self.assertEqual(Decimal(p["partial_closing_summary"]["currencies"][0]["net_pnl"]), Decimal(2))
        self.assertEqual(Decimal(summary["currencies"][0]["net_pnl"]), Decimal(0))
        self.assertEqual(p["coverage"]["position_history_rows_observed"], 3)
        self.assertEqual(p["coverage"]["fully_closed_rows_observed"], 2)
        self.assertEqual(p["coverage"]["partially_closed_rows_observed"], 1)
        self.assertEqual(summary["holding_duration_observed_count"], 2)
        self.assertEqual(Decimal(summary["holding_median_hours_wall_clock"]), Decimal(4))
        self.assertEqual((summary["leverage_min"], summary["leverage_max"]), (3, 10))
        self.assertEqual(p["partially_closed_positions"][0]["closed_at_display"], "--")
        self.assertIsNone(p["partially_closed_positions"][0]["holding_hours_wall_clock"])
        self.assertEqual(len(summary["top_winners"]), 1)
        self.assertEqual(summary["top_losers"][0]["closing_pnl"], "-1")

    def test_transfers_separate_assets_and_unknown_direction(self):
        time = "2026-01-01 00:00:00"
        transfers = [[time, "USDT", "1,000.12345678", "Spot", "Lead Trading"],
                     [time, "USDT", "100", "Lead Trading", "Spot"],
                     [time, "BNB", "2", "Spot", "Lead Trading"],
                     [time, "BNB", "1", "Spot", "Other"],
                     [time, "BTC", "--", "Spot", "Lead Trading"]]
        p = profile(capture([position("1 USDT")], transfers))
        assets = {a["asset"]: a for a in p["transfer_summary"]["assets"]}
        self.assertEqual(Decimal(assets["USDT"]["net_flow"]), Decimal("900.12345678"))
        self.assertEqual(Decimal(assets["BNB"]["inflow"]), Decimal(2))
        self.assertIsNone(assets["BNB"]["net_flow"])
        self.assertEqual(assets["BNB"]["unknown_direction_count"], 1)
        self.assertIsNone(assets["BTC"]["inflow"])
        self.assertIsNone(assets["BTC"]["net_flow"])
        self.assertEqual(Decimal(p["closed_summary"]["currencies"][0]["net_pnl"]), Decimal(1))
        self.assertNotIn("record_key", p["transfers"][0])

    def test_duplicate_pages_do_not_remove_row_occurrences(self):
        data = capture([position(), position()])
        tab = data["portfolios"][0]["tabs"]["Position History"]
        duplicate = copy.deepcopy(tab["pages"][0])
        duplicate["page_index"] = 2
        duplicate["observed_at"] = "2026-10-01T00:00:20Z"
        tab["pages"].append(duplicate)
        p = profile(data)
        self.assertEqual(p["closed_summary"]["count"], 2)
        self.assertEqual(p["closed_positions"][0]["record_key"], p["closed_positions"][1]["record_key"])
        self.assertEqual(p["closed_positions"][1]["source_row_index"], 2)
        self.assertEqual(p["coverage"]["closed_pages"], 2)
        self.assertEqual(p["coverage"]["closed_history_capture"]["duplicate_pages"], 1)

    def test_stopped_loading_and_count_mismatch_keep_public_rows(self):
        data = capture([position()])
        data["stop"] = {"reason": "page_cap"}
        tab = data["portfolios"][0]["tabs"]["Position History"]
        tab["ui_pagination_exhausted"] = False
        tab["pages"][0].update(loading=True, private_notice=False, visible_row_count=2)
        p = profile(data)
        self.assertEqual(p["closed_summary"]["count"], 1)
        self.assertFalse(p["coverage"]["history_complete"])
        self.assertFalse(p["coverage"]["closed_ui_pagination_exhausted"])
        coverage = p["coverage"]["closed_history_capture"]
        self.assertTrue(coverage["loading"])
        self.assertEqual(coverage["row_count_mismatches"][0]["rows_collected"], 1)

    def test_private_history_rows_are_never_publishable(self):
        for kind, output_key in (("Position History", "closed_positions"), ("Transfer History", "transfers")):
            with self.subTest(kind=kind):
                data = capture([position("5.00 USDT")], transfers=[
                    ["2026-01-01 00:00:00", "USDT", "100", "Spot", "Lead Trading"]])
                page = data["portfolios"][0]["tabs"][kind]["pages"][0]
                page["private_notice"] = True
                with self.assertRaises(ValueError):
                    build_report(data)
                page["rows"] = []
                self.assertEqual(profile(data)[output_key], [])

    def test_private_missing_public_current_and_pillars_are_not_equity(self):
        data = capture()
        p = profile(data)
        self.assertTrue(p["coverage"]["current_positions_private"])
        self.assertEqual(p["coverage"]["current_position_rows_observed"], 0)
        self.assertNotIn("public_current_positions", p)
        del data["portfolios"][0]["current_positions"]
        missing = profile(data)
        self.assertIsNone(missing["coverage"]["current_positions_private"])
        self.assertIsNone(missing["coverage"]["current_position_rows_observed"])
        source_rows = ["TESTUSDT\nPerp\n5x\n+1.00 USDT\n(+10.00%)"]
        data["portfolios"][0]["current_positions"] = {
            "private_notice": False, "rows_collected": 1, "headers": ["Symbol", "PNL (ROE)"],
            "rows": source_rows, "observed_at": "2026-10-01T00:00:30Z"}
        public = profile(data)
        self.assertEqual(public["coverage"]["current_position_rows_observed"], 1)
        self.assertEqual(public["public_current_positions"]["rows"], source_rows)
        self.assertEqual(public["public_current_positions"]["headers"], ["Symbol", "PNL (ROE)"])
        self.assertFalse(public["coverage"]["history_complete"])
        self.assertEqual(public["three_pillars"]["engagement"]["status"], "unknown")
        self.assertEqual(public["three_pillars"]["recognition"]["status"], "unknown")
        self.assertEqual(public["verification_level"], "operator_capture")

    def test_missing_performance_and_unknown_pnl_are_not_zero(self):
        p = profile(capture([position("--")], performance=""))
        self.assertTrue(all(v is None for v in p["snapshot"].values()))
        self.assertEqual(p["closed_summary"]["currencies"], [])
        self.assertEqual(p["closed_summary"]["unknown_pnl_count"], 1)
        self.assertIsNone(p["closed_positions"][0]["closing_pnl"])

    def test_unrecognized_financial_shapes_fail_clearly(self):
        cases = [capture([position("10")]), capture([position("1,23.00 USDT")]),
                 capture([position().replace("Closing PNL", "PnL")]),
                 capture([position(closed="2024-01-01 00:00:00")]),
                 capture(performance="Performance\n30 Days\nROI\nPnL\n2\n1"),
                 capture(transfers=[["2026-01-01 00:00:00", "USDT", "-1", "Spot", "Lead Trading"]])]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError):
                build_report(data)

    def test_csv_exact_id_escaping_currency_json_and_receipt_allowlist(self):
        data = capture([position("-1.23 USDT")])
        data["portfolios"][0]["access"]["cookies"] = "secret"
        report = build_report(data)
        text = profile_csv(report)
        self.assertNotIn("\r\n", text)
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["portfolio_id"], "0009223372036854775809")
        self.assertEqual(rows[0]["display_name"], data["portfolios"][0]["access"]["portfolio_name"])
        self.assertEqual(rows[0]["leader_pnl_currency"], "")
        self.assertEqual(json.loads(rows[0]["closed_currencies_json"])[0]["net_pnl"], "-1.23")
        receipts = report["profiles"][0]["source_receipts"]
        self.assertEqual(receipts["projection_sha256"], "projection-hash")
        self.assertNotIn("cookies", receipts)
        self.assertEqual(receipts["performance_text"], data["portfolios"][0]["performance"]["text"])


if __name__ == "__main__":
    unittest.main()
