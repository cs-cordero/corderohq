"""Tests for receipt_handler module."""

import json
import os
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from corderohq.archiver.ledger import LedgerEntry
from corderohq.archiver.processor import ProcessingResult, RejectionDetail
from corderohq.aws.ses import Attachment, ParsedEmail

ENV_VARS = {
    "BUCKET_NAME": "test-bucket",
    "SSM_API_KEY_PARAM": "/test/api-key",
    "SSM_ALLOWED_SENDERS_PARAM": "/test/senders",
}


def _make_ses_event(
    message_id: str = "msg-123",
    spf: str = "PASS",
    dkim: str = "PASS",
    dmarc: str = "PASS",
) -> dict:
    return {
        "Records": [
            {
                "ses": {
                    "mail": {"messageId": message_id},
                    "receipt": {
                        "spfVerdict": {"status": spf},
                        "dkimVerdict": {"status": dkim},
                        "dmarcVerdict": {"status": dmarc},
                    },
                }
            }
        ]
    }


def _make_web_upload_event(**overrides: object) -> dict:
    defaults: dict[str, object] = {
        "source": "web-upload",
        "bucket": "test-bucket",
        "key": "raw-uploads/abc.pdf",
        "content_type": "application/pdf",
        "filename": "receipt.pdf",
        "force_store": False,
    }
    defaults.update(overrides)
    return defaults


def _sample_entry() -> LedgerEntry:
    return LedgerEntry(
        service_date=datetime(2025, 1, 15, tzinfo=UTC).date(),
        payment_date=None,
        provider="Dr Smith",
        patient="CHRIS",
        category="Medical",
        description="Office visit",
        amount=100.0,
        receipt_s3_uri="s3://test-bucket/receipts/2025/receipt.pdf",
    )


def _make_parsed_email(
    sender: str = "allowed@example.com",
    subject: str = "Receipt",
    body: str = "",
    attachments: list[Attachment] | None = None,
    top_body: str | None = None,
) -> ParsedEmail:
    if attachments is None:
        attachments = [Attachment("receipt.jpg", "image/jpeg", b"jpeg-data")]
    return ParsedEmail(
        sender=sender,
        subject=subject,
        body=body,
        top_body=body if top_body is None else top_body,
        attachments=attachments,
    )


class TestSesPath:
    @patch.dict(os.environ, ENV_VARS)
    @patch("corderohq.receipt_handler.tag_raw_email")
    @patch("corderohq.receipt_handler.notify_success")
    @patch("corderohq.receipt_handler.process_attachment")
    @patch("corderohq.receipt_handler.parse_ses_email")
    @patch("corderohq.receipt_handler.fetch_raw_email", return_value=b"raw-email")
    @patch("corderohq.receipt_handler.get_ssm_param")
    def test_happy_path(
        self,
        mock_ssm: MagicMock,
        mock_fetch_email: MagicMock,
        mock_parse: MagicMock,
        mock_process: MagicMock,
        mock_notify_success: MagicMock,
        mock_tag: MagicMock,
    ) -> None:
        mock_ssm.side_effect = lambda name: {"/test/api-key": "key", "/test/senders": "allowed@example.com"}[name]
        mock_parse.return_value = _make_parsed_email()
        mock_process.return_value = ProcessingResult(
            entries=[_sample_entry()],
            rejections=[],
            receipt_s3_uri="s3://test-bucket/receipts/2025/receipt.pdf",
        )

        from corderohq.receipt_handler import _handle

        result = _handle(_make_ses_event())

        assert result["statusCode"] == 200
        mock_fetch_email.assert_called_once()
        mock_process.assert_called_once()
        mock_notify_success.assert_called_once()
        mock_tag.assert_called_once()

    @patch.dict(os.environ, ENV_VARS)
    @patch("corderohq.receipt_handler.tag_raw_email")
    @patch("corderohq.receipt_handler.notify_success")
    @patch("corderohq.receipt_handler.process_attachment")
    @patch("corderohq.receipt_handler.parse_ses_email")
    @patch("corderohq.receipt_handler.fetch_raw_email", return_value=b"raw-email")
    @patch("corderohq.receipt_handler.get_ssm_param")
    def test_force_store_in_body_reaches_the_processor(
        self,
        mock_ssm: MagicMock,
        mock_fetch_email: MagicMock,
        mock_parse: MagicMock,
        mock_process: MagicMock,
        mock_notify_success: MagicMock,
        mock_tag: MagicMock,
    ) -> None:
        mock_ssm.side_effect = lambda name: {"/test/api-key": "key", "/test/senders": "allowed@example.com"}[name]
        mock_parse.return_value = _make_parsed_email(body="please keep this one\nforce_store\n")
        mock_process.return_value = ProcessingResult(entries=[_sample_entry()], rejections=[], receipt_s3_uri="s3://x")

        from corderohq.receipt_handler import _handle

        _handle(_make_ses_event())

        assert mock_process.call_args[1]["force_store"] is True

    @patch.dict(os.environ, ENV_VARS)
    @patch("corderohq.receipt_handler.tag_raw_email")
    @patch("corderohq.receipt_handler.notify_success")
    @patch("corderohq.receipt_handler.process_attachment")
    @patch("corderohq.receipt_handler.parse_ses_email")
    @patch("corderohq.receipt_handler.fetch_raw_email", return_value=b"raw-email")
    @patch("corderohq.receipt_handler.get_ssm_param")
    def test_empty_body_leaves_force_store_off(
        self,
        mock_ssm: MagicMock,
        mock_fetch_email: MagicMock,
        mock_parse: MagicMock,
        mock_process: MagicMock,
        mock_notify_success: MagicMock,
        mock_tag: MagicMock,
    ) -> None:
        mock_ssm.side_effect = lambda name: {"/test/api-key": "key", "/test/senders": "allowed@example.com"}[name]
        mock_parse.return_value = _make_parsed_email(body="")
        mock_process.return_value = ProcessingResult(entries=[_sample_entry()], rejections=[], receipt_s3_uri="s3://x")

        from corderohq.receipt_handler import _handle

        _handle(_make_ses_event())

        assert mock_process.call_args[1]["force_store"] is False

    @patch.dict(os.environ, ENV_VARS)
    @patch("corderohq.receipt_handler.tag_raw_email")
    @patch("corderohq.receipt_handler.notify_success")
    @patch("corderohq.receipt_handler.process_attachment")
    @patch("corderohq.receipt_handler.parse_ses_email")
    @patch("corderohq.receipt_handler.fetch_raw_email", return_value=b"raw-email")
    @patch("corderohq.receipt_handler.get_ssm_param")
    def test_force_store_in_a_quoted_reply_is_ignored(
        self,
        mock_ssm: MagicMock,
        mock_fetch_email: MagicMock,
        mock_parse: MagicMock,
        mock_process: MagicMock,
        mock_notify_success: MagicMock,
        mock_tag: MagicMock,
    ) -> None:
        """The handler reads `top_body`, so a quoted rejection notification cannot set the flag."""
        mock_ssm.side_effect = lambda name: {"/test/api-key": "key", "/test/senders": "allowed@example.com"}[name]
        mock_parse.return_value = _make_parsed_email(
            body='Thanks\n> re-send the email with "FORCE_STORE" in the body.',
            top_body="Thanks",
        )
        mock_process.return_value = ProcessingResult(entries=[_sample_entry()], rejections=[], receipt_s3_uri="s3://x")

        from corderohq.receipt_handler import _handle

        _handle(_make_ses_event())

        assert mock_process.call_args[1]["force_store"] is False

    @patch.dict(os.environ, ENV_VARS)
    def test_spf_fail_returns_403(self) -> None:
        from corderohq.receipt_handler import _handle

        result = _handle(_make_ses_event(spf="FAIL"))
        assert result["statusCode"] == 403
        assert result["body"] == "Email authentication failed"

    @patch.dict(os.environ, ENV_VARS)
    def test_dkim_fail_returns_403(self) -> None:
        from corderohq.receipt_handler import _handle

        result = _handle(_make_ses_event(dkim="FAIL"))
        assert result["statusCode"] == 403
        assert result["body"] == "Email authentication failed"

    @patch.dict(os.environ, ENV_VARS)
    def test_spf_gray_returns_403(self) -> None:
        from corderohq.receipt_handler import _handle

        result = _handle(_make_ses_event(spf="GRAY"))
        assert result["statusCode"] == 403
        assert result["body"] == "Email authentication failed"

    @patch.dict(os.environ, ENV_VARS)
    @patch("corderohq.receipt_handler.notify_detailed_failure")
    @patch("corderohq.receipt_handler._handle", side_effect=RuntimeError("boom"))
    def test_process_receipt_catches_exceptions(self, mock_handle: MagicMock, mock_notify_detailed: MagicMock) -> None:
        from corderohq.receipt_handler import process_receipt

        result = process_receipt(_make_ses_event(), None)
        assert result["statusCode"] == 500
        mock_notify_detailed.assert_called_once()
        assert isinstance(mock_notify_detailed.call_args[0][1], RuntimeError)

    @patch.dict(os.environ, ENV_VARS)
    @patch("corderohq.receipt_handler.tag_raw_email")
    @patch("corderohq.receipt_handler.parse_ses_email")
    @patch("corderohq.receipt_handler.fetch_raw_email", return_value=b"raw")
    @patch("corderohq.receipt_handler.get_ssm_param")
    def test_unauthorized_sender_returns_403(
        self,
        mock_ssm: MagicMock,
        mock_fetch: MagicMock,
        mock_parse: MagicMock,
        mock_tag: MagicMock,
    ) -> None:
        mock_ssm.side_effect = lambda name: {"/test/api-key": "key", "/test/senders": "allowed@example.com"}[name]
        mock_parse.return_value = _make_parsed_email(sender="intruder@evil.com")

        from corderohq.receipt_handler import _handle

        result = _handle(_make_ses_event())
        assert result["statusCode"] == 403
        mock_tag.assert_not_called()

    @patch.dict(os.environ, ENV_VARS)
    @patch("corderohq.receipt_handler.tag_raw_email")
    @patch("corderohq.receipt_handler.parse_ses_email")
    @patch("corderohq.receipt_handler.fetch_raw_email", return_value=b"raw")
    @patch("corderohq.receipt_handler.get_ssm_param")
    def test_no_attachments_returns_400(
        self,
        mock_ssm: MagicMock,
        mock_fetch: MagicMock,
        mock_parse: MagicMock,
        mock_tag: MagicMock,
    ) -> None:
        mock_ssm.side_effect = lambda name: {"/test/api-key": "key", "/test/senders": "allowed@example.com"}[name]
        mock_parse.return_value = _make_parsed_email(attachments=[])

        from corderohq.receipt_handler import _handle

        result = _handle(_make_ses_event())
        assert result["statusCode"] == 400

    @patch.dict(os.environ, ENV_VARS)
    @patch("corderohq.receipt_handler.tag_raw_email")
    @patch("corderohq.receipt_handler.notify_rejection")
    @patch("corderohq.receipt_handler.process_attachment")
    @patch("corderohq.receipt_handler.parse_ses_email")
    @patch("corderohq.receipt_handler.fetch_raw_email", return_value=b"raw")
    @patch("corderohq.receipt_handler.get_ssm_param")
    def test_rejection_sends_notification(
        self,
        mock_ssm: MagicMock,
        mock_fetch: MagicMock,
        mock_parse: MagicMock,
        mock_process: MagicMock,
        mock_reject: MagicMock,
        mock_tag: MagicMock,
    ) -> None:
        mock_ssm.side_effect = lambda name: {"/test/api-key": "key", "/test/senders": "allowed@example.com"}[name]
        mock_parse.return_value = _make_parsed_email()
        mock_process.return_value = ProcessingResult(
            entries=[],
            rejections=[RejectionDetail("receipt.jpg", "Not eligible", "Not HSA")],
            receipt_s3_uri=None,
        )

        from corderohq.receipt_handler import _handle

        result = _handle(_make_ses_event())
        assert result["statusCode"] == 200
        mock_reject.assert_called_once_with("receipt.jpg", "Not eligible", "Not HSA")

    @patch.dict(os.environ, ENV_VARS)
    @patch("corderohq.receipt_handler.tag_raw_email")
    @patch("corderohq.receipt_handler.notify_detailed_failure")
    @patch("corderohq.receipt_handler.notify_failure")
    @patch("corderohq.receipt_handler.process_attachment", side_effect=RuntimeError("API failed"))
    @patch("corderohq.receipt_handler.parse_ses_email")
    @patch("corderohq.receipt_handler.fetch_raw_email", return_value=b"raw")
    @patch("corderohq.receipt_handler.get_ssm_param")
    def test_attachment_error_sends_failure_notification(
        self,
        mock_ssm: MagicMock,
        mock_fetch: MagicMock,
        mock_parse: MagicMock,
        mock_process: MagicMock,
        mock_notify_failure: MagicMock,
        mock_notify_detailed: MagicMock,
        mock_tag: MagicMock,
    ) -> None:
        mock_ssm.side_effect = lambda name: {"/test/api-key": "key", "/test/senders": "allowed@example.com"}[name]
        mock_parse.return_value = _make_parsed_email()

        from corderohq.receipt_handler import _handle

        result = _handle(_make_ses_event())
        assert result["statusCode"] == 500
        assert "receipt.jpg" in result["body"]
        mock_notify_failure.assert_called_once()
        mock_notify_detailed.assert_called_once()
        assert isinstance(mock_notify_detailed.call_args[0][1], RuntimeError)
        mock_tag.assert_called_once()


class TestWebUploadPath:
    @patch.dict(os.environ, ENV_VARS)
    @patch("corderohq.receipt_handler.process_attachment")
    @patch("corderohq.receipt_handler.fetch_upload", return_value=b"file-data")
    @patch("corderohq.receipt_handler.get_ssm_param", return_value="api-key-123")
    def test_happy_path(
        self,
        mock_ssm: MagicMock,
        mock_fetch: MagicMock,
        mock_process: MagicMock,
    ) -> None:
        mock_process.return_value = ProcessingResult(
            entries=[_sample_entry()],
            rejections=[],
            receipt_s3_uri="s3://test-bucket/receipts/2025/receipt.pdf",
        )

        from corderohq.receipt_handler import _handle

        result = _handle(_make_web_upload_event())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["entries"]) == 1
        assert body["entries"][0]["provider"] == "Dr Smith"
        assert body["receipt_s3_uri"] == "s3://test-bucket/receipts/2025/receipt.pdf"
        mock_fetch.assert_called_once_with("test-bucket", "raw-uploads/abc.pdf")
        mock_process.assert_called_once()

    @patch.dict(os.environ, ENV_VARS)
    @patch("corderohq.receipt_handler.process_attachment")
    @patch("corderohq.receipt_handler.fetch_upload", return_value=b"file-data")
    @patch("corderohq.receipt_handler.get_ssm_param", return_value="api-key-123")
    def test_returns_rejections(
        self,
        mock_ssm: MagicMock,
        mock_fetch: MagicMock,
        mock_process: MagicMock,
    ) -> None:
        mock_process.return_value = ProcessingResult(
            entries=[],
            rejections=[RejectionDetail("receipt.pdf", "Not eligible", "Not HSA related")],
            receipt_s3_uri=None,
        )

        from corderohq.receipt_handler import _handle

        result = _handle(_make_web_upload_event())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["rejections"]) == 1
        assert body["rejections"][0]["reasoning"] == "Not HSA related"

    @patch.dict(os.environ, ENV_VARS)
    @patch("corderohq.receipt_handler.process_attachment", side_effect=RuntimeError("boom"))
    @patch("corderohq.receipt_handler.fetch_upload", return_value=b"file-data")
    @patch("corderohq.receipt_handler.get_ssm_param", return_value="api-key-123")
    @patch("corderohq.receipt_handler.notify_detailed_failure")
    def test_error_returns_500(
        self,
        mock_notify: MagicMock,
        mock_ssm: MagicMock,
        mock_fetch: MagicMock,
        mock_process: MagicMock,
    ) -> None:
        from corderohq.receipt_handler import process_receipt

        result = process_receipt(_make_web_upload_event(), None)
        assert result["statusCode"] == 500

    @patch.dict(os.environ, ENV_VARS)
    @patch("corderohq.receipt_handler.process_attachment")
    @patch("corderohq.receipt_handler.fetch_upload", return_value=b"file-data")
    @patch("corderohq.receipt_handler.get_ssm_param", return_value="api-key-123")
    def test_store_only_passes_flag_and_returns_uri(
        self,
        mock_ssm: MagicMock,
        mock_fetch: MagicMock,
        mock_process: MagicMock,
    ) -> None:
        mock_process.return_value = ProcessingResult(
            entries=[],
            rejections=[],
            receipt_s3_uri="s3://test-bucket/receipts/2025/manual.pdf",
        )

        from corderohq.receipt_handler import _handle

        result = _handle(_make_web_upload_event(store_only=True))

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["receipt_s3_uri"] == "s3://test-bucket/receipts/2025/manual.pdf"
        assert body["entries"] == []
        assert body["rejections"] == []
        mock_process.assert_called_once()
        call_kwargs = mock_process.call_args[1]
        assert call_kwargs["store_only"] is True


class TestUtilities:
    def test_parse_date_valid_string(self) -> None:
        from datetime import date

        from corderohq.util import parse_date

        result = parse_date("2025-03-15")
        assert result == date(2025, 3, 15)

    def test_parse_date_none_returns_none(self) -> None:
        from corderohq.util import parse_date

        assert parse_date(None) is None

    def test_today_returns_utc_date(self) -> None:
        from corderohq.util import today

        result = today()
        assert result == datetime.now(tz=UTC).date()
