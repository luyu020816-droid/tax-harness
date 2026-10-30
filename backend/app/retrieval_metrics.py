"""Gold-unit retrieval metrics and development-only threshold calibration."""

import math
import random
from collections import defaultdict


def recall_units(ranked, required, chunk_units, k=20):
    required = set(required)
    found = set().union(*(set(chunk_units.get(str(key), [])) for key in ranked[:k]))
    return len(required & found) / len(required) if required else None


def ndcg(ranked, grades, k=10):
    def dcg(values):
        return sum((2**grade - 1) / math.log2(i + 2) for i, grade in enumerate(values))

    ideal = dcg(sorted(grades.values(), reverse=True)[:k])
    return dcg([grades.get(str(key), 0) for key in ranked[:k]]) / ideal if ideal else None


def calibrate(positive_scores, negative_scores):
    """Minimize false accepts first, then retain the most positive questions.

    A positive score is the strongest gold-supporting seed for that question;
    negatives use their strongest candidate. No holdout values are accepted here.
    """
    if not positive_scores or not negative_scores:
        raise ValueError("Both answerable and unanswerable development questions are required")
    values = sorted(set([*positive_scores, *negative_scores]))
    if not all(math.isfinite(v) for v in values):
        raise ValueError("Calibration scores must be finite")
    cutoffs = [
        values[0] - 1,
        *[(a + b) / 2 for a, b in zip(values, values[1:], strict=False)],
        values[-1] + 1,
    ]
    results = []
    for cutoff in cutoffs:
        accepted_negative = sum(v >= cutoff for v in negative_scores)
        retained_positive = sum(v >= cutoff for v in positive_scores)
        results.append((accepted_negative, -retained_positive, cutoff))
    false_accepts, negative_tp, cutoff = min(results)
    return cutoff, {
        "objective": "min_false_accepts_then_max_gold_seed_retention",
        "positive_questions": len(positive_scores),
        "negative_questions": len(negative_scores),
        "development_false_accepts": false_accepts,
        "development_gold_seed_retained": -negative_tp,
        "raw_logits_are_probabilities": False,
    }


def paired_family_interval(rows, left, right, *, seed=20261030, samples=2000):
    """Percentile cluster bootstrap, paired by document family, not question."""
    families = defaultdict(list)
    for row in rows:
        if row.get(left) is not None and row.get(right) is not None:
            families[row["family"]].append(row[left] - row[right])
    values = [sum(v) / len(v) for v in families.values()]
    if not values:
        return None
    rng = random.Random(seed)
    draws = sorted(sum(rng.choices(values, k=len(values))) / len(values) for _ in range(samples))
    return {
        "mean_difference": sum(values) / len(values),
        "lower_95": draws[int(samples * 0.025)],
        "upper_95": draws[min(samples - 1, int(samples * 0.975))],
        "families": len(values),
        "method": "paired_document_family_percentile_bootstrap",
        "seed": seed,
        "samples": samples,
    }
