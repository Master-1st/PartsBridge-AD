"""Shared batch workflows used by both the CLI and desktop application."""

from __future__ import annotations

import csv
import io
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .client import ClientError, LCSCClient
from .models import Candidate

QUERY_HEADERS = {
    "query",
    "mpn",
    "manufacturer_part_number",
    "lcsc",
    "product_code",
    "code",
}
CODE_HEADERS = {"lcsc", "product_code", "code"}
RESOLVE_COLUMNS = [
    "query",
    "rank",
    "exact",
    "lcsc",
    "mpn",
    "manufacturer",
    "package",
    "stock",
    "price",
    "currency",
    "price_source",
    "product_url",
    "global_product_url",
    "datasheet_url",
    "description",
    "error",
]


class WorkflowCancelled(RuntimeError):
    """The user requested cancellation between two network/conversion items."""


@dataclass(slots=True)
class ResolveResult:
    rows: list[dict[str, Any]]
    query_count: int
    candidate_count: int
    no_match_count: int
    error_count: int


ProgressCallback = Callable[[int, int, str, str], None]


def read_values(path: Path, accepted_headers: set[str]) -> list[str]:
    payload = Path(path).read_bytes()
    text: str | None = None
    failures: list[str] = []
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = payload.decode(encoding)
            break
        except UnicodeDecodeError as exc:
            failures.append(f"{encoding}: {exc}")
    if text is None:
        raise ValueError(f"unsupported CSV encoding: {path}: {'; '.join(failures)}")
    rows = list(csv.reader(io.StringIO(text, newline="")))
    if not rows:
        raise ValueError(f"empty CSV: {path}")
    header = [cell.strip().lower() for cell in rows[0]]
    matching = [index for index, cell in enumerate(header) if cell in accepted_headers]
    if matching:
        index = matching[0]
        rows = rows[1:]
    else:
        index = 0
    output: list[str] = []
    for row in rows:
        if len(row) > index and row[index].strip():
            output.append(row[index].strip())
    return output


def read_queries(path: Path) -> list[str]:
    return read_values(path, QUERY_HEADERS)


def read_codes(path: Path) -> list[str]:
    return read_values(path, CODE_HEADERS)


def candidate_row(query: str, rank: int, candidate: Candidate) -> dict[str, Any]:
    value = candidate.to_dict()
    value.update(
        {
            "query": query,
            "rank": rank,
            "exact": str(bool(candidate.exact)).lower(),
            "error": "",
        }
    )
    return value


def resolve_queries(
    client: LCSCClient,
    queries: Iterable[str],
    *,
    limit: int = 5,
    in_stock: bool = False,
    progress: ProgressCallback | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> ResolveResult:
    values: list[str] = []
    seen: set[str] = set()
    for query in queries:
        value = str(query).strip()
        key = value.casefold()
        if not value or key in seen:
            continue
        seen.add(key)
        values.append(value)
    rows: list[dict[str, Any]] = []
    no_match_count = 0
    error_count = 0
    candidate_count = 0
    total = len(values)
    for index, query in enumerate(values, 1):
        if cancelled is not None and cancelled():
            raise WorkflowCancelled("candidate resolution cancelled")
        try:
            candidates = client.search(query, limit=limit, in_stock=in_stock)
            if candidates:
                for rank, candidate in enumerate(candidates, 1):
                    rows.append(candidate_row(query, rank, candidate))
                candidate_count += len(candidates)
                status = f"{len(candidates)} candidates"
            else:
                rows.append({"query": query, "error": "no candidates"})
                no_match_count += 1
                status = "no candidates"
        except ClientError as exc:
            rows.append({"query": query, "error": str(exc)})
            error_count += 1
            status = "error"
        if progress is not None:
            progress(index, total, query, status)
    return ResolveResult(
        rows=rows,
        query_count=total,
        candidate_count=candidate_count,
        no_match_count=no_match_count,
        error_count=error_count,
    )


def write_candidate_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESOLVE_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


__all__ = [
    "CODE_HEADERS",
    "QUERY_HEADERS",
    "RESOLVE_COLUMNS",
    "ResolveResult",
    "WorkflowCancelled",
    "candidate_row",
    "read_codes",
    "read_queries",
    "read_values",
    "resolve_queries",
    "write_candidate_csv",
]
