"""Runs the SQL in sql/rca against a DuckDB file and formats the results for a terminal or for docs/rca-report.md."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb

from lab.paths import SQL_DIR

RCA_DIR = SQL_DIR / "rca"
HEADER_KEYS = ("question", "technique", "reading")


@dataclass
class Result:
    name: str
    title: str
    question: str
    technique: str
    reading: str
    sql: str
    columns: list[str]
    rows: list[tuple[Any, ...]]


def parse_header(sql: str) -> dict[str, str]:
    """The leading comment block: `-- question:` / `-- technique:` / `-- reading:` with indented continuation lines."""
    found: dict[str, list[str]] = {}
    key = None
    for line in sql.splitlines():
        if not line.startswith("--"):
            break
        text = line[2:].strip()
        match = re.match(r"(question|technique|reading):\s*(.*)", text)
        if match:
            key = match.group(1)
            found[key] = [match.group(2)]
        elif key and text:
            found[key].append(text)
    return {k: " ".join(v) for k, v in found.items()}


def query_files() -> list[Path]:
    return sorted(p for p in RCA_DIR.glob("*.sql") if not p.name.startswith("00_"))


def title_of(path: Path) -> str:
    return path.stem.split("_", 1)[1].replace("_", " ").capitalize()


def run_all(con: duckdb.DuckDBPyConnection, only: str | None = None) -> list[Result]:
    con.execute((RCA_DIR / "00_views.sql").read_text(encoding="utf-8"))
    results = []
    for path in query_files():
        if only and not path.name.startswith(only):
            continue
        sql = path.read_text(encoding="utf-8")
        cursor = con.execute(sql)
        head = parse_header(sql)
        results.append(
            Result(path.stem, title_of(path), head.get("question", ""), head.get("technique", ""), head.get("reading", ""), sql,
                   [d[0] for d in cursor.description], cursor.fetchall())  # fmt: skip
        )
    return results


def cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def to_text(result: Result, width: int = 34) -> str:
    rows = [[cell(v)[:width] for v in row] for row in result.rows]
    sizes = [max(len(c), *(len(r[i]) for r in rows)) if rows else len(c) for i, c in enumerate(result.columns)]
    line = lambda cells: "  ".join(c.ljust(sizes[i]) for i, c in enumerate(cells)).rstrip()  # noqa: E731
    return "\n".join([f"{result.name}: {result.question}", line(result.columns), line(["-" * s for s in sizes]), *map(line, rows)])


def to_markdown_table(result: Result) -> str:
    head = "| " + " | ".join(result.columns) + " |"
    rule = "|" + "|".join("---" for _ in result.columns) + "|"
    body = ["| " + " | ".join(cell(v).replace("|", "/") for v in row) + " |" for row in result.rows]
    return "\n".join([head, rule, *body])


def to_markdown(results: list[Result], intro: str) -> str:
    parts = [intro.strip(), ""]
    for r in results:
        number = r.name.split("_", 1)[0]
        parts += [f"## {number}. {r.title}", "", f"**Question.** {r.question}", "", to_markdown_table(r), "",
                  f"**How to read it.** {r.reading}", "", f"**Technique.** {r.technique}", "",
                  f"<details><summary>The SQL ({r.name}.sql)</summary>\n\n```sql\n{r.sql.strip()}\n```\n\n</details>", ""]  # fmt: skip
    return "\n".join(parts).rstrip() + "\n"
