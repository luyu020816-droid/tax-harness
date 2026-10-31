import json

import pytest

from app.evidence_selection import SELECTION_VERSION, choose_groups, load_policy
from app.retrieval_metrics import calibrate, ndcg, recall_units


def block(ordinal, heading, text="正文"):
    return {
        "ordinal": ordinal,
        "heading_path": heading,
        "content": text,
        "locator": {"type": "lines", "from": ordinal + 1, "to": ordinal + 1},
    }


def chunk(identity, ordinal, blocks, cost=100):
    return {
        "chunk_id": identity,
        "ordinal": ordinal,
        "block_ordinals": blocks,
        "estimated_tokens": cost,
    }


def candidate(identity, score=5):
    return {"chunk_id": identity, "version_id": "v1", "rerank_logit": score}


POLICY = {"policy_id": "policy-test", "mode": "hybrid", "cutoff": 2}


def test_joint_support_requires_both_finite_dense_and_rerank_floor():
    from app.evidence_selection import SUPPORT_SELECTION_VERSION, support_score

    policy = {"version": SUPPORT_SELECTION_VERSION, "similarity_weight": 64, "rerank_floor": -6}
    assert support_score({"rerank_logit": -5, "distance": 0.2}, policy) == pytest.approx(46.2)
    assert support_score({"rerank_logit": -7, "distance": 0}, policy) < -90
    assert support_score({"rerank_logit": 5}, policy) < -90
    assert support_score({"rerank_logit": 5, "distance": float("nan")}, policy) < -90


def test_exception_subheading_follows_main_clause_across_chunks():
    blocks = [block(0, "## 第1条 规定"), block(1, "### 例外与限制"), block(2, "## 第2条 其他")]
    chunks = [chunk("main", 0, [0]), chunk("exception", 1, [1]), chunk("other", 2, [2])]
    ids, trace = choose_groups([candidate("main")], {"v1": blocks}, {"v1": chunks}, policy=POLICY)
    assert ids == ["main", "exception"] and trace["evidence_tokens"] == 200


def test_required_group_never_leaks_a_partial_main_clause_on_budget_failure():
    ids, trace = choose_groups(
        [candidate("main")],
        {"v1": [block(0, "## 第一条"), block(1, "### 例外")]},
        {"v1": [chunk("main", 0, [0], 1800), chunk("exception", 1, [1], 1400)]},
        policy=POLICY,
    )
    assert not ids and trace["budget_incomplete"] and trace["evidence_tokens"] == 0


def test_overlapping_chunks_merge_required_components_transitively():
    ids, _ = choose_groups(
        [candidate("a")],
        {"v1": [block(0, "## A"), block(1, "## B")]},
        {"v1": [chunk("a", 0, [0]), chunk("bridge", 1, [0, 1]), chunk("b", 2, [1])]},
        policy=POLICY,
    )
    assert ids == ["a", "bridge", "b"]


def test_same_text_or_heading_in_different_versions_never_merges():
    ids, _ = choose_groups(
        [candidate("a")],
        {"v1": [block(0, "## A")], "v2": [block(0, "## A")]},
        {"v1": [chunk("a", 0, [0])], "v2": [chunk("foreign", 0, [0])]},
        policy=POLICY,
    )
    assert ids == ["a"]


def test_low_relevance_is_rejected_only_for_calibrated_mode():
    args = ([candidate("a", 1)], {"v1": [block(0, "")]}, {"v1": [chunk("a", 0, [0])]})
    ids, trace = choose_groups(*args, policy=POLICY)
    assert not ids and trace["selection_calibrated"]
    ids, trace = choose_groups(*args, policy=POLICY, mode="lexical_only")
    assert ids == ["a"] and not trace["selection_calibrated"]


def test_policy_is_bound_to_model_profile_and_development_digest(tmp_path):
    path = tmp_path / "policy.json"
    value = {
        "version": SELECTION_VERSION,
        "profile_id": "a" * 64,
        "mode": "hybrid",
        "cutoff": 2.0,
        "development_sha256": "b" * 64,
    }
    path.write_text(json.dumps(value))
    policy, error = load_policy(path, "a" * 64)
    assert policy and not error and len(policy["policy_id"]) == 64
    assert load_policy(path, "c" * 64) == (None, "SELECTION_POLICY_MISMATCH")
    value["cutoff"] = float("nan")
    path.write_text(json.dumps(value))
    assert load_policy(path, "a" * 64)[0] is None


def test_recall_requires_both_main_and_exception_and_ndcg_has_standard_gain():
    units = {"a": ["main"], "b": ["exception"]}
    assert recall_units(["a"], ["main", "exception"], units) == 0.5
    assert recall_units(["a", "b"], ["main", "exception"], units) == 1
    assert ndcg(["a", "b"], {"a": 3, "b": 1}) == 1
    assert 0 < ndcg(["b", "a"], {"a": 3, "b": 1}) < 1


def test_development_calibration_cannot_use_nonfinite_or_one_class_data():
    cutoff, fit = calibrate([8, 9, 7], [1, 2, 3])
    assert (
        3 < cutoff < 7
        and fit["development_false_accepts"] == 0
        and fit["development_gold_seed_retained"] == 3
    )
    with pytest.raises(ValueError):
        calibrate([1], [])
    with pytest.raises(ValueError):
        calibrate([float("nan")], [0])


def test_frozen_suite_has_disjoint_families_and_complete_gold_units():
    from app.config import ROOT

    base = ROOT / "evals/retrieval-v1"
    dev = json.loads((base / "development.json").read_text(encoding="utf-8"))
    holdout = json.loads((base / "holdout.json").read_text(encoding="utf-8"))
    assert not {d["family"] for d in dev["documents"]} & {d["family"] for d in holdout["documents"]}
    assert (
        len(dev["questions"]) == 24
        and len(holdout["questions"]) == 48
        and len(holdout["boundary_questions"]) == 22
    )
    for suite in (dev, holdout):
        docs = {d["id"]: d for d in suite["documents"]}
        for q in suite["questions"]:
            assert q["answerable"] and len(q["required_units"]) == 2
            assert all(
                unit in docs[q["document"]]["units"] and docs[q["document"]]["grades"][unit] == 3
                for unit in q["required_units"]
            )
