from __future__ import annotations

import unittest
import urllib.error
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from lcsc_altium_loader.client import (
    EASYEDA_COMPONENT_URL,
    LCSC_CATEGORY_URL,
    LCSC_CHINA_SEARCH_URL,
    LCSC_DETAIL_URL,
    LCSC_SEARCH_URL,
    ClientError,
    LCSCClient,
)


class StubClient(LCSCClient):
    def __init__(self, responses: dict[str, dict]) -> None:
        super().__init__(retries=0, cache_dir=False)
        self.responses = responses
        self.calls: list[tuple[str, dict | None]] = []

    def _json(self, url: str, *, payload: dict | None = None) -> dict:
        self.calls.append((url, payload))
        if url.startswith(LCSC_DETAIL_URL + "?"):
            key = LCSC_DETAIL_URL
        elif url.startswith(LCSC_CHINA_SEARCH_URL + "?"):
            key = LCSC_CHINA_SEARCH_URL
        else:
            key = url
        if key == LCSC_CHINA_SEARCH_URL and key not in self.responses:
            return {"success": True, "result": {"total": 0, "productList": []}}
        return self.responses[key]


def product(code: str, *, product_id: int, stock: int, mpn: str = "PART") -> dict:
    return {
        "productCode": code,
        "productId": product_id,
        "productModel": mpn,
        "brandNameEn": "Maker",
        "encapStandard": "0603",
        "stockNumber": stock,
        "productPriceList": [{"currencyPrice": "0.25", "currencySymbol": "$"}],
        "pdfUrl": "https://example.test/data.pdf",
        "productIntroEn": "example",
    }


def china_product(code: str, *, product_id: int, stock: int, mpn: str = "PART") -> dict:
    return {
        "code": code,
        "id": product_id,
        "model": mpn,
        "brandName": "MORNSUN(金升阳)",
        "standard": "SMD-9",
        "stockNumber": str(stock),
        "priceList": [{"startNumber": 1, "endNumber": 9, "price": 32.53}],
        "name": "单通道高速CAN隔离收发器",
        "device_info": {
            "description": "中国站元件",
            "attributes": {"Datasheet": "https://item.szlcsc.com/datasheet/example.html"},
        },
    }


class LCSCClientTests(unittest.TestCase):
    def test_china_mpn_search_returns_domestic_stock_price_and_exact_code(self) -> None:
        client = StubClient(
            {
                LCSC_CHINA_SEARCH_URL: {
                    "success": True,
                    "result": {
                        "total": 1,
                        "productList": [
                            china_product(
                                "C7527764", product_id=8623563, stock=2, mpn="TD331SCANH"
                            )
                        ],
                    },
                }
            }
        )

        result = client.search("TD331SCANH", in_stock=True)

        self.assertEqual([item.lcsc for item in result], ["C7527764"])
        self.assertTrue(result[0].exact)
        self.assertEqual(result[0].stock, "2")
        self.assertEqual(result[0].price, "32.53")
        self.assertEqual(result[0].currency, "CNY")
        self.assertEqual(result[0].price_source, "china")
        self.assertEqual(result[0].product_url, "https://item.szlcsc.com/8623563.html")
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(client.calls[0][0].startswith(LCSC_CHINA_SEARCH_URL + "?"))

    def test_china_c_code_search_supplies_detail_without_global_endpoint(self) -> None:
        client = StubClient(
            {
                LCSC_CHINA_SEARCH_URL: {
                    "success": True,
                    "result": {
                        "total": 1,
                        "productList": [
                            china_product(
                                "C7527764", product_id=8623563, stock=2, mpn="TD331SCANH"
                            )
                        ],
                    },
                }
            }
        )

        result = client.search("C7527764", in_stock=True)

        self.assertEqual([item.mpn for item in result], ["TD331SCANH"])
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(client.calls[0][0].startswith(LCSC_CHINA_SEARCH_URL + "?"))

    def test_china_out_of_stock_result_does_not_fall_through_to_global_search(self) -> None:
        client = StubClient(
            {
                LCSC_CHINA_SEARCH_URL: {
                    "success": True,
                    "result": {
                        "total": 1,
                        "productList": [
                            china_product("C9", product_id=9, stock=0, mpn="DOMESTIC-ONLY")
                        ],
                    },
                }
            }
        )

        self.assertEqual(client.search("DOMESTIC-ONLY", in_stock=True), [])
        self.assertEqual(len(client.calls), 1)

    def test_c_code_uses_detail_and_labels_global_price(self) -> None:
        client = StubClient({LCSC_DETAIL_URL: {"result": product("C8734", product_id=9243, stock=12)}})

        result = client.search("c8734")

        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].exact)
        self.assertEqual(result[0].lcsc, "C8734")
        self.assertEqual(result[0].currency, "USD")
        self.assertEqual(result[0].price_source, "global")
        self.assertEqual(result[0].product_url, "https://item.szlcsc.com/9243.html")

    def test_exact_mpn_does_not_fall_through_to_category_search(self) -> None:
        row = product("C8734", product_id=9243, stock=12, mpn="STM32F103C8T6")
        client = StubClient({LCSC_SEARCH_URL: {"result": {"exactMatchResult": [row]}}})

        result = client.search("STM32F103C8T6")

        self.assertEqual([item.lcsc for item in result], ["C8734"])
        self.assertTrue(client.calls[0][0].startswith(LCSC_CHINA_SEARCH_URL + "?"))
        self.assertEqual(client.calls[1][0], LCSC_SEARCH_URL)

    def test_broad_search_uses_leaf_catalog_and_filters_stock(self) -> None:
        sold_out = product("C1", product_id=1, stock=0)
        stocked = product("C2", product_id=2, stock=50)
        client = StubClient(
            {
                LCSC_SEARCH_URL: {
                    "result": {
                        "topResults": [
                            {"catalogId": 10, "childCatalogs": [{"catalogId": 11}]}
                        ]
                    }
                },
                LCSC_CATEGORY_URL: {"result": {"dataList": [sold_out, stocked, stocked]}},
            }
        )

        result = client.search("10k 0603", limit=5, in_stock=True)

        self.assertEqual([item.lcsc for item in result], ["C2"])
        category_payload = client.calls[-1][1]
        self.assertEqual(category_payload["catalogIdList"], [11])

    def test_request_retries_transient_http_error_once(self) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"{}"
        transient = urllib.error.HTTPError(
            "https://example.test", 429, "rate limited", {}, None
        )
        client = LCSCClient(retries=1, min_interval=0)

        with patch(
            "lcsc_altium_loader.client.urllib.request.urlopen",
            side_effect=[transient, response],
        ) as urlopen, patch("lcsc_altium_loader.client.time.sleep") as sleep, patch(
            "lcsc_altium_loader.client.random.uniform", return_value=0
        ):
            body = client._request("https://example.test")

        self.assertEqual(body, b"{}")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once()

    def test_json_rejects_malformed_and_non_object_payloads(self) -> None:
        client = LCSCClient(retries=0)
        with patch.object(client, "_request", return_value=b"not-json"):
            with self.assertRaisesRegex(ClientError, "invalid JSON"):
                client._json("https://example.test")
        with patch.object(client, "_request", return_value=b"[]"):
            with self.assertRaisesRegex(ClientError, "unexpected JSON"):
                client._json("https://example.test")

    def test_json_rejects_html_antibot_response_explicitly(self) -> None:
        client = LCSCClient(retries=0)
        with patch.object(client, "_request", return_value=b"<!doctype html><title>blocked</title>"):
            with self.assertRaisesRegex(ClientError, "anti-bot"):
                client._json("https://example.test")

    def test_detail_rejects_empty_result(self) -> None:
        client = StubClient({LCSC_DETAIL_URL: {"result": {}}})

        with self.assertRaisesRegex(ClientError, "detail not found"):
            client.get_detail("C1")

    def test_detail_rejects_mismatched_code(self) -> None:
        client = StubClient(
            {LCSC_DETAIL_URL: {"result": product("C2", product_id=2, stock=1)}}
        )

        with self.assertRaisesRegex(ClientError, "code mismatch"):
            client.get_detail("C1")

    def test_component_rejects_missing_datastr(self) -> None:
        client = StubClient(
            {EASYEDA_COMPONENT_URL.format(code="C1"): {"result": {"uuid": "x"}}}
        )

        with self.assertRaisesRegex(ClientError, "missing dataStr"):
            client.get_component_data("C1")

    def test_component_rejects_mismatched_lcsc_code(self) -> None:
        client = StubClient(
            {
                EASYEDA_COMPONENT_URL.format(code="C1"): {
                    "result": {"dataStr": {"head": {}}, "lcsc": {"number": "C2"}}
                }
            }
        )

        with self.assertRaisesRegex(ClientError, "code mismatch"):
            client.get_component_data("C1")

    def test_component_cache_is_atomic_reused_and_reported(self) -> None:
        response = {
            "result": {
                "dataStr": {"head": {"c_para": {}}},
                "lcsc": {"number": "C1"},
                "uuid": "fixture",
            }
        }
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary)
            client = LCSCClient(
                retries=0,
                easyeda_min_interval=0,
                cache_dir=cache,
            )
            with patch.object(client, "_json", return_value=response) as request:
                first, first_meta = client.get_component_data_with_metadata("C1")
            second_client = LCSCClient(
                retries=0,
                easyeda_min_interval=0,
                cache_dir=cache,
            )
            with patch.object(
                second_client, "_json", side_effect=AssertionError("network used")
            ):
                second, second_meta = second_client.get_component_data_with_metadata("c1")

            self.assertEqual(first, second)
            self.assertEqual(first_meta["transport"], "network")
            self.assertEqual(second_meta["transport"], "cache")
            self.assertEqual(request.call_count, 1)
            self.assertTrue((cache / "C1.json").is_file())
            self.assertEqual(list(cache.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
