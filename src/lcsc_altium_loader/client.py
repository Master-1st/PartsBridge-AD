"""Public LCSC/EasyEDA HTTP boundary.

This module deliberately does not use browser sessions, cookies, HTML pages or
accounts.  Keeping all network I/O here makes the conversion layer deterministic
and makes a future API change local to one file.
"""

from __future__ import annotations

import json
import hashlib
import hmac
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import Candidate

LCSC_SEARCH_URL = "https://wmsc.lcsc.com/ftps/wm/search/v3/global"
LCSC_CATEGORY_URL = "https://wmsc.lcsc.com/ftps/wm/product/query/list"
LCSC_DETAIL_URL = "https://wmsc.lcsc.com/ftps/wm/product/detail"
LCSC_CHINA_SEARCH_URL = "https://pro.lceda.cn/api/eda/product/search"
EASYEDA_COMPONENT_URL = "https://easyeda.com/api/products/{code}/components?version=6.5.37"
EASYEDA_STEP_URL = "https://modules.easyeda.com/qAxj6KHrDKw4blvCG8QJPs7Y/{uuid}"
_CODE_RE = re.compile(r"^C\d+$", re.IGNORECASE)


class ClientError(RuntimeError):
    """A public endpoint request or response failed."""


class LCSCClient:
    """Small public-API client with bounded retries and no authentication."""

    def __init__(
        self,
        *,
        timeout: float = 25.0,
        retries: int = 2,
        min_interval: float = 0.15,
        easyeda_min_interval: float = 3.2,
        user_agent: str = "partsbridge-ad/0.3.8",
        cache_dir: str | Path | bool | None = None,
        component_cache_seconds: float = 7 * 24 * 60 * 60,
    ) -> None:
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.min_interval = max(0.0, float(min_interval))
        self.easyeda_min_interval = max(self.min_interval, float(easyeda_min_interval))
        self.user_agent = user_agent
        if cache_dir is False:
            self.cache_dir: Path | None = None
        elif cache_dir is None:
            local_data = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".cache"))
            self.cache_dir = local_data / "PartsBridge-AD" / "component-cache"
        else:
            self.cache_dir = Path(cache_dir)
        self.component_cache_seconds = max(0.0, float(component_cache_seconds))
        self._next_request_at: dict[str, float] = {}
        self._rate_lock = threading.Lock()

    def _wait_for_rate_limit(self, url: str) -> None:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        interval = self.easyeda_min_interval if host.endswith("easyeda.com") else self.min_interval
        if interval <= 0:
            return
        with self._rate_lock:
            now = time.monotonic()
            delay = self._next_request_at.get(host, 0.0) - now
            if delay > 0:
                time.sleep(delay)
                now = time.monotonic()
            self._next_request_at[host] = now + interval

    def _request(self, url: str, *, data: bytes | None = None, accept: str = "application/json") -> bytes:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": accept,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            self._wait_for_rate_limit(url)
            request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code not in {408, 425, 429} and not 500 <= exc.code <= 599:
                    break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = exc
            if attempt < self.retries:
                retry_after = 0.0
                if isinstance(last, urllib.error.HTTPError) and last.headers is not None:
                    try:
                        retry_after = min(15.0, max(0.0, float(last.headers.get("Retry-After", 0))))
                    except (TypeError, ValueError):
                        retry_after = 0.0
                backoff = min(8.0, 0.4 * (2**attempt)) + random.uniform(0.0, 0.15)
                time.sleep(max(retry_after, backoff))
        if isinstance(last, urllib.error.HTTPError):
            detail = f"HTTP {last.code}"
        else:
            detail = type(last).__name__ if last is not None else "unknown error"
        raise ClientError(f"request failed: {url}: {detail}")

    def _json(self, url: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = self._request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8")) if payload is not None else self._request(url)
        stripped = body.lstrip()
        if not stripped:
            raise ClientError(f"empty response from {url}")
        if stripped.startswith((b"<", b"<!")):
            raise ClientError(f"non-JSON HTML response from {url}; endpoint or anti-bot state changed")
        try:
            value = json.loads(body.decode("utf-8", errors="strict"))
        except UnicodeDecodeError as exc:
            raise ClientError(f"non-UTF-8 response from {url}") from exc
        except json.JSONDecodeError as exc:
            raise ClientError(f"invalid JSON response from {url}: {exc}") from exc
        if not isinstance(value, dict):
            raise ClientError(f"unexpected JSON response from {url}")
        return value

    def _bytes(self, url: str) -> bytes:
        return self._request(url, accept="application/octet-stream,application/step")

    @staticmethod
    def _price(value: dict[str, Any]) -> tuple[str, str]:
        prices = value.get("productPriceList") or []
        first = prices[0] if prices and isinstance(prices[0], dict) else {}
        amount = first.get("currencyPrice", first.get("productPrice", first.get("usdPrice", "")))
        currency = first.get("currencySymbol") or value.get("currencySymbol") or ""
        currency = {"$": "USD", "￥": "CNY", "¥": "CNY"}.get(str(currency), str(currency))
        return ("" if amount is None else str(amount), str(currency))

    @staticmethod
    def _domestic_url(value: dict[str, Any], code: str) -> str:
        product_id = value.get("productId")
        if product_id not in (None, ""):
            return f"https://item.szlcsc.com/{product_id}.html"
        return f"https://so.szlcsc.com/global.html?k={urllib.parse.quote(code)}"

    @staticmethod
    def _china_detail(value: dict[str, Any]) -> dict[str, Any]:
        """Normalize one LCSC China/EasyEDA Pro search row to the detail schema."""
        device = value.get("device_info") or {}
        if not isinstance(device, dict):
            device = {}
        attributes = device.get("attributes") or {}
        if not isinstance(attributes, dict):
            attributes = {}
        prices = []
        for price in value.get("priceList") or []:
            if not isinstance(price, dict) or price.get("price") in (None, ""):
                continue
            prices.append(
                {
                    "currencyPrice": str(price["price"]),
                    "currencySymbol": "￥",
                    "startPurchasedNumber": price.get("startNumber"),
                }
            )
        pdf_url = str(attributes.get("Datasheet") or value.get("pdfFileUrl") or "")
        if pdf_url.startswith("/"):
            pdf_url = urllib.parse.urljoin("https://pro.lceda.cn", pdf_url)
        return {
            "productCode": str(value.get("code") or attributes.get("Supplier Part") or ""),
            "productId": value.get("id"),
            "productModel": str(value.get("model") or attributes.get("Manufacturer Part") or ""),
            "brandNameEn": str(value.get("brandName") or attributes.get("Manufacturer") or ""),
            "encapStandard": str(value.get("standard") or attributes.get("Supplier Footprint") or ""),
            "stockNumber": value.get("stockNumber", ""),
            "productPriceList": prices,
            "currencySymbol": "￥",
            "pdfUrl": pdf_url,
            "productIntroEn": str(
                value.get("desc") or device.get("description") or value.get("name") or ""
            ),
            "_partsbridge_source": "lcsc_china",
        }

    @classmethod
    def _candidate(cls, value: dict[str, Any], query: str, *, exact: bool = False) -> Candidate:
        code = str(value.get("productCode") or value.get("code") or "")
        price, currency = cls._price(value)
        stock = value.get("stockNumber", value.get("stockSz", ""))
        if stock is None:
            stock = ""
        global_url = str(value.get("url") or (f"https://www.lcsc.com/product-detail/{code}.html" if code else ""))
        return Candidate(
            query=query,
            lcsc=code,
            mpn=str(value.get("productModel") or ""),
            manufacturer=str(value.get("brandNameEn") or ""),
            package=str(value.get("encapStandard") or ""),
            stock=stock,
            price=price,
            currency=currency,
            price_source=(
                "china" if value.get("_partsbridge_source") == "lcsc_china" else "global"
            ),
            product_url=cls._domestic_url(value, code),
            global_product_url=global_url,
            datasheet_url=str(value.get("pdfUrl") or ""),
            description=str(value.get("productIntroEn") or value.get("productDescEn") or value.get("productNameEn") or ""),
            exact=exact,
            raw=value,
        )

    @staticmethod
    def _in_stock(item: Candidate) -> bool:
        try:
            return float(item.stock) > 0
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _catalog_ids(value: Any) -> list[int]:
        """Take leaf category IDs first, preserving the server's relevance order."""
        ids: list[int] = []

        def walk(node: Any) -> None:
            if isinstance(node, list):
                for item in node:
                    walk(item)
                return
            if not isinstance(node, dict):
                return
            children = node.get("childCatalogs") or node.get("childCatelogs") or []
            if children:
                walk(children)
            else:
                try:
                    catalog_id = int(node.get("catalogId"))
                except (TypeError, ValueError):
                    return
                if catalog_id not in ids:
                    ids.append(catalog_id)

        walk(value)
        return ids

    def _category_search(self, query: str, catalog_ids: Iterable[int], limit: int) -> list[Candidate]:
        output: list[Candidate] = []
        for catalog_id in list(catalog_ids)[:8]:
            payload = {
                "keyword": "",
                "globalKeyword": query,
                "scene": "FULL_MATCH",
                "catalogIdList": [catalog_id],
                "brandIdList": [],
                "encapValueList": [],
                "isStock": False,
                "isOtherSuppliers": False,
                "isAsianBrand": False,
                "isDeals": False,
                "isRohsCert": False,
                "paramNameValueMap": {},
                "currentPage": 1,
                "pageSize": max(limit, 1),
            }
            data = self._json(LCSC_CATEGORY_URL, payload=payload)
            result = data.get("result") or {}
            if not isinstance(result, dict):
                raise ClientError("unexpected LCSC category result schema")
            rows = result.get("dataList") or []
            if not isinstance(rows, list):
                raise ClientError("unexpected LCSC category dataList schema")
            output.extend(self._candidate(item, query) for item in rows if isinstance(item, dict))
            if len(output) >= limit:
                break
        deduped: list[Candidate] = []
        seen: set[str] = set()
        for item in output:
            key = item.lcsc or item.mpn
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped[:limit]

    def _china_search(self, query: str, limit: int) -> list[Candidate]:
        data = self._json(
            LCSC_CHINA_SEARCH_URL
            + "?"
            + urllib.parse.urlencode(
                {"keyword": query, "type": 3, "page": 1, "pageSize": max(1, limit)}
            )
        )
        if data.get("success") is False:
            raise ClientError("LCSC China search reported failure")
        result = data.get("result") or {}
        if not isinstance(result, dict):
            raise ClientError("unexpected LCSC China search schema")
        products = result.get("productList") or []
        if not isinstance(products, list):
            raise ClientError("unexpected LCSC China productList schema")
        normalized_query = query.casefold()
        rows: list[Candidate] = []
        seen: set[str] = set()
        for value in products:
            if not isinstance(value, dict):
                continue
            detail = self._china_detail(value)
            code = str(detail.get("productCode") or "")
            model = str(detail.get("productModel") or "")
            key = code.casefold() or model.casefold()
            if not key or key in seen:
                continue
            seen.add(key)
            rows.append(
                self._candidate(
                    detail,
                    query,
                    exact=normalized_query in {code.casefold(), model.casefold()},
                )
            )
        rows.sort(key=lambda item: not item.exact)
        return rows[:limit]

    def search(self, query: str, *, limit: int = 10, in_stock: bool = False) -> list[Candidate]:
        query = query.strip()
        if not query:
            raise ClientError("query must not be empty")
        limit = max(1, min(int(limit), 200))
        if _CODE_RE.fullmatch(query):
            detail = self.get_detail(query)
            item = self._candidate(detail, query, exact=True)
            return [item] if (not in_stock or self._in_stock(item)) else []

        try:
            china_rows = self._china_search(query, limit)
        except ClientError:
            china_rows = []
        if china_rows:
            return [item for item in china_rows if not in_stock or self._in_stock(item)][:limit]

        data = self._json(LCSC_SEARCH_URL, payload={"keyword": query})
        result = data.get("result") or {}
        if not isinstance(result, dict):
            raise ClientError("unexpected LCSC search result schema")
        exact = result.get("exactMatchResult") or []
        if not isinstance(exact, list):
            raise ClientError("unexpected LCSC exact-match schema")
        rows = [self._candidate(item, query, exact=True) for item in exact if isinstance(item, dict)]
        rows = [item for item in rows if not in_stock or self._in_stock(item)]
        if rows:
            return rows[:limit]

        ids = self._catalog_ids(result.get("topResults"))
        if not ids:
            ids = self._catalog_ids(result.get("catalogVOS"))
        rows = self._category_search(query, ids, limit)
        return [item for item in rows if not in_stock or self._in_stock(item)][:limit]

    def get_detail(self, code: str) -> dict[str, Any]:
        code = code.strip().upper()
        if not _CODE_RE.fullmatch(code):
            raise ClientError(f"invalid LCSC code: {code}")
        try:
            china_rows = self._china_search(code, 20)
        except ClientError:
            china_rows = []
        for item in china_rows:
            if item.lcsc.upper() == code and isinstance(item.raw, dict):
                return item.raw
        data = self._json(LCSC_DETAIL_URL + "?" + urllib.parse.urlencode({"productCode": code}))
        result = data.get("result")
        if not isinstance(result, dict) or not result:
            raise ClientError(f"LCSC detail not found: {code}")
        returned_code = str(result.get("productCode") or "").upper()
        if returned_code and returned_code != code:
            raise ClientError(f"LCSC detail code mismatch: requested {code}, received {returned_code}")
        return result

    def get_component_data(self, code: str) -> dict[str, Any]:
        data, _ = self.get_component_data_with_metadata(code)
        return data

    @staticmethod
    def _validate_component_data(code: str, result: Any) -> dict[str, Any]:
        if not isinstance(result, dict) or not result:
            raise ClientError(f"EasyEDA component data not found: {code}")
        if not isinstance(result.get("dataStr"), dict) or not result.get("dataStr"):
            raise ClientError(f"EasyEDA component schema missing dataStr: {code}")
        lcsc = result.get("lcsc") or {}
        if isinstance(lcsc, dict):
            returned_code = str(lcsc.get("number") or "").upper()
            if returned_code and returned_code != code:
                raise ClientError(
                    f"EasyEDA component code mismatch: requested {code}, received {returned_code}"
                )
        return result

    def _component_cache_path(self, code: str) -> Path | None:
        return None if self.cache_dir is None else self.cache_dir / f"{code}.json"

    def _read_component_cache(self, code: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        path = self._component_cache_path(code)
        if path is None or self.component_cache_seconds <= 0:
            return None
        try:
            wrapper = json.loads(path.read_text(encoding="utf-8"))
            fetched_at = float(wrapper["fetched_at"])
            age = max(0.0, time.time() - fetched_at)
            if wrapper.get("schema_version") != 2 or age > self.component_cache_seconds:
                return None
            payload = self._validate_component_data(code, wrapper.get("payload"))
            canonical = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            if not hmac.compare_digest(
                hashlib.sha256(canonical).hexdigest(),
                str(wrapper.get("payload_sha256") or ""),
            ):
                return None
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError, ClientError):
            return None
        return payload, {
            "transport": "cache",
            "fetched_at": datetime.fromtimestamp(fetched_at, timezone.utc).isoformat().replace("+00:00", "Z"),
            "cache_age_seconds": round(age, 3),
        }

    def _write_component_cache(self, code: str, result: dict[str, Any], fetched_at: float) -> None:
        path = self._component_cache_path(code)
        if path is None or self.component_cache_seconds <= 0:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            canonical = json.dumps(
                result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            temporary.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "fetched_at": fetched_at,
                        "payload_sha256": hashlib.sha256(canonical).hexdigest(),
                        "payload": result,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def get_component_data_with_metadata(self, code: str) -> tuple[dict[str, Any], dict[str, Any]]:
        code = code.strip().upper()
        if not _CODE_RE.fullmatch(code):
            raise ClientError(f"invalid LCSC code: {code}")
        cached = self._read_component_cache(code)
        if cached is not None:
            return cached
        data = self._json(EASYEDA_COMPONENT_URL.format(code=code))
        result = self._validate_component_data(code, data.get("result"))
        fetched_at = time.time()
        self._write_component_cache(code, result, fetched_at)
        return result, {
            "transport": "network",
            "fetched_at": datetime.fromtimestamp(fetched_at, timezone.utc).isoformat().replace("+00:00", "Z"),
            "cache_age_seconds": 0.0,
        }

    def get_step_model(self, uuid: str) -> bytes:
        uuid = uuid.strip()
        if not uuid or not re.fullmatch(r"[0-9a-fA-F-]{8,64}", uuid):
            raise ClientError("invalid EasyEDA 3D model UUID")
        return self._bytes(EASYEDA_STEP_URL.format(uuid=uuid))


__all__ = ["ClientError", "EASYEDA_COMPONENT_URL", "LCSCClient"]
