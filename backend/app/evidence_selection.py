"""Structural evidence groups and a profile-bound, development-calibrated selector."""

import json
import math
import re
from pathlib import Path

from app.values import digest

SELECTION_VERSION = "structural-groups-v1"
LIMIT_TOKENS, LIMIT_GROUPS = 3000, 6


def load_policy(path, profile_id):
    try:
        policy = json.loads(Path(path).read_text(encoding="utf-8"))
        if (
            policy["version"] != SELECTION_VERSION
            or policy["profile_id"] != profile_id
            or policy["mode"] != "hybrid"
            or not math.isfinite(policy["cutoff"])
            or not re.fullmatch(r"[a-f0-9]{64}", policy["development_sha256"])
        ):
            return None, "SELECTION_POLICY_MISMATCH"
        return {**policy, "policy_id": digest(policy)}, None
    except (OSError, ValueError, KeyError, TypeError):
        return None, "SELECTION_POLICY_NOT_CALIBRATED"


def structural_keys(blocks):
    """Conservative grouping from explicit structure, never inferred legal meaning.

    Subheadings/notes stay with their parent clause. Unheaded continuation stays
    with the preceding numbered clause; an entirely unstructured document is one
    group. This can reject a large ambiguous group instead of losing an exception.
    """
    keys, anchor, heading = {}, "unheaded", None
    for block in sorted(blocks, key=lambda b: b["ordinal"]):
        text = block.get("prefix", block.get("content", ""))
        current = block["heading_path"].strip()
        special = bool(
            re.match(r"^(?:#+\s*)?(?:例外|除外|限制|附注|注释|补充|说明|注[:：])", current or text)
        )
        if current and current != heading:
            level = len(current) - len(current.lstrip("#"))
            if not special and (level <= 2 or heading is None):
                anchor = "heading:" + str(block["ordinal"])
            heading = current
        elif not current and re.match(
            r"^\s*(?:第[一二三四五六七八九十百零\d]+条|\d+[、.．]\s)", text
        ):
            anchor = "clause:" + str(block["ordinal"])
        keys[block["ordinal"]] = anchor
    return keys


def components(blocks, chunks):
    keys = structural_keys(blocks)
    parent = {str(c["chunk_id"]): str(c["chunk_id"]) for c in chunks}

    def root(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    first = {}
    for chunk in chunks:
        key = str(chunk["chunk_id"])
        for ordinal in chunk["block_ordinals"]:
            group = keys[ordinal]
            if group in first:
                parent[root(key)] = root(first[group])
            else:
                first[group] = key
    groups = {}
    for chunk in chunks:
        groups.setdefault(root(str(chunk["chunk_id"])), []).append(chunk)
    return list(groups.values())


def choose_groups(
    candidates,
    blocks_by_version,
    chunks_by_version,
    *,
    policy=None,
    mode="hybrid",
    budget=LIMIT_TOKENS,
):
    groups, lookup = {}, {}
    for version, chunks in chunks_by_version.items():
        for members in components(blocks_by_version[version], chunks):
            members.sort(key=lambda c: c["ordinal"])
            key = digest([version, [str(c["chunk_id"]) for c in members]])
            groups[key] = members
            lookup.update({str(c["chunk_id"]): key for c in members})
    calibrated = bool(policy and mode == policy["mode"])
    seeds = [
        c
        for c in candidates
        if not calibrated or c.get("rerank_logit", -math.inf) >= policy["cutoff"]
    ]
    ordered = list(dict.fromkeys(lookup[str(c["chunk_id"])] for c in seeds))
    chosen, trace, consumed = [], [], 0
    for position, key in enumerate(ordered):
        members = groups[key]
        cost = sum(c["estimated_tokens"] for c in members)
        fits = position < LIMIT_GROUPS and consumed + cost <= budget
        trace.append(
            {
                "group_id": key,
                "seed_chunk_ids": [
                    str(c["chunk_id"]) for c in seeds if lookup[str(c["chunk_id"])] == key
                ],
                "chunk_ids": [str(c["chunk_id"]) for c in members[:40]],
                "chunk_count": len(members),
                "chunk_ids_complete": len(members) <= 40,
                "estimated_tokens": cost,
                "selected": fits,
            }
        )
        if fits:
            chosen.extend(str(c["chunk_id"]) for c in members)
            consumed += cost
    omitted = any(not g["selected"] for g in trace)
    return chosen, {
        "selection_version": SELECTION_VERSION,
        "selection_calibrated": calibrated,
        "selection_policy_id": policy["policy_id"] if calibrated else None,
        "selection_cutoff": policy["cutoff"] if calibrated else None,
        "evidence_groups": trace,
        "evidence_tokens": consumed,
        "evidence_budget": budget,
        "budget_incomplete": omitted,
        "qualified_candidates": len(seeds),
    }
