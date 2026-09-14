"""Shared test helpers."""


def strip_sql_comments(sql: str) -> str:
    """Removes line comments and blank lines, leaving executable SQL only.

    Needed because every SQL template in this repo opens with an explanatory
    comment block. Assertions about what the SQL *does* must not accidentally
    match prose that merely mentions a keyword -- several of these templates now
    discuss `total_amount` at length precisely to explain why it is excluded.
    """
    lines = []
    for line in sql.splitlines():
        code = line.split("--", 1)[0].strip()
        if code:
            lines.append(code)
    return "\n".join(lines)
