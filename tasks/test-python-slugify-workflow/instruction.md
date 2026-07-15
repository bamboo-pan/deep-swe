The small `flowcheck` package contains `slugify_label()` in
`flowcheck/text.py`. Its current implementation only handles simple spaces.

Update `slugify_label(value)` so that it follows these rules:

- `value` must be a string. Raise `TypeError` for every other input type.
- Convert ASCII letters to lowercase.
- Replace each maximal run of characters outside `[a-z0-9]` with one hyphen.
- Remove leading and trailing hyphens.
- Return `"untitled"` if no letters or digits remain.

Do not change the existing `collapse_spaces()` behavior. Use only the Python
standard library. Commit the completed change; grading reads committed work.
