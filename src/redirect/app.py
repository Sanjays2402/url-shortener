"""GET /{id} — resolve a short id, count the click atomically, 301 redirect.

Success: 301 with ``Location: <targetUrl>``.
Unknown/expired id: 404 JSON.

The click counter increments with a single conditional UpdateItem so the
lookup and the increment are one atomic DynamoDB operation — no read/modify/
write race under concurrent clicks. The same operation stamps
``lastClickedAt`` and refuses expired links (DynamoDB TTL deletes them
eventually, but the item can linger — so expiry is enforced here too).

Environment:
    TABLE_NAME: DynamoDB table with partition key ``shortId``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
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


def _not_found(message: str) -> dict[str, Any]:
    return {
        "statusCode": 404,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": message}),
    }


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    short_id = (event.get("pathParameters") or {}).get("id") or ""

    if not _ID_PATTERN.match(short_id):
        return _not_found("Short link not found.")

    now = int(time.time())
    now_iso = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()

    table = _get_table()
    try:
        response = table.update_item(
            Key={"shortId": short_id},
            UpdateExpression=(
                "SET clickCount = if_not_exists(clickCount, :zero) + :one, "
                "lastClickedAt = :now_iso"
            ),
            # Expired links 404 even before DynamoDB's TTL sweeper removes them.
            ConditionExpression=(
                "attribute_exists(shortId) AND "
                "(attribute_not_exists(expiresAt) OR expiresAt > :now)"
            ),
            ExpressionAttributeValues={
                ":zero": 0,
                ":one": 1,
                ":now": now,
                ":now_iso": now_iso,
            },
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return _not_found("Short link not found or expired.")
        log.exception("DynamoDB update_item failed")
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "Failed to resolve the short link."}),
        }

    target = response["Attributes"]["targetUrl"]
    log.info("Redirect %s -> %s", short_id, target)
    return {
        "statusCode": 301,
        "headers": {"Location": target},
        "body": "",
    }
