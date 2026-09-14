"""POST /shorten — validate a URL, mint (or claim) a short id, persist the mapping.

Request body (JSON):
    {
      "url": "https://example.com/some/long/path",   // required
      "alias": "my-link",                             // optional custom short id
      "expiresIn": 86400                              // optional TTL, seconds
    }

Response (200):
    {"shortId": "aB3xK9q", "shortUrl": "https://<host>/aB3xK9q", "expiresAt": 1728...}

Environment:
    TABLE_NAME: DynamoDB table with partition key ``shortId``.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

MAX_URL_LENGTH = 2048
ID_LENGTH = 7
_ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
_MAX_ID_ATTEMPTS = 5

# Custom aliases: 3-32 chars from the URL-safe set.
_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,32}$")

# Words that must never become a short id — they collide with real routes or
# would confuse visitors (e.g. /shorten, /health, /favicon.ico).
RESERVED_ALIASES = frozenset(
    {
        "shorten",
        "api",
        "health",
        "healthz",
        "stats",
        "metrics",
        "admin",
        "login",
        "logout",
        "signup",
        "register",
        "www",
        "static",
        "assets",
        "docs",
        "help",
        "support",
        "about",
        "contact",
        "pricing",
        "terms",
        "privacy",
        "status",
        "blog",
        "index",
        "index.html",
        "favicon.ico",
        "robots.txt",
        "sitemap.xml",
        ".well-known",
    }
)

# Expiry window: 1 minute … 365 days, expressed in seconds.
MIN_EXPIRY_SECONDS = 60
MAX_EXPIRY_SECONDS = 365 * 24 * 3600

_table = None


def _get_table():
    """Lazily resolve the DynamoDB table (keeps import time cheap and tests easy)."""
    global _table
    if _table is None:
        table_name = os.environ["TABLE_NAME"]
        _table = boto3.resource("dynamodb").Table(table_name)
    return _table


def _json(status: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


def is_valid_url(raw: Any) -> bool:
    """A usable target: a non-empty http(s) URL with a host, within length limits."""
    if not isinstance(raw, str):
        return False
    url = raw.strip()
    if not url or len(url) > MAX_URL_LENGTH:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def is_valid_alias(raw: Any) -> bool:
    """A claimable custom short id: right charset/length and not reserved."""
    if not isinstance(raw, str):
        return False
    alias = raw.strip()
    if not _ALIAS_PATTERN.match(alias):
        return False
    return alias.lower() not in RESERVED_ALIASES


def parse_expires_in(raw: Any) -> int | None:
    """User-supplied TTL in seconds; ``None`` means no expiry.

    Raises ValueError when the value is present but out of range or not an int.
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError("expiresIn must be a number of seconds.")
    seconds = int(raw)
    if seconds != raw:  # fractional seconds are not meaningful for TTL
        raise ValueError("expiresIn must be a whole number of seconds.")
    if not MIN_EXPIRY_SECONDS <= seconds <= MAX_EXPIRY_SECONDS:
        raise ValueError(
            f"expiresIn must be between {MIN_EXPIRY_SECONDS} seconds and "
            f"{MAX_EXPIRY_SECONDS} seconds (365 days)."
        )
    return seconds


def generate_short_id(length: int = ID_LENGTH) -> str:
    """Cryptographically random, URL-safe id over an unambiguous alphabet."""
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(length))


def _decode_body(event: dict[str, Any]) -> dict[str, Any]:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return json.loads(body) if body else {}


def _short_url_for(event: dict[str, Any], short_id: str) -> str:
    headers = event.get("headers") or {}
    host = headers.get("host") or headers.get("Host", "")
    proto = headers.get("x-forwarded-proto", "https")
    stage = (event.get("requestContext") or {}).get("stage") or ""
    prefix = f"/{stage}" if stage and stage != "$default" else ""
    return f"{proto}://{host}{prefix}/{short_id}"


def _conditional_failed(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    try:
        payload = _decode_body(event)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _json(400, {"error": "Request body must be valid JSON."})

    url = (payload.get("url") or "").strip()
    if not is_valid_url(url):
        return _json(
            400,
            {"error": "Provide a valid absolute http(s) URL up to 2048 characters."},
        )

    alias = payload.get("alias")
    if alias is not None:
        alias = alias.strip() if isinstance(alias, str) else alias
        if not is_valid_alias(alias):
            return _json(
                400,
                {
                    "error": (
                        "Alias must be 3-32 characters of letters, numbers, "
                        "'-' or '_' and not a reserved word."
                    )
                },
            )

    try:
        expires_in = parse_expires_in(payload.get("expiresIn"))
    except ValueError as exc:
        return _json(400, {"error": str(exc)})

    table = _get_table()

    item = {
        "targetUrl": url,
        "clickCount": 0,
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    expires_at = None
    if expires_in is not None:
        expires_at = int(time.time()) + expires_in
        item["expiresAt"] = expires_at  # DynamoDB TTL attribute (epoch seconds)

    if alias:
        # Custom alias: exactly one attempt; a taken alias is a 409, not a retry.
        item["shortId"] = alias
        try:
            table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(shortId)",
            )
        except ClientError as exc:
            if _conditional_failed(exc):
                return _json(409, {"error": f'Alias "{alias}" is already taken.'})
            log.exception("DynamoDB put_item failed")
            return _json(500, {"error": "Failed to store the short link."})
        short_id = alias
    else:
        short_id = ""
        for _ in range(_MAX_ID_ATTEMPTS):
            candidate = generate_short_id()
            item["shortId"] = candidate
            try:
                table.put_item(
                    Item=item,
                    ConditionExpression="attribute_not_exists(shortId)",
                )
            except ClientError as exc:
                if _conditional_failed(exc):
                    continue  # id collision: vanishingly rare, just mint another
                log.exception("DynamoDB put_item failed")
                return _json(500, {"error": "Failed to store the short link."})
            short_id = candidate
            break
        if not short_id:
            return _json(500, {"error": "Could not mint a unique short id; try again."})

    log.info("Created short link %s -> %s", short_id, url)
    response: dict[str, Any] = {
        "shortId": short_id,
        "shortUrl": _short_url_for(event, short_id),
    }
    if expires_at is not None:
        response["expiresAt"] = expires_at
    return _json(200, response)
