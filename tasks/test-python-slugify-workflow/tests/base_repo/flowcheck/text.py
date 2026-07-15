"""Small text helpers used by the workflow test fixture."""


def collapse_spaces(value: str) -> str:
    """Collapse all whitespace runs to one regular space."""
    return " ".join(value.split())


def slugify_label(value: str) -> str:
    """Return a lowercase label with regular spaces replaced by hyphens."""
    return value.strip().lower().replace(" ", "-")
