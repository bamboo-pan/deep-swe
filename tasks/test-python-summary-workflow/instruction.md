The small `flowcheck` package contains `summarize_numbers()` in
`flowcheck/stats.py`. Complete the function with the following behavior:

- Accept any iterable that terminates (the function does not need to support an
  infinite iterator).
- Every value must be an `int` or `float`, but booleans are invalid.
  Raise `TypeError` if any value is invalid.
- Return a dictionary with exactly these keys: `count`, `total`, `average`,
  `minimum`, and `maximum`.
- For a non-empty iterable, calculate all five values normally.
- For an empty iterable, return `count` and `total` as `0`; return `None` for
  `average`, `minimum`, and `maximum`.

Use only the Python standard library. Commit the completed change; grading
reads committed work.
