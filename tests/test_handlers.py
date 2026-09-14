"""Unit tests for the URL shortener Lambda handlers (boto3/DynamoDB mocked).

Run:  python -m pytest tests/ -v
"""

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent


def _cfn_unknown(loader, tag_suffix, node):
    """CloudFormation/SAM templates use intrinsic tags (!Ref, !Sub, !GetAtt…).

    Treat them as their underlying scalar/sequence/mapping so the template
    parses for smoke-check purposes.
    """
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


yaml.SafeLoader.add_multi_constructor("!", _cfn_unknown)


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


shorten_app = _load_module("shorten_app", ROOT / "src" / "shorten" / "app.py")
redirect_app = _load_module("redirect_app", ROOT / "src" / "redirect" / "app.py")


@pytest.fixture
def mock_table(monkeypatch):
    table = MagicMock()
    monkeypatch.setattr(shorten_app, "_table", table)
    monkeypatch.setattr(redirect_app, "_table", table)
    monkeypatch.setenv("TABLE_NAME", "test-links")
    return table


def _post_event(url, host="abc123.execute-api.us-east-1.amazonaws.com", **extra):
    body = {"url": url}
    body.update(extra)
    return {
        "body": json.dumps(body),
        "isBase64Encoded": False,
        "headers": {"host": host, "x-forwarded-proto": "https"},
        "requestContext": {"stage": "$default"},
    }


def _cond_check_failed():
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "nope"}},
        "PutItem",
    )


# ----------------------------------------------------------------------
# URL validation
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://example.com", True),
        ("http://example.com/a/b?x=1#frag", True),
        ("https://sub.domain.co.uk/path", True),
        ("ftp://example.com/file", False),
        ("example.com/no-scheme", False),
        ("javascript:alert(1)", False),
        ("", False),
        (None, False),
        (123, False),
        ("https://" + "a" * 2048, False),  # over the length limit
    ],
)
def test_is_valid_url(url, expected):
    assert shorten_app.is_valid_url(url) is expected


def test_generate_short_id_shape():
    ids = {shorten_app.generate_short_id() for _ in range(200)}
    assert len(ids) == 200  # no duplicates in a small sample
    assert all(len(i) == 7 and i.isalnum() for i in ids)


# ----------------------------------------------------------------------
# POST /shorten
# ----------------------------------------------------------------------
def test_shorten_happy_path(mock_table):
    resp = shorten_app.lambda_handler(_post_event("https://example.com/long"), None)

    assert resp["statusCode"] == 200
    payload = json.loads(resp["body"])
    short_id = payload["shortId"]
    assert payload["shortUrl"] == f"https://abc123.execute-api.us-east-1.amazonaws.com/{short_id}"

    kwargs = mock_table.put_item.call_args.kwargs
    item = kwargs["Item"]
    assert item["shortId"] == short_id
    assert item["targetUrl"] == "https://example.com/long"
    assert item["clickCount"] == 0
    assert item["createdAt"]  # ISO timestamp recorded at creation
    assert "expiresAt" not in item  # no TTL unless requested
    assert kwargs["ConditionExpression"] == "attribute_not_exists(shortId)"


def test_shorten_rejects_bad_input(mock_table):
    for body in ['{"url": "not-a-url"}', '{"url": "ftp://x.com"}', "{}", "not-json{{"]:
        resp = shorten_app.lambda_handler({**_post_event(""), "body": body}, None)
        assert resp["statusCode"] == 400
    mock_table.put_item.assert_not_called()


def test_shorten_retries_on_id_collision(mock_table):
    mock_table.put_item.side_effect = [_cond_check_failed(), None]

    resp = shorten_app.lambda_handler(_post_event("https://example.com/x"), None)

    assert resp["statusCode"] == 200
    assert mock_table.put_item.call_count == 2


def test_shorten_returns_500_on_dynamodb_error(mock_table):
    mock_table.put_item.side_effect = ClientError(
        {"Error": {"Code": "InternalServerError", "Message": "boom"}}, "PutItem"
    )

    resp = shorten_app.lambda_handler(_post_event("https://example.com/x"), None)

    assert resp["statusCode"] == 500


def test_short_url_includes_named_stage():
    event = _post_event("https://example.com/x")
    event["requestContext"]["stage"] = "prod"
    mock_table = MagicMock()
    shorten_app._table = mock_table

    resp = shorten_app.lambda_handler(event, None)

    short_id = json.loads(resp["body"])["shortId"]
    assert json.loads(resp["body"])["shortUrl"].endswith(f"/prod/{short_id}")


# ----------------------------------------------------------------------
# GET /{id}
# ----------------------------------------------------------------------
def test_redirect_happy_path(mock_table):
    mock_table.update_item.return_value = {
        "Attributes": {"targetUrl": "https://example.com/long", "clickCount": 3}
    }
    event = {"pathParameters": {"id": "aB3xK9q"}}

    resp = redirect_app.lambda_handler(event, None)

    assert resp["statusCode"] == 301
    assert resp["headers"]["Location"] == "https://example.com/long"

    kwargs = mock_table.update_item.call_args.kwargs
    assert kwargs["Key"] == {"shortId": "aB3xK9q"}
    assert kwargs["ConditionExpression"] == (
        "attribute_exists(shortId) AND "
        "(attribute_not_exists(expiresAt) OR expiresAt > :now)"
    )
    update = kwargs["UpdateExpression"]
    assert "clickCount = if_not_exists(clickCount, :zero) + :one" in update
    assert "lastClickedAt = :now_iso" in update
    values = kwargs["ExpressionAttributeValues"]
    assert values[":one"] == 1
    assert values[":now_iso"]  # timestamp of this click


def test_redirect_unknown_id_404(mock_table):
    mock_table.update_item.side_effect = _cond_check_failed()
    event = {"pathParameters": {"id": "nope123"}}

    resp = redirect_app.lambda_handler(event, None)

    assert resp["statusCode"] == 404
    assert "not found" in json.loads(resp["body"])["error"]


@pytest.mark.parametrize("bad_id", ["", "../etc", "a b", "x" * 33, None])
def test_redirect_rejects_malformed_ids(mock_table, bad_id):
    resp = redirect_app.lambda_handler({"pathParameters": {"id": bad_id}}, None)
    assert resp["statusCode"] == 404
    mock_table.update_item.assert_not_called()


def test_redirect_returns_500_on_dynamodb_error(mock_table):
    mock_table.update_item.side_effect = ClientError(
        {"Error": {"Code": "InternalServerError", "Message": "boom"}}, "UpdateItem"
    )
    resp = redirect_app.lambda_handler({"pathParameters": {"id": "aB3xK9q"}}, None)
    assert resp["statusCode"] == 500


# ----------------------------------------------------------------------
# Custom aliases
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "alias, expected",
    [
        ("my-link", True),
        ("abc", True),
        ("A1-_x" * 6, True),  # 30 chars, mixed case + symbols
        ("ab", False),  # too short
        ("x" * 33, False),  # too long
        ("has space", False),
        ("semi;colon", False),
        ("shorten", False),  # reserved route
        ("SHORTEN", False),  # reserved, case-insensitive
        ("favicon.ico", False),  # reserved static file
        ("api", False),
        ("", False),
        (None, False),
        (123, False),
    ],
)
def test_is_valid_alias(alias, expected):
    assert shorten_app.is_valid_alias(alias) is expected


def test_shorten_custom_alias_happy_path(mock_table):
    resp = shorten_app.lambda_handler(
        _post_event("https://example.com/long", alias="my-link"), None
    )

    assert resp["statusCode"] == 200
    payload = json.loads(resp["body"])
    assert payload["shortId"] == "my-link"
    assert payload["shortUrl"].endswith("/my-link")

    kwargs = mock_table.put_item.call_args.kwargs
    assert kwargs["Item"]["shortId"] == "my-link"
    assert kwargs["ConditionExpression"] == "attribute_not_exists(shortId)"


def test_shorten_alias_collision_returns_409(mock_table):
    mock_table.put_item.side_effect = _cond_check_failed()

    resp = shorten_app.lambda_handler(
        _post_event("https://example.com/x", alias="taken"), None
    )

    assert resp["statusCode"] == 409
    assert "already taken" in json.loads(resp["body"])["error"]


@pytest.mark.parametrize("bad_alias", ["ab", "x" * 33, "has space", "shorten", "API", ""])
def test_shorten_rejects_bad_alias(mock_table, bad_alias):
    resp = shorten_app.lambda_handler(
        _post_event("https://example.com/x", alias=bad_alias), None
    )
    assert resp["statusCode"] == 400
    mock_table.put_item.assert_not_called()


def test_shorten_alias_whitespace_is_trimmed(mock_table):
    resp = shorten_app.lambda_handler(
        _post_event("https://example.com/x", alias="  padded  "), None
    )
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["shortId"] == "padded"


# ----------------------------------------------------------------------
# Link expiry (DynamoDB TTL)
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, None),
        ("", None),
        (60, 60),  # minimum
        (86400, 86400),
        (31536000, 31536000),  # 365 days, maximum
    ],
)
def test_parse_expires_in_valid(raw, expected):
    assert shorten_app.parse_expires_in(raw) == expected


@pytest.mark.parametrize("bad", [0, -5, 59, 31536001, "3600", 1.5, True, [3600]])
def test_parse_expires_in_invalid(bad):
    with pytest.raises(ValueError):
        shorten_app.parse_expires_in(bad)


def test_shorten_with_expiry_sets_ttl_attribute(mock_table):
    before = __import__("time").time()
    resp = shorten_app.lambda_handler(
        _post_event("https://example.com/x", expiresIn=3600), None
    )
    after = __import__("time").time()

    assert resp["statusCode"] == 200
    payload = json.loads(resp["body"])

    item = mock_table.put_item.call_args.kwargs["Item"]
    assert int(before) <= item["expiresAt"] - 3600 <= int(after) + 1
    assert payload["expiresAt"] == item["expiresAt"]


@pytest.mark.parametrize("bad", [0, -60, 59, 99999999, "soon", 1.5])
def test_shorten_rejects_bad_expiry(mock_table, bad):
    resp = shorten_app.lambda_handler(
        _post_event("https://example.com/x", expiresIn=bad), None
    )
    assert resp["statusCode"] == 400
    mock_table.put_item.assert_not_called()


# ----------------------------------------------------------------------
# Expired redirects
# ----------------------------------------------------------------------
def test_redirect_expired_link_404s(mock_table):
    # DynamoDB's TTL sweeper can lag, so an expired item fails the condition
    # check and must read as "not found" rather than redirecting.
    mock_table.update_item.side_effect = _cond_check_failed()
    resp = redirect_app.lambda_handler({"pathParameters": {"id": "oldlink1"}}, None)
    assert resp["statusCode"] == 404
    assert "expired" in json.loads(resp["body"])["error"]


# ----------------------------------------------------------------------
# Template smoke check
# ----------------------------------------------------------------------
def test_template_parses_and_has_required_resources():
    with open(ROOT / "template.yaml") as fh:
        template = yaml.safe_load(fh)

    assert template["Transform"] == "AWS::Serverless-2016-10-31"
    resources = template["Resources"]
    assert resources["ShortenFunction"]["Type"] == "AWS::Serverless::Function"
    assert resources["RedirectFunction"]["Type"] == "AWS::Serverless::Function"
    assert resources["UrlTable"]["Type"] == "AWS::DynamoDB::Table"
    assert resources["UrlHttpApi"]["Type"] == "AWS::Serverless::HttpApi"
    assert resources["FrontendBucket"]["Type"] == "AWS::S3::Bucket"

    handlers = {
        resources["ShortenFunction"]["Properties"]["Handler"],
        resources["RedirectFunction"]["Properties"]["Handler"],
    }
    assert handlers == {"app.lambda_handler"}
    assert "ApiEndpoint" in template["Outputs"]


def test_template_enables_ttl_and_throttling():
    with open(ROOT / "template.yaml") as fh:
        template = yaml.safe_load(fh)
    resources = template["Resources"]

    ttl = resources["UrlTable"]["Properties"]["TimeToLiveSpecification"]
    assert ttl["AttributeName"] == "expiresAt"
    assert ttl["Enabled"] is True

    route_settings = resources["UrlHttpApi"]["Properties"]["DefaultRouteSettings"]
    assert route_settings["ThrottlingBurstLimit"] > 0
    assert route_settings["ThrottlingRateLimit"] > 0
