"""The main Lambda handler. It reads an HSA receipt from an email or from a web upload."""

import json
import logging
from email.utils import parseaddr
from typing import Any

from corderohq.archiver.processor import ProcessingResult, process_attachment
from corderohq.aws.s3 import fetch_raw_email, fetch_upload, tag_raw_email
from corderohq.aws.ses import parse_ses_email
from corderohq.aws.sns import notify_detailed_failure, notify_failure, notify_rejection, notify_success
from corderohq.aws.ssm import get_ssm_param
from corderohq.util import get_env_var

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)

BUCKET_NAME = get_env_var("BUCKET_NAME")
SSM_API_KEY_PARAM = get_env_var("SSM_API_KEY_PARAM")
SSM_ALLOWED_SENDERS_PARAM = get_env_var("SSM_ALLOWED_SENDERS_PARAM")

FORCE_STORE_PREFIX = "FORCE_STORE"


def process_receipt(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Process an incoming SES email event or web upload event."""
    try:
        return _handle(event)
    except Exception as exc:
        LOGGER.exception("Failed to process receipt")
        notify_detailed_failure("Top-level failure in process_receipt", exc)
        return {"statusCode": 500, "body": "Internal error"}


def _handle(event: dict[str, Any]) -> dict[str, Any]:
    if event.get("source") == "web-upload":
        return _handle_web_upload(event)
    return _handle_ses(event)


def _handle_ses(event: dict[str, Any]) -> dict[str, Any]:
    ses_record = event["Records"][0]["ses"]
    mail = ses_record["mail"]
    message_id = mail["messageId"]

    receipt = ses_record["receipt"]
    raw_email_key = f"raw-emails/{message_id}"
    LOGGER.info("Processing email %s", message_id)

    spf = receipt.get("spfVerdict", {}).get("status", "GRAY")
    dkim = receipt.get("dkimVerdict", {}).get("status", "GRAY")
    if spf != "PASS" or dkim != "PASS":
        LOGGER.warning("Email authentication failed: SPF=%s, DKIM=%s", spf, dkim)
        return {"statusCode": 403, "body": "Email authentication failed"}

    raw_email = fetch_raw_email(BUCKET_NAME, raw_email_key)
    parsed = parse_ses_email(raw_email)

    _, sender_email = parseaddr(parsed.sender)
    sender_email = sender_email.lower()

    allowed_senders = get_ssm_param(SSM_ALLOWED_SENDERS_PARAM)
    allowed_set = {s.strip().lower() for s in allowed_senders.split(",")}
    if sender_email not in allowed_set:
        LOGGER.warning("Unauthorized sender: %s", sender_email)
        return {"statusCode": 403, "body": "Unauthorized sender"}

    if not parsed.attachments:
        LOGGER.warning("No attachments found in email from %s", sender_email)
        return {"statusCode": 400, "body": "No attachments"}

    # Read the flag from `top_body`, and not from `body`. A reply quotes the rejection
    # notification, and that notification names FORCE_STORE in its instructions.
    force_store = FORCE_STORE_PREFIX in parsed.top_body.upper()
    LOGGER.info(
        "FORCE_STORE found: %s (top body of %d characters, full body of %d)",
        force_store,
        len(parsed.top_body),
        len(parsed.body),
    )
    api_key = get_ssm_param(SSM_API_KEY_PARAM)

    failed_attachments: list[str] = []
    for i, attachment in enumerate(parsed.attachments):
        LOGGER.info(
            "Attachment %d/%d: filename=%s, content_type=%s, size=%d bytes",
            i + 1,
            len(parsed.attachments),
            attachment.filename,
            attachment.content_type,
            len(attachment.data),
        )
        try:
            result = process_attachment(
                data=attachment.data,
                content_type=attachment.content_type,
                filename=attachment.filename,
                force_store=force_store,
                api_key=api_key,
                bucket=BUCKET_NAME,
            )
            _send_notifications(result)
        except Exception as exc:
            LOGGER.exception("Failed to process attachment %s", attachment.filename)
            notify_failure(f"Failed to process attachment: {attachment.filename}")
            notify_detailed_failure(f"Failed to process attachment: {attachment.filename}", exc)
            failed_attachments.append(attachment.filename)

    tag_raw_email(BUCKET_NAME, raw_email_key)

    if failed_attachments:
        body = f"Failed attachments: {', '.join(failed_attachments)}"
        if len(failed_attachments) == len(parsed.attachments):
            return {"statusCode": 500, "body": body}
        return {"statusCode": 207, "body": body}

    return {"statusCode": 200, "body": "Processed"}


def _handle_web_upload(event: dict[str, Any]) -> dict[str, Any]:
    bucket = event["bucket"]
    key = event["key"]
    content_type = event["content_type"]
    filename = event["filename"]
    force_store = event.get("force_store", False)
    store_only = event.get("store_only", False)

    LOGGER.info("Processing web upload: %s (content_type=%s, store_only=%s)", filename, content_type, store_only)

    data = fetch_upload(bucket, key)
    api_key = get_ssm_param(SSM_API_KEY_PARAM)

    result = process_attachment(
        data=data,
        content_type=content_type,
        filename=filename,
        force_store=force_store,
        api_key=api_key,
        bucket=bucket,
        store_only=store_only,
    )

    response_body = {
        "entries": [
            {
                "service_date": e.service_date.isoformat() if e.service_date else None,
                "payment_date": e.payment_date.isoformat() if e.payment_date else None,
                "provider": e.provider,
                "patient": e.patient,
                "category": e.category,
                "description": e.description,
                "amount": e.amount,
                "receipt_s3_uri": e.receipt_s3_uri,
            }
            for e in result.entries
        ],
        "rejections": [
            {
                "filename": r.filename,
                "description": r.description,
                "reasoning": r.reasoning,
            }
            for r in result.rejections
        ],
        "receipt_s3_uri": result.receipt_s3_uri,
    }

    return {"statusCode": 200, "body": json.dumps(response_body)}


def _send_notifications(result: ProcessingResult) -> None:
    """Send the SNS notifications for the results."""
    for rejection in result.rejections:
        notify_rejection(rejection.filename, rejection.description, rejection.reasoning)
    if result.entries:
        notify_success(result.entries)
