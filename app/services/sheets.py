"""Google Sheets client.

Authenticates as a service account: a JWT signed with the account's private
key is exchanged for an access token, which is cached until it expires. Only
the REST API is used — no Google SDK — so the dependency is one signature
library instead of a stack.

For this to reach a spreadsheet, the admin has to share it with the service
account's email (the `client_email` in the JSON). Google returns 403 otherwise,
and `probe` says exactly that instead of a bare error code.
"""
from __future__ import annotations

import time
from urllib.parse import quote

import httpx
import jwt

TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPE = "https://www.googleapis.com/auth/spreadsheets"

TOKEN_TTL = 3600
TIMEOUT = 30
# Rows read when inspecting a tab: enough to see the shape, not the whole sheet.
PREVIEW_ROWS = 5


class SheetsError(RuntimeError):
    """Anything that stops us from reading or writing the spreadsheet."""


# client_email -> (access token, expiry). One token serves every call.
_tokens: dict[str, tuple[str, float]] = {}


def _access_token(credentials: dict) -> str:
    email = credentials.get("client_email", "")
    cached = _tokens.get(email)
    if cached and cached[1] > time.time() + 60:
        return cached[0]

    issued = int(time.time())
    assertion = jwt.encode(
        {
            "iss": email,
            "scope": SCOPE,
            "aud": credentials.get("token_uri") or TOKEN_URL,
            "iat": issued,
            "exp": issued + TOKEN_TTL,
        },
        credentials["private_key"],
        algorithm="RS256",
    )

    try:
        response = httpx.post(
            credentials.get("token_uri") or TOKEN_URL,
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                  "assertion": assertion},
            timeout=TIMEOUT,
        )
    except httpx.HTTPError as err:
        raise SheetsError(f"No pude contactar a Google: {err}") from None

    if not response.is_success:
        raise SheetsError(
            "Google rechazó las credenciales de la cuenta de servicio "
            f"({response.status_code}). Revise que el JSON sea el correcto y que la "
            "API de Sheets esté habilitada en el proyecto.")

    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise SheetsError("Google no devolvió un token de acceso.")

    _tokens[email] = (token, time.time() + int(payload.get("expires_in", TOKEN_TTL)))
    return token


def _request(credentials: dict, method: str, path: str, **kwargs) -> dict:
    token = _access_token(credentials)
    try:
        response = httpx.request(
            method, f"{API_BASE}/{path}",
            headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT, **kwargs)
    except httpx.HTTPError as err:
        raise SheetsError(f"No pude contactar a Google Sheets: {err}") from None

    if response.status_code == 403:
        raise SheetsError(
            "La cuenta de servicio no tiene acceso a esa hoja. Compartila con "
            f"{credentials.get('client_email')} como editor.")
    if response.status_code == 404:
        raise SheetsError("No existe una hoja con ese ID (o no es visible para la cuenta).")
    if not response.is_success:
        raise SheetsError(f"Google Sheets respondió {response.status_code}: "
                          f"{response.text[:200]}")
    return response.json()


# ----------------------------------------------------------------- lectura

def spreadsheet(credentials: dict, spreadsheet_id: str) -> dict:
    """Metadata: title and the tabs it contains."""
    data = _request(credentials, "GET", quote(spreadsheet_id, safe=""),
                    params={"fields": "properties.title,sheets.properties"})
    return {
        "title": (data.get("properties") or {}).get("title", ""),
        "tabs": [(sheet.get("properties") or {}).get("title", "")
                 for sheet in data.get("sheets", [])],
    }


def read_rows(credentials: dict, spreadsheet_id: str, tab: str,
              limit: int | None = None) -> list[list]:
    """Values of a tab, first row included."""
    span = f"{tab}!A1:ZZ{limit}" if limit else f"{tab}"
    data = _request(credentials, "GET",
                    f"{quote(spreadsheet_id, safe='')}/values/{quote(span, safe='')}",
                    params={"majorDimension": "ROWS"})
    return data.get("values", [])


def append_row(credentials: dict, spreadsheet_id: str, tab: str, values: list) -> dict:
    """Add a row at the end of the tab."""
    return _request(
        credentials, "POST",
        f"{quote(spreadsheet_id, safe='')}/values/{quote(tab, safe='')}:append",
        params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
        json={"values": [values]})


def update_row(credentials: dict, spreadsheet_id: str, tab: str,
               row_number: int, values: list) -> dict:
    """Overwrite one row, 1-based as the sheet numbers them."""
    span = f"{tab}!A{row_number}"
    return _request(
        credentials, "PUT",
        f"{quote(spreadsheet_id, safe='')}/values/{quote(span, safe='')}",
        params={"valueInputOption": "USER_ENTERED"},
        json={"values": [values]})


# ---------------------------------------------------------------- inspección

def probe(credentials: dict | None, spreadsheet_id: str, tabs: dict[str, str]) -> dict:
    """Check access and report what the sheet actually contains.

    This is what answers "todavía no vimos qué trae": for each configured tab it
    returns the header row, how many rows there are and a small sample, which is
    what the field mapping will be decided from.
    """
    if not credentials:
        raise SheetsError("Falta el JSON de la cuenta de servicio.")
    if not spreadsheet_id:
        raise SheetsError("Falta el ID de la hoja de cálculo.")

    meta = spreadsheet(credentials, spreadsheet_id)
    report = {
        "title": meta["title"],
        "tabs_available": meta["tabs"],
        "service_account": credentials.get("client_email"),
        "entities": {},
    }

    for entity, tab in tabs.items():
        if not tab:
            continue
        if tab not in meta["tabs"]:
            report["entities"][entity] = {
                "tab": tab, "found": False,
                "problem": f"La hoja no tiene una pestaña «{tab}».",
            }
            continue
        rows = read_rows(credentials, spreadsheet_id, tab, limit=PREVIEW_ROWS + 1)
        header = rows[0] if rows else []
        report["entities"][entity] = {
            "tab": tab, "found": True,
            "columns": header,
            "sample": rows[1:PREVIEW_ROWS + 1],
            "preview_rows": max(len(rows) - 1, 0),
        }
    return report
