from app.answer_renderer import _uncovered_public_queries


def result(city, day, *, excerpt="晴"):
    return {
        "ok": True,
        "data": {
            "kind": "public_web",
            "city": city,
            "weather_date": day,
            "query": f"{city} {day}",
            "metric": "overview",
            "results": [{"excerpt": excerpt}],
        },
    }


def test_duplicate_queries_can_be_satisfied_by_either_actual_result():
    outputs = {
        "first": result("南京", "2026-11-12"),
        "second": result("南京", "2026-11-12", excerpt="多云"),
    }
    assert not _uncovered_public_queries(outputs, {"first"})
    assert not _uncovered_public_queries(outputs, {"second"})
    assert _uncovered_public_queries(outputs, {"invented"})


def test_different_city_or_date_does_not_count_as_same_query():
    outputs = {
        "nanjing": result("南京", "2026-11-12"),
        "shanghai": result("上海", "2026-11-12"),
        "tomorrow": result("南京", "2026-11-13"),
    }
    assert len(_uncovered_public_queries(outputs, {"nanjing"})) == 2
    assert not _uncovered_public_queries(outputs, set(outputs))
