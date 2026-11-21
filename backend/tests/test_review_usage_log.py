"""Token use of the AI calls inside the tools, for the comparison.

In composed mode those calls are hidden inside one tool, so the agent log
cannot see them. With EMAILCHRONO_REVIEW_USAGE_LOG set, every AI call appends
one JSON line, so both modes can be costed the same way.
"""

import json

from app.review import chronology


def test_each_ai_call_appends_a_usage_line(planted_case, monkeypatch, tmp_path):
    log = tmp_path / "usage.jsonl"
    monkeypatch.setenv(chronology.MODEL_STUB_ENV, "tests.chronology_stub:respond")
    monkeypatch.setenv(chronology.USAGE_LOG_ENV, str(log))
    merged = planted_case["card"]("02_lighting_fr.eml")

    split = chronology.propose_splits(merged)
    chronology.propose_fields(merged, split)

    lines = [json.loads(line) for line in log.read_text().splitlines()]
    assert [line["task"] for line in lines] == ["split", "fields"]
    for line in lines:
        assert line["ok"] is True
        assert {"model", "input_tokens", "output_tokens", "seconds"} <= set(line)


def test_no_usage_file_without_the_setting(planted_case, monkeypatch, tmp_path):
    monkeypatch.setenv(chronology.MODEL_STUB_ENV, "tests.chronology_stub:respond")
    monkeypatch.delenv(chronology.USAGE_LOG_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    chronology.propose_splits(planted_case["card"]("02_lighting_fr.eml"))
    assert list(tmp_path.iterdir()) == []
