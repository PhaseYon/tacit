"""Pure metric functions of benchmark/bench.py (no database)."""

import importlib.util
from pathlib import Path

_path = Path(__file__).resolve().parents[1] / "benchmark" / "bench.py"
_spec = importlib.util.spec_from_file_location("tacit_bench", _path)
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)

PR = "https://github.com/acme/api/pull/7"
ADR = "https://github.com/acme/handbook/blob/main/docs/adr/0001-x.md"


def _q(qid, rank, found, strict=None, ms=10.0, type_="why", history=False):
    return bench.QuestionResult(qid, type_, history, rank, found, strict, ms, [])


def test_doc_url_maps_comment_anchors_to_their_parent():
    assert bench.doc_url(f"{PR}#issuecomment-1") == PR
    assert bench.doc_url(f"{PR}#pullrequestreview-2") == PR
    assert bench.doc_url(ADR) == ADR
    assert bench.gold_docs([f"{PR}#issuecomment-1", PR, ADR]) == [PR, ADR]


def test_rank_and_gold_found():
    ranked = ["a", PR, "b", ADR]
    assert bench.first_rank(ranked, {PR, ADR}) == 2
    assert bench.first_rank(ranked, {"zzz"}) is None
    assert bench.gold_found(ranked, {PR, "zzz"}) == 0.5
    assert bench.gold_found(ranked, set()) == 0.0


def test_summary_metrics():
    qs = [
        _q("q1", 1, 1.0, strict=1, ms=10),
        _q("q2", 4, 0.5, strict=None, ms=20),
        _q("q3", 8, 1.0, strict=8, ms=30),
        _q("q4", None, 0.0, ms=40),
    ]
    s = bench.summarize(qs, 10)
    assert s["n"] == 4
    assert s["recall@5"] == 0.5  # q1, q2
    assert s["recall@10"] == 0.75
    assert s["strict_recall@10"] == 0.5
    assert s["gold_coverage"] == 0.625
    assert s["mrr"] == round((1 + 1 / 4 + 1 / 8) / 4, 3)
    assert s["p50_ms"] == 30  # nearest rank on sorted values, like v0
    assert s["p95_ms"] == 40
    assert bench.summarize([], 10) == {}


def test_fingerprint_changes_only_with_rankings():
    a = [bench.QuestionResult("q1", "why", False, 1, 1.0, 1, 5.0, [PR, ADR])]
    b = [bench.QuestionResult("q1", "why", False, 1, 1.0, 1, 99.0, [PR, ADR])]
    c = [bench.QuestionResult("q1", "why", False, 2, 1.0, 2, 5.0, [ADR, PR])]
    assert bench.fingerprint(a) == bench.fingerprint(b)  # latency doesn't count
    assert bench.fingerprint(a) != bench.fingerprint(c)


def test_cited_urls_keeps_fact_order_and_dedupes():
    facts = [
        {"citations": [{"url": "a"}, {"url": "b"}]},
        {"citations": [{"url": "b"}]},
        {"citations": []},
        {"citations": [{"url": "c"}]},
    ]
    assert bench.cited_urls(facts) == ["a", "b", "c"]


def test_rrf_rewards_documents_both_lists_agree_on():
    fused = bench.rrf(["x", "a", "b"], ["a", "y"])
    assert fused[0] == "a"  # in both lists
    assert set(fused) == {"x", "a", "b", "y"}
    assert bench.rrf(["p", "q"], []) == ["p", "q"]
