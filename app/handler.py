"""
SLA Monitoring — Lambda entry point.
Built step by step.
  Step 1: extract the uploaded CSV bytes from the incoming API Gateway event.
  Step 2: wire extraction -> cleaning -> (stubbed) DB write -> response.
"""

import base64
import json
from email.parser import BytesParser
from email.policy import default
from io import BytesIO

import pandas as pd

from cleaning import clean_dataframe
from db import insert_rows


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
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def handler(event, context):
    """
    Lambda entry point. AWS invokes this directly.

    Flow: extract CSV bytes from the event -> parse into a DataFrame ->
    run the cleaning pipeline -> write cleaned rows to the DB -> return a
    JSON summary (row counts, what was flagged) for the frontend to show.
    """
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


if __name__ == "__main__":
    # Build a synthetic API Gateway event, the same shape AWS would send,
    # to test the extraction logic without needing a real deployment yet.
    boundary = "----WebKitFormBoundaryTest123"
    csv_content = b"service_id,status_code\nsvc-auth,200\nsvc-auth,500\n"

    fake_multipart_body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="checks.csv"\r\n'
        f"Content-Type: text/csv\r\n\r\n"
    ).encode("utf-8") + csv_content + f"\r\n--{boundary}--\r\n".encode("utf-8")

    fake_event = {
        "headers": {"content-type": f"multipart/form-data; boundary={boundary}"},
        "body": base64.b64encode(fake_multipart_body).decode("utf-8"),
        "isBase64Encoded": True,
    }

    extracted = extract_csv_bytes(fake_event)
    print("Extracted bytes match original:", extracted == csv_content)
    print(extracted.decode("utf-8"))

    print()
    print("=== Full handler() test with a real CSV file ===")
    with open("/mnt/user-data/uploads/monitoring_checks_9d_seed101.csv", "rb") as f:
        real_csv_bytes = f.read()

    real_body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="real.csv"\r\n'
        f"Content-Type: text/csv\r\n\r\n"
    ).encode("utf-8") + real_csv_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

    real_event = {
        "headers": {"content-type": f"multipart/form-data; boundary={boundary}"},
        "body": base64.b64encode(real_body).decode("utf-8"),
        "isBase64Encoded": True,
    }

    result = handler(real_event, context=None)
    print("statusCode:", result["statusCode"])
    print("body:", json.dumps(json.loads(result["body"]), indent=2))