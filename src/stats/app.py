"""GET /stats/{id} — read-only click statistics for a short link.

Response (200):
    {
      "shortId": "aB3xK9q",
      "targetUrl": "https://example.com/long/path",
      "clickCount": 42,
      "createdAt": "2026-09-15T12:00:00+00:00",
      "lastClickedAt": "2026-09-15T14:30:00+00:00",  // null when never clicked
      "expiresAt": 1728...                            // only when the link expires
    }

Unknown/expired id: 404 JSON. The endpoint never touches the click counter —
statistics are a plain DynamoDB ``get_item``.

Environment:
    TABLE_NAME: DynamoDB table with partition key ``shortId``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

_table = None


def _get_table():
    """Lazily resolve the DynamoDB table (keeps import time cheap and tests easy)."""
    global _table
    if _table is None:
        table_name = os.environ["TABLE_NAME"]
        _table = boto3.resource("dynamodb").Table(table_name)
    return _table


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    short_id = (event.get("pathParameters") or {}).get("id") or ""

    if not _ID_PATTERN.match(short_id):
        return _response(404, {"error": "Short link not found."})

    table = _get_table()
    try:
        result = table.get_item(
            Key={"shortId": short_id},
            ProjectionExpression=(
                "targetUrl, clickCount, createdAt, lastClickedAt, expiresAt"
            ),
        )
    except ClientError:
        log.exception("DynamoDB get_item failed")
        return _response(500, {"error": "Failed to read the link statistics."})

    item = result.get("Item")
    now = int(time.time())
    if item is None or ("expiresAt" in item and item["expiresAt"] <= now):
        # DynamoDB's TTL sweeper can lag, so expired links 404 here too —
        # the same rule the redirect handler enforces.
        return _response(404, {"error": "Short link not found or expired."})

    stats: dict[str, Any] = {
        "shortId": short_id,
        "targetUrl": item.get("targetUrl"),
        "clickCount": int(item.get("clickCount", 0)),
        "createdAt": item.get("createdAt"),
        "lastClickedAt": item.get("lastClickedAt"),
    }
    if "expiresAt" in item:
        stats["expiresAt"] = int(item["expiresAt"])
    return _response(200, stats)


def _response(status: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }
