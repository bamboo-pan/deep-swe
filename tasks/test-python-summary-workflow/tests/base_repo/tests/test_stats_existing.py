from flowcheck import summarize_numbers


def test_summary_handles_empty_iterable():
    result = summarize_numbers([])
    assert result["count"] == 0
    assert result["total"] == 0
    assert result["average"] is None


def test_summary_calculates_legacy_fields():
    result = summarize_numbers([2, 4, 6])
    assert result["count"] == 3
    assert result["total"] == 12
    assert result["average"] == 4


def test_summary_accepts_tuples():
    assert summarize_numbers((1, 3))["average"] == 2
