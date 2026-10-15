"""Publish notifications to SNS."""

import traceback

import boto3

from corderohq.archiver.ledger import LedgerEntry
from corderohq.util import get_env_var

_SNS_CLIENT = boto3.client("sns")

# Two topics, split by the reader. `TOPIC_ARN` carries the messages that a household member
# reads. `ALERT_TOPIC_ARN` is the shared `corderohq-alerts` topic, and it carries the messages
# that need a developer: a stack trace, a CloudWatch alarm, and an AWS budget alert.
TOPIC_ARN = get_env_var("SNS_TOPIC_ARN")
ALERT_TOPIC_ARN = get_env_var("SNS_ALERT_TOPIC_ARN")


def notify_success(entries: list[LedgerEntry]) -> None:
    """Publish a success notification with a formatted table of ledger entries."""
    n = len(entries)
    subject = f"HSA Receipt Archived ({n} item{'s' if n != 1 else ''})"

    rows: list[str] = []
    for entry in entries:
        service_date = entry.service_date.isoformat() if entry.service_date else "N/A"
        payment_date = entry.payment_date.isoformat() if entry.payment_date else "N/A"
        patient = entry.patient or "N/A"
        amount = f"${entry.amount:.2f}"
        rows.append(
            f"  {service_date:<12}  {payment_date:<12}  {entry.provider:<20}  {patient:<15}  "
            f"{entry.category:<10}  {entry.description:<30}  {amount}"
        )

    header = (
        f"  {'Service Date':<12}  {'Payment Date':<12}  {'Provider':<20}  {'Patient/For':<15}  "
        f"{'Category':<10}  {'Description':<30}  Amount"
    )
    separator = "  " + "-" * (len(header) - 2)
    receipt_uri = entries[0].receipt_s3_uri if entries else ""

    message = "\n".join([header, separator, *rows])
    if receipt_uri:
        message += f"\n\nReceipt: {receipt_uri}"

    _SNS_CLIENT.publish(TopicArn=TOPIC_ARN, Subject=subject, Message=message)


def notify_failure(message: str) -> None:
    """Publish a failure notification when the app cannot read a receipt."""
    body = (
        "An error occurred while processing your receipt.\n\n"
        f"Error: {message}\n\n"
        "Please try re-sending the email. If the problem persists, "
        "check that the attachment is a supported image (JPEG, PNG, GIF, WebP) or PDF."
    )
    _SNS_CLIENT.publish(TopicArn=TOPIC_ARN, Subject="HSA Receipt Processing Failed", Message=body)


def notify_rejection(filename: str, description: str, reasoning: str) -> None:
    """Publish a rejection notification explaining why a receipt was not eligible."""
    body = (
        f"Your receipt for {description} was determined to not be HSA-eligible.\n\n"
        f"Source file: {filename}\n"
        f"Reasoning: {reasoning}\n\n"
        "If you believe this is incorrect, send the same email again and write "
        '"FORCE_STORE" at the top of the body. The app archives the receipt then, '
        "whatever the eligibility. Write the word in your own text. The app ignores "
        "the quoted text of this message below your reply."
    )
    _SNS_CLIENT.publish(TopicArn=TOPIC_ARN, Subject="HSA Receipt Not Eligible", Message=body)


def notify_detailed_failure(message: str, exception: BaseException) -> None:
    """Publish the full details of a failure, with the stack trace, to the shared alert topic."""
    tb = "".join(traceback.format_exception(type(exception), exception, exception.__traceback__))
    body = (
        f"Failure context: {message}\n\n"
        f"Exception type: {type(exception).__qualname__}\n"
        f"Exception message: {exception}\n\n"
        f"Stack trace:\n{tb}"
    )
    _SNS_CLIENT.publish(
        TopicArn=ALERT_TOPIC_ARN,
        Subject="HSA Receipt Processing Failed (Detailed)",
        Message=body,
    )
