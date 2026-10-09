"""Fetch Australian CDR plan details."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from tariff_catalogue.harvest.au_cdr.brands import Brand, CDRResponseError
from tariff_catalogue.harvest.common.http import HarvestHTTPError, PoliteClient

PLANS_PATH = "/cds-au/v1/energy/plans"


def _is_version_error(payload: Any) -> bool:
    if not isinstance(payload, dict) or not payload.get("errors"):
        return False
    text = json.dumps(payload["errors"], ensure_ascii=False).casefold()
    return "version" in text and any(
        marker in text for marker in ("invalid", "unsupported", "missing", "required")
    )


def fetch_detail(
    client: PoliteClient, brand: Brand, plan_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch plan details, falling back to older CDR API versions when needed."""
    url = f"{brand.product_base_uri}{PLANS_PATH}/{quote(plan_id, safe='@')}"
    for version in ("3", "2", "1"):
        try:
            payload, metadata = client.get_json(url, headers={"x-v": version})
        except HarvestHTTPError as error:
            if error.status == 406 and version != "1":
                continue
            raise
        if _is_version_error(payload):
            if version != "1":
                continue
            raise CDRResponseError(f"CDR detail API rejected x-v: {payload['errors']}")
        if not isinstance(payload, dict):
            raise CDRResponseError("CDR detail response must be an object")
        if payload.get("errors"):
            raise CDRResponseError(f"CDR detail API returned errors: {payload['errors']}")
        return payload, metadata
    raise CDRResponseError(f"CDR detail API rejected all supported versions for {plan_id}")
