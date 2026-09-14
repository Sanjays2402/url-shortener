"""POST /shorten — validate a URL, mint a short id, persist the mapping.

Request body (JSON):  {"url": "https://example.com/some/long/path"}
Response (200):       {"shortId": "aB3xK9q", "shortUrl": "https://<host>/aB3xK9q"}

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

    table = _get_table()

    short_id = ""
    for _ in range(_MAX_ID_ATTEMPTS):
        candidate = generate_short_id()
        try:
            table.put_item(
                Item={
                    "shortId": candidate,
                    "targetUrl": url,
                    "clickCount": 0,
                },
                ConditionExpression="attribute_not_exists(shortId)",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                continue  # id collision: vanishingly rare, just mint another
            log.exception("DynamoDB put_item failed")
            return _json(500, {"error": "Failed to store the short link."})
        short_id = candidate
        break

    if not short_id:
        return _json(500, {"error": "Could not mint a unique short id; try again."})

    log.info("Created short link %s -> %s", short_id, url)
    return _json(
        200,
        {"shortId": short_id, "shortUrl": _short_url_for(event, short_id)},
    )
