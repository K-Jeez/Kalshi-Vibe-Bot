"""Fixture tests for the observation-only research ledger.

These checks are not a trading result. The JSON files are synthetic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import research_ledger as ledger  # noqa: E402

FIXTURES = TOOLS / "research_ledger_fixtures"
FORBIDDEN = (
    "place_order",
    "place_buy",
    "cancel_order",
    "KALSHI_API",
    "import httpx",
    "import requests",
    "urllib",
    "src.clients",
    "src.bot",
    "src.config",
)


def _experiment() -> dict:
    return json.loads((FIXTURES / "experiment.json").read_text(encoding="utf-8"))


def _snapshot(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_source_has_no_order_or_network_path():
    text = (TOOLS / "research_ledger.py").read_text(encoding="utf-8")
    for token in FORBIDDEN:
        assert token not in text
    assert ledger.__name__ == "research_ledger"


def test_fixture_round_trip_scores_without_inventing_empty_metrics(tmp_path):
    book = ledger.ResearchLedger(tmp_path / "ledger")
    book.init(_experiment())
    assert book.record_snapshot(_snapshot("snapshot_oos_yes.json"))["status"] == "recorded"
    assert book.record_snapshot(_snapshot("snapshot_oos_yes.json"))["status"] == "duplicate"
    assert book.record_snapshot(_snapshot("snapshot_oos_no.json"))["status"] == "recorded"
    assert book.record_outcomes(json.loads((FIXTURES / "outcomes.json").read_text(encoding="utf-8")))
    report = book.score()
    score = report["pre_registered_scorecard"]
    assert score["n"] == 2
    assert score["brier_model"] < score["brier_market_mid"]
    assert score["hypothetical"]["n_trades"] == 2
    assert score["hypothetical"]["side_win_rate"] == 1.0
    assert score["event_bootstrap"]["status"] == "ok"
    assert "live_order" not in json.dumps(report)
    early = dict(_snapshot("snapshot_oos_yes.json"))
    early["market_id"] = "SYNTHETIC-EARLY"
    early["forecast_time"] = "2026-11-08T11:00:00Z"
    early["quote_time"] = "2026-11-08T10:59:00Z"
    book.record_snapshot(early)
    book.record_outcome(
        {"market_id": "SYNTHETIC-EARLY", "resolved_at": "2026-11-08T18:00:00Z", "result": "yes"}
    )
    again = book.score()
    assert again["pre_registered_scorecard"]["n"] == 2
    assert again["exploratory_not_the_result"]["n"] == 1


def test_changed_snapshot_is_rejected_and_file_is_unchanged(tmp_path):
    book = ledger.ResearchLedger(tmp_path / "ledger")
    book.init(_experiment())
    book.record_snapshot(_snapshot("snapshot_oos_yes.json"))
    before = (tmp_path / "ledger" / "forecasts.jsonl").read_text(encoding="utf-8")
    changed = dict(_snapshot("snapshot_oos_yes.json"))
    changed["ai_probability_yes"] = 0.2
    try:
        book.record_snapshot(changed)
    except ledger.LedgerError as exc:
        assert "different contents" in str(exc)
    else:
        raise AssertionError("changed snapshot was accepted")
    assert (tmp_path / "ledger" / "forecasts.jsonl").read_text(encoding="utf-8") == before


def test_malformed_stale_and_one_sided_quotes_are_rejected(tmp_path):
    book = ledger.ResearchLedger(tmp_path / "ledger")
    book.init(_experiment())
    stale = dict(_snapshot("snapshot_oos_yes.json"))
    stale["quote_time"] = "2026-11-08T10:00:00Z"
    missing = dict(_snapshot("snapshot_oos_yes.json"))
    del missing["no_ask"]
    broken = dict(_snapshot("snapshot_oos_yes.json"))
    broken["ai_probability_yes"] = 1.4
    late_quote = dict(_snapshot("snapshot_oos_yes.json"))
    late_quote["quote_time"] = "2026-11-08T13:05:00Z"
    for payload in (stale, missing, broken, late_quote):
        try:
            book.record_snapshot(payload)
        except ledger.LedgerError:
            pass
        else:
            raise AssertionError(f"accepted bad snapshot {payload}")
    assert (tmp_path / "ledger" / "forecasts.jsonl").read_text(encoding="utf-8") == ""


def test_outcome_before_forecast_is_excluded_and_not_a_win_rate(tmp_path):
    book = ledger.ResearchLedger(tmp_path / "ledger")
    book.init(_experiment())
    book.record_snapshot(_snapshot("snapshot_oos_yes.json"))
    leaked = {
        "market_id": "SYNTHETIC-YES",
        "resolved_at": "2026-11-08T12:30:00Z",
        "result": "yes",
    }
    try:
        book.record_outcome(leaked)
    except ledger.LedgerError as exc:
        assert "not after" in str(exc)
    else:
        raise AssertionError("leaking outcome was stored")
    # A hand-edited outcome line still cannot enter the scorecard.
    line = dict(leaked)
    line["record_sha256"] = "hand-edit"
    with (tmp_path / "ledger" / "outcomes.jsonl").open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")
    report = book.score()
    assert report["counts"]["leakage_excluded"] == 1
    assert report["counts"]["joined"] == 0
    assert report["pre_registered_scorecard"]["n"] == 0
    assert report["pre_registered_scorecard"]["brier_model"] is None
    assert report["pre_registered_scorecard"]["hypothetical"]["side_win_rate"] is None


def test_missing_outcome_is_counted_and_not_scored(tmp_path):
    book = ledger.ResearchLedger(tmp_path / "ledger")
    book.init(_experiment())
    book.record_snapshot(_snapshot("snapshot_oos_no.json"))
    report = book.score()
    assert report["counts"]["missing_outcome"] == 1
    assert report["pre_registered_scorecard"]["n"] == 0


def test_edited_experiment_refuses_to_score(tmp_path):
    book = ledger.ResearchLedger(tmp_path / "ledger")
    book.init(_experiment())
    book.record_snapshot(_snapshot("snapshot_oos_yes.json"))
    config = json.loads((tmp_path / "ledger" / "experiment.json").read_text(encoding="utf-8"))
    config["hypothetical"]["min_edge_points"] = 0
    (tmp_path / "ledger" / "experiment.json").write_text(json.dumps(config), encoding="utf-8")
    try:
        book.score()
    except ledger.LedgerError as exc:
        assert "frozen" in str(exc)
    else:
        raise AssertionError("edited experiment was scored")


def test_tampered_forecast_line_is_not_scored(tmp_path):
    book = ledger.ResearchLedger(tmp_path / "ledger")
    book.init(_experiment())
    book.record_snapshot(_snapshot("snapshot_oos_yes.json"))
    book.record_outcomes(json.loads((FIXTURES / "outcomes.json").read_text(encoding="utf-8")))
    path = tmp_path / "ledger" / "forecasts.jsonl"
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    row["ai_probability_yes"] = 0.99
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    report = book.score()
    assert report["counts"]["malformed"] == 1
    assert report["counts"]["joined"] == 0
    assert report["pre_registered_scorecard"]["brier_model"] is None


def test_equal_resolve_time_does_not_enter_the_scorecard(tmp_path):
    book = ledger.ResearchLedger(tmp_path / "ledger")
    book.init(_experiment())
    book.record_snapshot(_snapshot("snapshot_oos_yes.json"))
    try:
        book.record_outcome(
            {
                "market_id": "SYNTHETIC-YES",
                "resolved_at": "2026-11-08T13:00:00Z",
                "result": "yes",
            }
        )
    except ledger.LedgerError as exc:
        assert "not after" in str(exc)
    else:
        raise AssertionError("resolve time equal to the forecast was stored")


def test_one_event_does_not_invent_a_confidence_interval(tmp_path):
    book = ledger.ResearchLedger(tmp_path / "ledger")
    book.init(_experiment())
    first = _snapshot("snapshot_oos_yes.json")
    second = dict(first)
    second["market_id"] = "SYNTHETIC-YES-2"
    second["forecast_time"] = "2026-11-08T13:05:00Z"
    second["quote_time"] = "2026-11-08T13:04:30Z"
    book.record_snapshot(first)
    book.record_snapshot(second)
    book.record_outcome(
        {"market_id": "SYNTHETIC-YES", "resolved_at": "2026-11-08T18:00:00Z", "result": "yes"}
    )
    book.record_outcome(
        {"market_id": "SYNTHETIC-YES-2", "resolved_at": "2026-11-08T18:00:00Z", "result": "yes"}
    )
    boot = book.score()["pre_registered_scorecard"]["event_bootstrap"]
    assert boot["status"] == "undefined"
    assert boot["n_events"] == 1


def test_wrong_model_id_is_rejected(tmp_path):
    book = ledger.ResearchLedger(tmp_path / "ledger")
    book.init(_experiment())
    bad = dict(_snapshot("snapshot_oos_yes.json"))
    bad["model_id"] = "some-other-model"
    try:
        book.record_snapshot(bad)
    except ledger.LedgerError as exc:
        assert "model_id" in str(exc)
    else:
        raise AssertionError("a different model was recorded into a frozen experiment")
    assert (tmp_path / "ledger" / "forecasts.jsonl").read_text(encoding="utf-8") == ""


def test_cli_score_writes_markdown(tmp_path):
    code = ledger.main(
        [
            "init",
            "--dir",
            str(tmp_path / "ledger"),
            "--config",
            str(FIXTURES / "experiment.json"),
        ]
    )
    assert code == 0
    assert (
        ledger.main(
            [
                "record",
                "--dir",
                str(tmp_path / "ledger"),
                "--snapshot",
                str(FIXTURES / "snapshot_oos_yes.json"),
            ]
        )
        == 0
    )
    assert (
        ledger.main(
            [
                "resolve",
                "--dir",
                str(tmp_path / "ledger"),
                "--outcome",
                str(FIXTURES / "outcomes.json"),
            ]
        )
        == 0
    )
    out = tmp_path / "score"
    assert ledger.main(["score", "--dir", str(tmp_path / "ledger"), "--out", str(out)]) == 0
    text = (out / "scorecard.md").read_text(encoding="utf-8")
    assert "not a live trading result" in text
    assert "Hypothetical" in text
