"""External endpoint client (replaces the former Playwright/Computer Use flow).

The structure is ready: once the endpoint contract is known, only the response
mapping inside `sync()` needs to be implemented.
Configured via .env: EXTERNAL_API_URL and EXTERNAL_API_KEY.
"""
from __future__ import annotations

import time

import httpx
from sqlalchemy.orm import Session

from ..config import EXTERNAL_API_AUTH_SCHEME, EXTERNAL_API_KEY, EXTERNAL_API_URL


class ExternalAPIError(RuntimeError):
    pass


# settings value -> Authorization keyword
AUTH_SCHEMES = {"bearer": "Bearer", "api-key": "Api-Key"}


def auth_headers(token: str, scheme: str = "bearer") -> dict:
    """The Authorization header for an external endpoint, or {} without token.

    "bearer" sends `Bearer <token>`; "api-key" sends `Api-Key <key>`, the
    convention of djangorestframework-api-key deployments.
    """
    if not token:
        return {}
    keyword = AUTH_SCHEMES.get((scheme or "bearer").strip().lower(), "Bearer")
    return {"Authorization": f"{keyword} {token}"}


def is_configured() -> bool:
    return bool(EXTERNAL_API_URL)


def status() -> dict:
    """Integration status, for the panel or health checks."""
    return {
        "configured": is_configured(),
        "url": EXTERNAL_API_URL,
        "has_api_key": bool(EXTERNAL_API_KEY),
    }


def _client() -> httpx.Client:
    if not EXTERNAL_API_URL:
        raise ExternalAPIError("EXTERNAL_API_URL is not configured in fintech_back/.env.")
    headers = {"Accept": "application/json",
               **auth_headers(EXTERNAL_API_KEY or "", EXTERNAL_API_AUTH_SCHEME)}
    return httpx.Client(base_url=EXTERNAL_API_URL, headers=headers, timeout=30)


def probe() -> dict:
    """Basic ping (GET to the configured root) to inspect the contract."""
    with _client() as client:
        resp = client.get("")
        return {
            "status_code": resp.status_code,
            "ok": resp.is_success,
            "content_type": resp.headers.get("content-type"),
            # Short excerpt so the contract can be inspected without flooding the response.
            "preview": resp.text[:500],
        }


def fetch(path: str = "") -> object:
    """GET a path on the external endpoint and return the decoded JSON.

    Field mapping onto our models happens in agent/tools/external.py, which is
    configuration-driven so a contract change needs no code change here.
    """
    with _client() as client:
        response = client.get(path or "")
        response.raise_for_status()
        try:
            return response.json()
        except ValueError:
            raise ExternalAPIError(
                f"El endpoint respondió {response.headers.get('content-type')} en vez de JSON."
            )


def inspect(base_url: str, token: str, targets: list[tuple[str, str]],
            scheme: str = "bearer") -> dict:
    """Call the endpoint and describe what it returns.

    Nothing is mapped or stored: this is the look before the mapping. For each
    (name, path) target it reports the status, how long it took and the shape
    of the payload — the fields of the first record — so its columns can be
    matched against ours.
    """
    headers = {"Accept": "application/json", **auth_headers(token, scheme)}

    report = {"base_url": base_url, "paths": {}}
    with httpx.Client(base_url=base_url, headers=headers, timeout=30) as client:
        for name, path in [("root", "")] + list(targets):
            if name != "root" and not path:
                continue
            started = time.perf_counter()
            try:
                response = client.get(path or "")
            except httpx.HTTPError as err:
                report["paths"][name] = {"path": path or "/", "ok": False, "problem": str(err)}
                continue

            entry = {
                "path": path or "/",
                "status_code": response.status_code,
                "ok": response.is_success,
                "ms": round((time.perf_counter() - started) * 1000, 1),
                "content_type": response.headers.get("content-type", ""),
            }
            try:
                entry.update(_describe(response.json()))
            except ValueError:
                entry["preview"] = response.text[:400]
            report["paths"][name] = entry
    return report


def _describe(payload) -> dict:
    """Shape of a JSON payload: how many records and which fields they have."""
    records, wrapper = payload, None
    if isinstance(payload, dict):
        # A list usually comes wrapped: {"items": [...]}, {"data": [...]}.
        for key in ("items", "data", "results", "records", "rows"):
            if isinstance(payload.get(key), list):
                records, wrapper = payload[key], key
                break
        else:
            return {"kind": "object", "fields": sorted(payload)[:40]}

    if not isinstance(records, list):
        return {"kind": type(records).__name__}
    first = records[0] if records else None
    return {
        "kind": "list", "wrapper": wrapper, "count": len(records),
        "fields": sorted(first)[:40] if isinstance(first, dict) else [],
        "sample": records[:2],
    }


def sync(db: Session) -> dict:
    """Kept for the REST route; the agent tool `sync_external_data` does the work."""
    from ..agent.tools.external import sync_external_data

    result = sync_external_data(db)
    return result.data
