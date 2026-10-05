"""The report's tables, from a bench run dir and the run's trace."""

from __future__ import annotations

import json
from pathlib import Path

from demo_hub.report import load_runs, tables
from demo_hub.telemetry import TraceStore
from tests.test_telemetry import record, turn_events


def test_the_report_tables_come_from_runs_and_their_traces(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / "traces")
    rec, _ = record(turn_events())
    trace = rec.to_dict()
    trace["spans"][2]["attrs"]["gateway_overhead_ms"] = 41.0
    store.save(trace)
    out = tmp_path / "granite-progressive"
    out.mkdir()
    (out / "meta.json").write_text(json.dumps({"disclosure": "progressive",
                                               "target": "gateway-recipes"}))
    (out / "runs.jsonl").write_text(json.dumps({
        "model": "ollama:granite4.2:8b#think=false", "case": "tomato-penne-no-us", "rep": 1,
        "passed": True, "seconds": 160.0, "trace_id": trace["id"],
        "answer_confidence": 1.0}) + "\n")
    text = tables(load_runs([out]), store)
    assert "| `ollama:granite4.2:8b#think=false · progressive` | 1 | 1/1 | 1.00 | 2.7 min |" in text
    assert "| `pantry-plan-recipe` | 1 | 280 ms | 41.0 ms |" in text
    assert "| select_products | 1 | 40.0 ms |" in text
    assert "$0" in text and "gemini-3-flash-preview" in text
