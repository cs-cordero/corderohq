"""Shared test fixtures for HSA receipt archiver tests."""

import os
from datetime import date
from email.message import EmailMessage

import pytest

# Set dummy AWS credentials so module-level boto3.client() calls don't fail during import.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123456789012:test-topic")
os.environ.setdefault("SNS_ALERT_TOPIC_ARN", "arn:aws:sns:us-east-1:123456789012:test-alerts")

from corderohq.archiver.ledger import LedgerEntry


@pytest.fixture
def sample_ledger_entry() -> LedgerEntry:
    """A standard ledger entry with both dates set."""
    return LedgerEntry(
        service_date=date(2025, 1, 15),
        payment_date=date(2025, 1, 16),
        provider="Test Provider",
        patient="CHRIS",
        category="Medical",
        description="Office visit copay",
        amount=123.45,
        receipt_s3_uri="s3://test-bucket/receipts/2025/2025-01-15_Test_Provider_Medical.pdf",
    )


@pytest.fixture
def ledger_entry_no_dates() -> LedgerEntry:
    """Ledger entry with None dates for edge case testing."""
    return LedgerEntry(
        service_date=None,
        payment_date=None,
        provider="Unknown Provider",
        patient="UNKNOWN",
        category="Other",
        description="Unknown receipt",
        amount=50.00,
        receipt_s3_uri="s3://test-bucket/receipts/2025/receipt.pdf",
    )


@pytest.fixture
def make_mime_email() -> type[
    tuple[
        str,
        str,
        str,
        list[tuple[str, str, bytes]] | None,
    ]
]:
    """Factory fixture to build raw MIME email bytes.

    Usage:
        email_bytes = make_mime_email(
            sender="test@example.com",
            subject="Test",
            body="Email body",
            attachments=[("receipt.jpg", "image/jpeg", b"jpeg-data")],
        )
    """

    def _make(
        sender: str = "test@example.com",
        subject: str = "Test Subject",
        body: str = "Test body",
        attachments: list[tuple[str, str, bytes]] | None = None,
    ) -> bytes:
        msg = EmailMessage()
        msg["From"] = sender
        msg["Subject"] = subject
        msg["To"] = "receipts@example.com"
        msg.set_content(body)

        if attachments:
            for filename, content_type, data in attachments:
                maintype, subtype = content_type.split("/")
                msg.add_attachment(
                    data,
                    maintype=maintype,
                    subtype=subtype,
                    filename=filename,
                )

        return msg.as_bytes()

    return _make  # type: ignore[return-value]
