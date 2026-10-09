"""Discover energy brands from the CDR Register."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

from tariff_catalogue.harvest.common.http import PoliteClient
from tariff_catalogue.harvest.common.report import RunReport

BRANDS_URL = "https://api.cdr.gov.au/cdr-register/v1/energy/data-holders/brands/summary"


@dataclass(frozen=True, slots=True)
class Brand:
    brand_id: str
    brand_name: str
    product_base_uri: str
    status: str
    product_base_uri_field: str = "productBaseUri"


class CDRResponseError(ValueError):
    """An invalid or unsuccessful response from a CDR API."""


def _api_errors(payload: Any) -> Any:
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if errors:
            return errors
    return None


def _summaries(payload: Any) -> list[dict[str, Any]]:
    errors = _api_errors(payload)
    if errors:
        raise CDRResponseError(f"CDR Register returned errors: {errors}")
    try:
        summaries = payload["data"]["brandSummaries"]
    except (KeyError, TypeError) as error:
        raise CDRResponseError("CDR Register response has no data.brandSummaries") from error
    if not isinstance(summaries, list) or any(not isinstance(item, dict) for item in summaries):
        raise CDRResponseError("CDR Register data.brandSummaries must be a list of objects")
    return summaries


def discover_brands(
    client: PoliteClient, report: RunReport | None = None
) -> list[Brand]:
    """Return active brands, following CDR Register pagination links."""
    brands: list[Brand] = []
    next_url: str | None = BRANDS_URL
    seen_urls: set[str] = set()
    while next_url is not None:
        if next_url in seen_urls:
            raise CDRResponseError(f"CDR Register pagination loop at {next_url}")
        seen_urls.add(next_url)
        payload, _meta = client.get_json(next_url, headers={"x-v": "1"})
        for summary in _summaries(payload):
            brand_id = summary.get("dataHolderBrandId") or summary.get("brandId")
            brand_name = summary.get("brandName")
            status = str(
                summary.get("dataHolderBrandStatus") or summary.get("status") or ""
            ).strip()
            base_uri_field = (
                "productBaseUri" if summary.get("productBaseUri") else "publicBaseUri"
            )
            base_uri = summary.get(base_uri_field)
            if not isinstance(brand_id, str) or not brand_id:
                raise CDRResponseError("CDR Register brand is missing its ID or name")
            if not isinstance(brand_name, str) or not brand_name:
                raise CDRResponseError("CDR Register brand is missing its ID or name")
            if not isinstance(base_uri, str) or not base_uri:
                raise CDRResponseError(f"CDR Register brand {brand_id} has no product base URI")
            brand = Brand(
                brand_id=brand_id,
                brand_name=brand_name,
                product_base_uri=base_uri.rstrip("/"),
                status=status,
                product_base_uri_field=base_uri_field,
            )
            if brand.status.casefold() == "active":
                brands.append(brand)
            elif report is not None:
                report.record_warning(
                    f"Skipping inactive CDR brand {brand.brand_id} "
                    f"({brand.brand_name}; status: {brand.status or 'unknown'})"
                )

        links = payload.get("links", {}) if isinstance(payload, dict) else {}
        next_link = links.get("next") if isinstance(links, dict) else None
        next_url = (
            urljoin(next_url, next_link)
            if isinstance(next_link, str) and next_link
            else None
        )
    return brands
