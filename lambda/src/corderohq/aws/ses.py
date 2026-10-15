"""Parse incoming SES emails and extract attachments."""

import email
import email.policy
import html
import re
from dataclasses import dataclass

SUPPORTED_CONTENT_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "application/pdf",
    }
)

# A mail client can send the body as plain text, as HTML, or as both. Read every form, because
# the body carries the FORCE_STORE flag. An HTML-only email left the body empty before.
_TEXT_CONTENT_TYPES = frozenset({"text/plain", "text/html"})

# A tag that ends a line becomes a newline. The quote search below reads the body line by line,
# and an HTML body can hold its whole text on one line.
_HTML_LINE_BREAK_PATTERN = re.compile(r"<br\s*/?>|</(?:p|div|li|tr|h[1-6]|blockquote)\s*>", re.IGNORECASE)

# Each remaining tag becomes an empty string, and not a space. A mail client can break a word
# across two tags, such as `FORCE<span>_STORE</span>`. An empty string joins that word again.
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")

# A reply quotes the message before it. The rejection notification tells the reader to send
# "FORCE_STORE", so a quoted copy of that notification holds the word. The sender did not write
# it, and it must not turn the flag on. These patterns mark where the quoted part starts.
#
# A reply puts the new words at the top, so `top_body` keeps the lines before the first mark.

# The element that holds the quote in an HTML body. ProtonMail, Gmail, Apple Mail, and Outlook
# each use a different one. ProtonMail puts its separator and its attribution line in the
# `protonmail_quote` div, above the blockquote, so the div must be in this list too.
_HTML_QUOTE_START_PATTERN = re.compile(
    r"<blockquote\b|<div[^>]*\b(?:protonmail_quote|gmail_quote|divRplyFwdMsg|appendonsend)\b",
    re.IGNORECASE,
)

# The line that starts the quote in a plain-text body.
_QUOTE_START_PATTERNS = (
    re.compile(r"^\s*>"),  # the standard quote mark
    re.compile(r"^\s*-{2,}\s*original message\s*-{2,}", re.IGNORECASE),  # Outlook
    re.compile(r"^\s*_{10,}\s*$"),  # the Outlook separator rule
    re.compile(r"^\s*on\b.{0,300}\bwrote:\s*$", re.IGNORECASE),  # Gmail, Apple Mail
)

# Outlook writes a header block and no quote mark. A `From:` line starts that block only when
# another header of the block follows it. A sender can write "From: the clinic" in a sentence.
_FROM_PATTERN = re.compile(r"^\s*from:\s*\S", re.IGNORECASE)
_FOLLOWING_HEADER_PATTERN = re.compile(r"^\s*(?:sent|date|to|subject):\s*\S", re.IGNORECASE)


@dataclass
class ParsedEmail:
    sender: str
    subject: str
    body: str
    """Every text part of the email, the quoted reply chain included."""
    top_body: str
    """The words that the sender wrote, without the quoted reply chain.

    Read a flag such as FORCE_STORE from here. `body` holds a quoted copy of an earlier
    notification, and that notification names the flag in its instructions.
    """
    attachments: list["Attachment"]


@dataclass
class Attachment:
    filename: str
    content_type: str
    data: bytes


def parse_ses_email(raw_email: bytes) -> ParsedEmail:
    """Parse a raw email from S3 into structured data."""
    msg = email.message_from_bytes(raw_email, policy=email.policy.default)

    sender = str(msg.get("From", ""))
    subject = str(msg.get("Subject", ""))

    # Collect every text part. An assignment here would keep the last part only, and a mail
    # client can put the words of the sender in an earlier part than the signature.
    body_parts: list[str] = []
    top_body_parts: list[str] = []
    attachments: list[Attachment] = []

    for part in msg.walk():
        content_type = part.get_content_type()
        disposition = str(part.get("Content-Disposition", ""))

        if content_type in _TEXT_CONTENT_TYPES and "attachment" not in disposition:
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                raw_text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                if content_type == "text/html":
                    # Cut the quote before the tags go, because the cut needs the tags.
                    body_parts.append(_strip_html(raw_text))
                    top_body_parts.append(_strip_html(_cut_at(raw_text, _HTML_QUOTE_START_PATTERN)))
                else:
                    body_parts.append(raw_text)
                    top_body_parts.append(raw_text)
        elif content_type in SUPPORTED_CONTENT_TYPES:
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                filename = part.get_filename() or f"attachment.{content_type.split('/')[-1]}"
                attachments.append(Attachment(filename=filename, content_type=content_type, data=payload))

    return ParsedEmail(
        sender=sender,
        subject=subject,
        body="\n".join(body_parts),
        top_body=_strip_quoted_lines("\n".join(top_body_parts)),
        attachments=attachments,
    )


def _strip_html(markup: str) -> str:
    """Remove the HTML tags from a body, so that a text search can read the words."""
    text = _HTML_LINE_BREAK_PATTERN.sub("\n", markup)
    return html.unescape(_HTML_TAG_PATTERN.sub("", text))


def _cut_at(text: str, pattern: re.Pattern[str]) -> str:
    """Return the text before the first match. Return all of it when nothing matches."""
    match = pattern.search(text)
    return text[: match.start()] if match else text


def _strip_quoted_lines(text: str) -> str:
    """Return the lines before the quoted reply chain starts."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if any(pattern.match(line) for pattern in _QUOTE_START_PATTERNS):
            return "\n".join(lines[:index])
        # Look ahead a few lines, because Outlook writes `From:` then `Sent:` then `To:`.
        if _FROM_PATTERN.match(line) and any(
            _FOLLOWING_HEADER_PATTERN.match(following) for following in lines[index + 1 : index + 4]
        ):
            return "\n".join(lines[:index])
    return text
