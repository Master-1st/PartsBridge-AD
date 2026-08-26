"""Small, stable data objects used by the CLI and the network boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Candidate:
    query: str
    lcsc: str = ""
    mpn: str = ""
    manufacturer: str = ""
    package: str = ""
    stock: int | float | str = ""
    price: str = ""
    currency: str = ""
    price_source: str = "global"
    product_url: str = ""
    global_product_url: str = ""
    datasheet_url: str = ""
    description: str = ""
    exact: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "lcsc": self.lcsc,
            "mpn": self.mpn,
            "manufacturer": self.manufacturer,
            "package": self.package,
            "stock": self.stock,
            "price": self.price,
            "currency": self.currency,
            "price_source": self.price_source,
            "product_url": self.product_url,
            "global_product_url": self.global_product_url,
            "datasheet_url": self.datasheet_url,
            "description": self.description,
            "exact": self.exact,
        }


@dataclass(slots=True)
class ComponentResult:
    """Result of converting one EasyEDA component."""

    code: str
    component: dict[str, Any]
    symbol_name: str
    footprint_name: str
    symbol_counts: dict[str, int]
    footprint_counts: dict[str, int]
    warnings: list[str]
    raw_symbol_counts: dict[str, int]
    raw_footprint_counts: dict[str, int]
    three_d: dict[str, Any]
