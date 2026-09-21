"""
SLA Monitoring — Lambda entry point.

Routes:
    POST   /upload     — parse, clean, and persist a CSV upload
    GET    /dashboard  — aggregated SLA stats for the dashboard stats panel
    GET    /checks     — paginated log records (filterable by service / date range)
    OPTIONS *          — CORS preflight (required before every cross-origin request)
"""

import base64
import json
from email.parser import BytesParser
from email.policy import default
from io import BytesIO

import pandas as pd

from app.cleaning import clean_dataframe
from app.db import insert_rows, get_dashboard_data, get_checks


_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_csv_bytes(event: dict) -> bytes:
    """
    Pull the raw CSV file bytes out of an API Gateway event carrying a
    multipart/form-data upload (a browser <input type="file"> POST).

    API Gateway hands the whole HTTP body to Lambda as one blob — base64
    encoded when it contains binary data (isBase64Encoded=True) — with the
    original Content-Type header (including the multipart boundary)
    available in event['headers'].

    We reconstruct a minimal MIME message (Content-Type header + body) and
    hand it to Python's built-in email parser, which already knows how to
    split a multipart body into parts. This avoids pulling in a dedicated
    multipart-parsing dependency for something the standard library already
    does, via a well-known trick: multipart/form-data is a valid MIME
    structure, so an email parser can read it.
    """
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    content_type = headers.get("content-type", "")

    if "multipart/form-data" not in content_type:
        raise ValueError(f"Expected multipart/form-data, got: {content_type!r}")

    body = event.get("body", "")
    raw_body = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode("utf-8")

    mime_message = f"Content-Type: {content_type}\r\n\r\n".encode("utf-8") + raw_body
    message = BytesParser(policy=default).parsebytes(mime_message)

    if not message.is_multipart():
        raise ValueError("Body did not parse as multipart")

    for part in message.iter_parts():
        # the uploaded file part has a filename on its Content-Disposition;
        # other form fields (if any) won't
        if part.get_filename():
            return part.get_payload(decode=True)

    raise ValueError("No file part found in the uploaded form data")


def _get_filename(event: dict) -> str | None:
    """
    Same multipart parsing as extract_csv_bytes, but returns the original
    uploaded filename instead of the file contents — used to tag DB rows
    with which upload they came from (source_file column).
    """
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    content_type = headers.get("content-type", "")
    body = event.get("body", "")
    raw_body = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode("utf-8")

    mime_message = f"Content-Type: {content_type}\r\n\r\n".encode("utf-8") + raw_body
    message = BytesParser(policy=default).parsebytes(mime_message)

    if not message.is_multipart():
        return None

    for part in message.iter_parts():
        if part.get_filename():
            return part.get_filename()
    return None


def _response(status_code: int, body: dict) -> dict:
    """API Gateway expects this exact shape back from Lambda proxy integrations."""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json", **_CORS_HEADERS},
        "body": json.dumps(body),
    }


def _query_params(event: dict) -> dict:
    """Safely extract query string parameters; always returns a dict."""
    return event.get("queryStringParameters") or {}


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def handle_upload(event: dict) -> dict:
    """POST /upload — parse, clean, and persist an uploaded CSV."""
    try:
        csv_bytes = extract_csv_bytes(event)
    except ValueError as e:
        return _response(400, {"error": str(e)})

    try:
        raw_df = pd.read_csv(BytesIO(csv_bytes), dtype=str)
    except Exception as e:
        return _response(400, {"error": f"Could not parse CSV: {e}"})

    cleaned_df, report = clean_dataframe(raw_df)

    source_file = _get_filename(event) or "unknown.csv"
    try:
        db_report = insert_rows(cleaned_df, source_file=source_file)
    except Exception as e:
        # Cleaning already succeeded at this point — a DB failure shouldn't
        # look identical to a bad-upload failure, so it gets its own status
        # code and the cleaning report is still returned for visibility.
        return _response(502, {"error": f"Cleaning succeeded but DB write failed: {e}", **report})

    return _response(200, {"message": "Upload processed", **report, **db_report})


def handle_dashboard(event: dict) -> dict:
    """GET /dashboard — aggregated SLA stats. Supports ?from_date=&to_date= filters."""
    try:
        params = _query_params(event)
        data = get_dashboard_data(
            from_date=params.get("from_date"),
            to_date=params.get("to_date"),
        )
        return _response(200, data)
    except Exception as e:
        return _response(500, {"error": f"Failed to load dashboard: {e}"})


def handle_checks(event: dict) -> dict:
    """
    GET /checks — paginated check records.

    Query params:
        service_id  (optional) filter by service
        from_date   (optional) ISO 8601, e.g. "2025-05-08" or "2025-05-08T00:00:00Z"
        to_date     (optional) ISO 8601
        limit       (optional, default 50, max 200)
        offset      (optional, default 0)
    """
    try:
        params = _query_params(event)

        try:
            limit = int(params.get("limit", 50))
        except (TypeError, ValueError):
            limit = 50

        try:
            offset = int(params.get("offset", 0))
        except (TypeError, ValueError):
            offset = 0

        limit = max(1, min(limit, 200))   # clamp: 1 – 200
        offset = max(0, offset)

        data = get_checks(
            service_id=params.get("service_id"),
            from_date=params.get("from_date"),
            to_date=params.get("to_date"),
            limit=limit,
            offset=offset,
        )
        return _response(200, data)
    except Exception as e:
        return _response(500, {"error": f"Failed to load checks: {e}"})


# ---------------------------------------------------------------------------
# Lambda entry point — router
# ---------------------------------------------------------------------------

def handler(event, context):
    """
    Lambda entry point. AWS invokes this directly.

    Supports both API Gateway REST (httpMethod / path) and HTTP API v2
    (requestContext.http.method / rawPath) event shapes.
    """
    # Resolve method + path for both API Gateway REST and HTTP API v2 shapes
    http_info = event.get("requestContext", {}).get("http", {})
    method = http_info.get("method") or event.get("httpMethod", "")
    path = event.get("rawPath") or event.get("path") or ""

    # CORS preflight — browsers send OPTIONS before every cross-origin request
    if method == "OPTIONS":
        return {"statusCode": 200, "headers": _CORS_HEADERS, "body": ""}

    if method == "POST" and path == "/upload":
        return handle_upload(event)

    if method == "GET" and path == "/dashboard":
        return handle_dashboard(event)

    if method == "GET" and path == "/checks":
        return handle_checks(event)

    return _response(404, {"error": "Route not found", "method": method, "path": path})


# ---------------------------------------------------------------------------
# Local smoke-test  (python -m app.handler)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Build a synthetic API Gateway event, the same shape AWS would send,
    # to test the extraction logic without needing a real deployment yet.
    from dotenv import load_dotenv
    load_dotenv()

    boundary = "----WebKitFormBoundaryTest123"
    csv_path = "tasks/monitoring_checks_9d_seed101.csv"

    with open(csv_path, "rb") as f:
        real_csv_bytes = f.read()

    real_body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="real.csv"\r\n'
        f"Content-Type: text/csv\r\n\r\n"
    ).encode("utf-8") + real_csv_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

    upload_event = {
        "httpMethod": "POST",
        "path": "/upload",
        "headers": {"content-type": f"multipart/form-data; boundary={boundary}"},
        "body": base64.b64encode(real_body).decode("utf-8"),
        "isBase64Encoded": True,
    }

    print("=== POST /upload ===")
    result = handler(upload_event, context=None)
    print("statusCode:", result["statusCode"])
    print("body:", json.dumps(json.loads(result["body"]), indent=2))

    print("\n=== GET /dashboard ===")
    dashboard_event = {"httpMethod": "GET", "path": "/dashboard", "queryStringParameters": None}
    result = handler(dashboard_event, context=None)
    print("statusCode:", result["statusCode"])
    print("body:", json.dumps(json.loads(result["body"]), indent=2))

    print("\n=== GET /checks?limit=5 ===")
    checks_event = {"httpMethod": "GET", "path": "/checks", "queryStringParameters": {"limit": "5"}}
    result = handler(checks_event, context=None)
    print("statusCode:", result["statusCode"])
    print("body:", json.dumps(json.loads(result["body"]), indent=2))