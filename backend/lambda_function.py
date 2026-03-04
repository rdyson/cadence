"""
Cadence Lambda function
Handles GET /state and POST /state for checkbox persistence.
Reads from DynamoDB, scoped per user via Cognito JWT claims.
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3

TABLE_NAME = os.environ.get("DYNAMODB_TABLE", "cadence-study")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Authorization,Content-Type",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Content-Type": "application/json",
}


def response(status: int, body: Any) -> dict:
    return {
        "statusCode": status,
        "headers": CORS_HEADERS,
        "body": json.dumps(body),
    }


def get_user_email(event: dict) -> str | None:
    """Extract user email from Cognito JWT claims injected by API Gateway."""
    ctx = event.get("requestContext", {})
    authorizer = ctx.get("authorizer", {})
    claims = authorizer.get("jwt", {}).get("claims", {})
    return claims.get("email")


def handler(event: dict, context: Any) -> dict:
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    path = event.get("rawPath", "")

    # OPTIONS preflight
    if method == "OPTIONS":
        return response(200, {})

    # GET /state — return all users' checkbox state
    if method == "GET" and path.endswith("/state"):
        return handle_get_state(event)

    # POST /state — update caller's checkbox state
    if method == "POST" and path.endswith("/state"):
        return handle_post_state(event)

    return response(404, {"error": "Not found"})


def handle_get_state(event: dict) -> dict:
    email = get_user_email(event)
    if not email:
        return response(401, {"error": "Unauthorized — no email in token"})

    try:
        result = table.scan()
        users_state = {}
        for item in result.get("Items", []):
            uid = item.get("userId")
            if uid:
                users_state[uid] = item.get("checks", {})
        return response(200, {"users": users_state})
    except Exception as e:
        return response(500, {"error": str(e)})


def handle_post_state(event: dict) -> dict:
    email = get_user_email(event)
    if not email:
        return response(401, {"error": "Unauthorized — no email in token"})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return response(400, {"error": "Invalid JSON"})

    item_title = body.get("item")
    checked = body.get("checked")

    if item_title is None or checked is None:
        return response(400, {"error": "Missing 'item' or 'checked'"})

    if not isinstance(checked, bool):
        return response(400, {"error": "'checked' must be a boolean"})

    try:
        # Read-modify-write: simple and avoids nested attribute issues
        result = table.get_item(Key={"userId": email})
        item = result.get("Item", {"userId": email, "checks": {}})
        checks = item.get("checks", {})

        if checked:
            checks[item_title] = True
        else:
            checks.pop(item_title, None)

        table.put_item(Item={
            "userId": email,
            "checks": checks,
            "updatedAt": _now_iso(),
        })
        return response(200, {"ok": True})
    except Exception as e:
        return response(500, {"error": str(e)})


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
