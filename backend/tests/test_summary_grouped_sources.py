import json

import pytest

from app.compaction import _decode
from app.errors import AppError


def response(labels):
    return {"message": {"content": json.dumps({"summary": "保留原事实", "covered_sources": labels})}}


def test_provider_grouped_sources_have_one_unambiguous_flat_representation():
    decoded = _decode(response([["s1", "s2"], ["s3"]]))
    assert decoded == {"summary": "保留原事实", "covered_sources": ["s1", "s2", "s3"]}


@pytest.mark.parametrize("labels", [[["s1"], [["s2"]]], [["s1"], [42]], [["s1"], "s2"]])
def test_malformed_grouped_sources_still_fail_closed(labels):
    with pytest.raises(AppError) as error:
        _decode(response(labels))
    assert error.value.code == "SUMMARY_INVALID"
