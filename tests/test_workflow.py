from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from lcsc_altium_loader.client import ClientError
from lcsc_altium_loader.models import Candidate
from lcsc_altium_loader.workflow import (
    WorkflowCancelled,
    read_codes,
    resolve_queries,
    write_candidate_csv,
)


class WorkflowClient:
    def search(self, query: str, *, limit: int, in_stock: bool) -> list[Candidate]:
        del limit, in_stock
        if query == "bad":
            raise ClientError("network failed")
        if query == "none":
            return []
        return [
            Candidate(
                query=query,
                lcsc="C1",
                mpn="PART",
                stock=10,
                price="0.1",
                currency="USD",
                product_url="https://item.szlcsc.com/1.html",
                exact=True,
            )
        ]


class WorkflowTests(unittest.TestCase):
    def test_resolve_keeps_candidates_no_matches_and_errors_auditable(self) -> None:
        progress: list[tuple[int, int, str, str]] = []

        result = resolve_queries(
            WorkflowClient(),
            ["part", "none", "bad"],
            progress=lambda *value: progress.append(value),
        )

        self.assertEqual(result.query_count, 3)
        self.assertEqual(result.candidate_count, 1)
        self.assertEqual(result.no_match_count, 1)
        self.assertEqual(result.error_count, 1)
        self.assertEqual([row.get("error") for row in result.rows], ["", "no candidates", "network failed"])
        self.assertEqual([value[0] for value in progress], [1, 2, 3])

    def test_resolve_cancellation_happens_between_queries(self) -> None:
        calls = 0

        def cancelled() -> bool:
            nonlocal calls
            calls += 1
            return calls > 1

        with self.assertRaises(WorkflowCancelled):
            resolve_queries(WorkflowClient(), ["one", "two"], cancelled=cancelled)

    def test_candidate_csv_is_utf8_bom_and_contains_source_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "candidates.csv"
            write_candidate_csv(
                path,
                [
                    {
                        "query": "part",
                        "rank": 1,
                        "lcsc": "C1",
                        "price_source": "global",
                        "global_product_url": "https://example.test/global",
                    }
                ],
            )
            payload = path.read_bytes()
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))

        self.assertTrue(payload.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(row["price_source"], "global")
        self.assertEqual(row["global_product_url"], "https://example.test/global")

    def test_read_codes_prefers_explicit_code_column(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "codes.csv"
            path.write_text("mpn,code\nignored,C25804\n", encoding="utf-8")

            values = read_codes(path)

        self.assertEqual(values, ["C25804"])

    def test_read_codes_accepts_excel_style_gb18030_csv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "中文清单.csv"
            path.write_bytes("code,备注\nC25804,电阻\n".encode("gb18030"))

            values = read_codes(path)

        self.assertEqual(values, ["C25804"])

    def test_large_resolve_batch_deduplicates_case_insensitively(self) -> None:
        queries = ["PART", "part"] * 500

        result = resolve_queries(WorkflowClient(), queries)

        self.assertEqual(result.query_count, 1)
        self.assertEqual(result.candidate_count, 1)


if __name__ == "__main__":
    unittest.main()
