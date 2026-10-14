"""Tests for SNS notifier module."""

from unittest.mock import MagicMock, patch

from corderohq.archiver.ledger import LedgerEntry
from corderohq.aws.sns import (
    ALERT_TOPIC_ARN,
    notify_detailed_failure,
    notify_failure,
    notify_rejection,
    notify_success,
)


@patch("corderohq.aws.sns._SNS_CLIENT")
def test_notify_success_publishes_to_sns(mock_sns: MagicMock, sample_ledger_entry: LedgerEntry) -> None:
    notify_success([sample_ledger_entry])
    mock_sns.publish.assert_called_once()
    call_kwargs = mock_sns.publish.call_args[1]
    assert "TopicArn" in call_kwargs
    assert "1 item)" in call_kwargs["Subject"]


@patch("corderohq.aws.sns._SNS_CLIENT")
def test_notify_success_plural_subject(mock_sns: MagicMock, sample_ledger_entry: LedgerEntry) -> None:
    notify_success([sample_ledger_entry, sample_ledger_entry])
    call_kwargs = mock_sns.publish.call_args[1]
    assert "2 items)" in call_kwargs["Subject"]


@patch("corderohq.aws.sns._SNS_CLIENT")
def test_notify_success_includes_entry_details(mock_sns: MagicMock, sample_ledger_entry: LedgerEntry) -> None:
    notify_success([sample_ledger_entry])
    message = mock_sns.publish.call_args[1]["Message"]
    assert "Test Provider" in message
    assert "$123.45" in message
    assert "2025-01-15" in message
    assert "Medical" in message
    assert "Office visit copay" in message


@patch("corderohq.aws.sns._SNS_CLIENT")
def test_notify_success_includes_receipt_uri(mock_sns: MagicMock, sample_ledger_entry: LedgerEntry) -> None:
    notify_success([sample_ledger_entry])
    message = mock_sns.publish.call_args[1]["Message"]
    assert sample_ledger_entry.receipt_s3_uri in message


@patch("corderohq.aws.sns._SNS_CLIENT")
def test_notify_success_none_dates_show_na(mock_sns: MagicMock) -> None:
    entry = LedgerEntry(
        service_date=None,
        payment_date=None,
        provider="P",
        patient="",
        category="Other",
        description="D",
        amount=10.00,
        receipt_s3_uri="s3://b/r.pdf",
    )
    notify_success([entry])
    message = mock_sns.publish.call_args[1]["Message"]
    assert "N/A" in message


@patch("corderohq.aws.sns._SNS_CLIENT")
def test_notify_failure_publishes_to_sns(mock_sns: MagicMock) -> None:
    notify_failure("Something broke")
    mock_sns.publish.assert_called_once()
    call_kwargs = mock_sns.publish.call_args[1]
    assert call_kwargs["Subject"] == "HSA Receipt Processing Failed"
    assert "Something broke" in call_kwargs["Message"]


@patch("corderohq.aws.sns._SNS_CLIENT")
def test_notify_rejection_publishes_to_sns(mock_sns: MagicMock) -> None:
    notify_rejection("receipt.pdf", "Gym membership", "Not HSA-eligible")
    mock_sns.publish.assert_called_once()
    call_kwargs = mock_sns.publish.call_args[1]
    assert call_kwargs["Subject"] == "HSA Receipt Not Eligible"
    assert "Gym membership" in call_kwargs["Message"]
    assert "Not HSA-eligible" in call_kwargs["Message"]
    assert "receipt.pdf" in call_kwargs["Message"]


@patch("corderohq.aws.sns._SNS_CLIENT")
def test_notify_rejection_includes_force_store_instructions(mock_sns: MagicMock) -> None:
    notify_rejection("file.pdf", "Item", "Reason")
    message = mock_sns.publish.call_args[1]["Message"]
    assert "FORCE_STORE" in message


@patch("corderohq.aws.sns._SNS_CLIENT")
def test_notify_detailed_failure_publishes_to_alert_topic(mock_sns: MagicMock) -> None:
    exc = RuntimeError("something broke")
    notify_detailed_failure("Context message", exc)
    mock_sns.publish.assert_called_once()
    call_kwargs = mock_sns.publish.call_args[1]
    assert call_kwargs["TopicArn"] == ALERT_TOPIC_ARN
    assert call_kwargs["Subject"] == "HSA Receipt Processing Failed (Detailed)"
    assert "Context message" in call_kwargs["Message"]
    assert "RuntimeError" in call_kwargs["Message"]
    assert "something broke" in call_kwargs["Message"]


@patch("corderohq.aws.sns._SNS_CLIENT")
def test_notify_detailed_failure_includes_stack_trace(mock_sns: MagicMock) -> None:
    try:
        raise ValueError("test error")
    except ValueError as exc:
        notify_detailed_failure("Processing failed", exc)
    message = mock_sns.publish.call_args[1]["Message"]
    assert "Traceback" in message
    assert "ValueError" in message
    assert "test error" in message
