"""Tests for email_parser module."""

from collections.abc import Callable

from corderohq.aws.ses import parse_ses_email


def test_parse_simple_text_email(make_mime_email: Callable[..., bytes]) -> None:
    raw = make_mime_email(body="Hello world")
    parsed = parse_ses_email(raw)
    assert parsed.body.strip() == "Hello world"
    assert parsed.attachments == []


def test_parse_email_extracts_sender_and_subject(make_mime_email: Callable[..., bytes]) -> None:
    raw = make_mime_email(sender="alice@example.com", subject="My Receipt")
    parsed = parse_ses_email(raw)
    assert "alice@example.com" in parsed.sender
    assert parsed.subject == "My Receipt"


def test_parse_email_with_jpeg_attachment(make_mime_email: Callable[..., bytes]) -> None:
    raw = make_mime_email(attachments=[("photo.jpg", "image/jpeg", b"jpeg-data")])
    parsed = parse_ses_email(raw)
    assert len(parsed.attachments) == 1
    assert parsed.attachments[0].content_type == "image/jpeg"
    assert parsed.attachments[0].filename == "photo.jpg"
    assert parsed.attachments[0].data == b"jpeg-data"


def test_parse_email_with_pdf_attachment(make_mime_email: Callable[..., bytes]) -> None:
    raw = make_mime_email(attachments=[("receipt.pdf", "application/pdf", b"pdf-data")])
    parsed = parse_ses_email(raw)
    assert len(parsed.attachments) == 1
    assert parsed.attachments[0].content_type == "application/pdf"


def test_parse_email_with_multiple_attachments(make_mime_email: Callable[..., bytes]) -> None:
    raw = make_mime_email(
        attachments=[
            ("photo.jpg", "image/jpeg", b"jpeg-data"),
            ("doc.pdf", "application/pdf", b"pdf-data"),
            ("scan.png", "image/png", b"png-data"),
        ]
    )
    parsed = parse_ses_email(raw)
    assert len(parsed.attachments) == 3
    content_types = {a.content_type for a in parsed.attachments}
    assert content_types == {"image/jpeg", "application/pdf", "image/png"}


def test_parse_email_ignores_unsupported_content_types(make_mime_email: Callable[..., bytes]) -> None:
    raw = make_mime_email(
        attachments=[
            ("archive.zip", "application/zip", b"zip-data"),
            ("photo.jpg", "image/jpeg", b"jpeg-data"),
        ]
    )
    parsed = parse_ses_email(raw)
    assert len(parsed.attachments) == 1
    assert parsed.attachments[0].content_type == "image/jpeg"


def test_parse_email_body_not_overwritten_by_attachment(make_mime_email: Callable[..., bytes]) -> None:
    raw = make_mime_email(
        body="Original body",
        attachments=[("photo.jpg", "image/jpeg", b"jpeg-data")],
    )
    parsed = parse_ses_email(raw)
    assert parsed.body.strip() == "Original body"
    assert len(parsed.attachments) == 1


def test_parse_email_missing_headers() -> None:
    from email.message import EmailMessage

    msg = EmailMessage()
    msg.set_content("body")
    raw = msg.as_bytes()
    parsed = parse_ses_email(raw)
    assert parsed.sender == ""
    assert parsed.subject == ""
    assert parsed.body.strip() == "body"


def test_parse_html_only_email_body() -> None:
    """An HTML-only email must still yield its words.

    A mail client that sends no text/plain part left the body empty before. The FORCE_STORE
    flag lives in the body, so an empty body silently turned the flag off.
    """
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg.set_content("<html><body><p>FORCE_STORE</p></body></html>", subtype="html")
    parsed = parse_ses_email(msg.as_bytes())
    assert "FORCE_STORE" in parsed.body


def test_parse_html_body_joins_a_word_split_across_tags() -> None:
    """A mail client can wrap part of a word in a tag. The word must survive."""
    from email.message import EmailMessage

    msg = EmailMessage()
    msg.set_content("<p>FORCE<span>_STORE</span></p>", subtype="html")
    parsed = parse_ses_email(msg.as_bytes())
    assert "FORCE_STORE" in parsed.body


def test_parse_html_body_unescapes_entities() -> None:
    from email.message import EmailMessage

    msg = EmailMessage()
    msg.set_content("<p>Caf&eacute; &amp; Clinic</p>", subtype="html")
    parsed = parse_ses_email(msg.as_bytes())
    assert "Café & Clinic" in parsed.body


def test_parse_multipart_alternative_keeps_both_parts() -> None:
    """A plain part and an HTML part both reach the body."""
    from email.message import EmailMessage

    msg = EmailMessage()
    msg.set_content("FORCE_STORE")
    msg.add_alternative("<p>FORCE_STORE</p>", subtype="html")
    parsed = parse_ses_email(msg.as_bytes())
    assert parsed.body.count("FORCE_STORE") == 2


def test_parse_later_text_part_does_not_overwrite_an_earlier_one() -> None:
    """Two text/plain parts both reach the body. An assignment kept the last one only."""
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    msg = MIMEMultipart("mixed")
    msg.attach(MIMEText("FORCE_STORE", "plain"))
    msg.attach(MIMEText("Sent from my phone", "plain"))
    parsed = parse_ses_email(msg.as_bytes())
    assert "FORCE_STORE" in parsed.body
    assert "Sent from my phone" in parsed.body


def test_top_body_drops_a_standard_quoted_reply(make_mime_email: Callable[..., bytes]) -> None:
    raw = make_mime_email(
        body='Please archive this.\n\n> Your receipt was not eligible.\n> Re-send with "FORCE_STORE".\n'
    )
    parsed = parse_ses_email(raw)
    assert "FORCE_STORE" in parsed.body
    assert "FORCE_STORE" not in parsed.top_body
    assert "Please archive this." in parsed.top_body


def test_top_body_drops_a_gmail_attribution_quote(make_mime_email: Callable[..., bytes]) -> None:
    raw = make_mime_email(
        body="No comment.\n\nOn Mon, 13 Oct 2026 at 09:14, HSA <receipts@hsa.corderohq.com> wrote:\n"
        'Re-send the email with "FORCE_STORE" in the body.\n'
    )
    parsed = parse_ses_email(raw)
    assert "FORCE_STORE" not in parsed.top_body
    assert "No comment." in parsed.top_body


def test_top_body_drops_an_outlook_original_message_quote(make_mime_email: Callable[..., bytes]) -> None:
    raw = make_mime_email(body='Hello\n\n-----Original Message-----\nUse "FORCE_STORE" to archive it.\n')
    parsed = parse_ses_email(raw)
    assert "FORCE_STORE" not in parsed.top_body
    assert "Hello" in parsed.top_body


def test_top_body_drops_an_outlook_header_block(make_mime_email: Callable[..., bytes]) -> None:
    raw = make_mime_email(
        body="Hello\n\nFrom: HSA <receipts@hsa.corderohq.com>\nSent: Monday\nTo: Me\n"
        'Subject: Not eligible\n\nUse "FORCE_STORE".\n'
    )
    parsed = parse_ses_email(raw)
    assert "FORCE_STORE" not in parsed.top_body
    assert "Hello" in parsed.top_body


def test_top_body_keeps_a_from_line_that_is_ordinary_prose(make_mime_email: Callable[..., bytes]) -> None:
    """A `From:` line starts a quote only when another header follows it."""
    raw = make_mime_email(body="From: the clinic on Main Street\nFORCE_STORE\n")
    parsed = parse_ses_email(raw)
    assert "FORCE_STORE" in parsed.top_body


def test_top_body_drops_an_html_blockquote() -> None:
    from email.message import EmailMessage

    msg = EmailMessage()
    msg.set_content(
        "<p>Please archive this.</p><blockquote><p>Re-send with FORCE_STORE.</p></blockquote>",
        subtype="html",
    )
    parsed = parse_ses_email(msg.as_bytes())
    assert "FORCE_STORE" in parsed.body
    assert "FORCE_STORE" not in parsed.top_body
    assert "Please archive this." in parsed.top_body


def test_top_body_drops_a_gmail_quote_div() -> None:
    from email.message import EmailMessage

    msg = EmailMessage()
    msg.set_content(
        '<p>Keep it.</p><div class="gmail_quote"><p>Re-send with FORCE_STORE.</p></div>',
        subtype="html",
    )
    parsed = parse_ses_email(msg.as_bytes())
    assert "FORCE_STORE" not in parsed.top_body
    assert "Keep it." in parsed.top_body


def test_top_body_keeps_the_flag_when_the_sender_writes_it(make_mime_email: Callable[..., bytes]) -> None:
    """The case this whole feature exists for must still work."""
    raw = make_mime_email(body="FORCE_STORE\n\n> Your receipt was not eligible.\n")
    parsed = parse_ses_email(raw)
    assert "FORCE_STORE" in parsed.top_body


def test_top_body_equals_body_when_there_is_no_quote(make_mime_email: Callable[..., bytes]) -> None:
    raw = make_mime_email(body="Just a receipt")
    parsed = parse_ses_email(raw)
    assert parsed.top_body.strip() == parsed.body.strip() == "Just a receipt"


# ProtonMail is the mail client in daily use, so its reply shape gets its own tests.
# It composes in HTML, and it holds the whole body on one line.
_NOTIFICATION = 'Your receipt was not HSA-eligible. Send the email again and write "FORCE_STORE" at the top.'

_PROTONMAIL_HTML_REPLY = (
    "<div>Please archive this one.</div>"
    '<div class="protonmail_signature_block"><div>Sent with Proton Mail secure email.</div></div>'
    "<div><br></div>"
    '<div class="protonmail_quote">------- Original Message -------<br>'
    "On Tuesday, October 14th, 2026 at 9:14 AM, HSA &lt;receipts@hsa.corderohq.com&gt; wrote:<br>"
    f'<blockquote class="protonmail_quote" type="cite">{_NOTIFICATION}</blockquote></div>'
)

_PROTONMAIL_PLAIN_REPLY = (
    "Please archive this one.\n\nSent with Proton Mail secure email.\n\n"
    "------- Original Message -------\n"
    "On Tuesday, October 14th, 2026 at 9:14 AM, HSA <receipts@hsa.corderohq.com> wrote:\n\n"
    f"> {_NOTIFICATION}\n"
)


def _html_email(markup: str) -> bytes:
    from email.message import EmailMessage

    msg = EmailMessage()
    msg.set_content(markup, subtype="html")
    return msg.as_bytes()


def test_protonmail_html_reply_drops_the_quote() -> None:
    parsed = parse_ses_email(_html_email(_PROTONMAIL_HTML_REPLY))
    assert "FORCE_STORE" in parsed.body
    assert "FORCE_STORE" not in parsed.top_body
    assert "Please archive this one." in parsed.top_body


def test_protonmail_plain_reply_drops_the_quote(make_mime_email: Callable[..., bytes]) -> None:
    parsed = parse_ses_email(make_mime_email(body=_PROTONMAIL_PLAIN_REPLY))
    assert "FORCE_STORE" not in parsed.top_body
    assert "Please archive this one." in parsed.top_body


def test_protonmail_multipart_reply_drops_the_quote() -> None:
    from email.message import EmailMessage

    msg = EmailMessage()
    msg.set_content(_PROTONMAIL_PLAIN_REPLY)
    msg.add_alternative(_PROTONMAIL_HTML_REPLY, subtype="html")
    parsed = parse_ses_email(msg.as_bytes())
    assert "FORCE_STORE" not in parsed.top_body
    assert "Please archive this one." in parsed.top_body


def test_protonmail_reply_keeps_the_flag_the_sender_wrote() -> None:
    """The case the feature exists for, in the client actually in use."""
    markup = (
        "<div>FORCE_STORE</div>"
        '<div class="protonmail_quote">------- Original Message -------'
        f"<blockquote>{_NOTIFICATION}</blockquote></div>"
    )
    parsed = parse_ses_email(_html_email(markup))
    assert "FORCE_STORE" in parsed.top_body


def test_html_block_tags_become_separate_lines() -> None:
    """The quote search reads the body line by line, and an HTML body can be one long line."""
    parsed = parse_ses_email(_html_email("<div>Hello</div><div>FORCE_STORE</div>"))
    assert parsed.top_body.splitlines()[:2] == ["Hello", "FORCE_STORE"]
