"""Numeric helpers used by the workflow test fixture."""


def summarize_numbers(values):
    """Return the count, total, and average for an iterable of numbers."""
    numbers = list(values)
    if not numbers:
        return {"count": 0, "total": 0, "average": None}
    total = sum(numbers)
    return {
        "count": len(numbers),
        "total": total,
        "average": total / len(numbers),
    }
