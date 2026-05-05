"""lore-mcp-server: MCP tools for lore.kernel.org.

Provides MCP tools for searching and reading Linux kernel mailing
list archives hosted on lore.kernel.org / public-inbox.
"""

import email
import gzip
import logging
import mailbox
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.message import EmailMessage
from email.utils import parseaddr
from functools import lru_cache

from fastmcp import FastMCP
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

mcp = FastMCP(name="lore-mcp-server")

LORE_BASE = "https://lore.kernel.org"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class LoreSearchResult(BaseModel):
    """A single search hit from lore.kernel.org (no message body)."""

    url: str = Field(description="URL of the message on lore.kernel.org.")
    subject: str = Field(description="Subject line of the email.")
    from_name: str = Field(description="Display name of the sender.")
    from_email: str = Field(description="Email address of the sender.")
    date: str = Field(description="Last-updated timestamp (ISO 8601).")


class LoreMessage(BaseModel):
    """A single email message from lore.kernel.org."""

    message_id: str = Field(description="Unique Message-ID of the email.")
    in_reply_to: str | None = Field(
        description="Message-ID of the parent message, or null for the thread root.",
    )
    subject: str = Field(description="Subject line of the email.")
    from_name: str = Field(description="Display name of the sender.")
    from_email: str = Field(description="Email address of the sender.")
    date: str = Field(description="Date header value (RFC 2822 format).")
    body: str = Field(description="Plain-text body of the email.")


class LoreThreadEntry(BaseModel):
    """Lightweight summary of a message within a thread (no body)."""

    message_id: str = Field(description="Unique Message-ID of the email.")
    in_reply_to: str | None = Field(
        description="Message-ID of the parent message, or null for the thread root.",
    )
    subject: str = Field(description="Subject line of the email.")
    from_name: str = Field(description="Display name of the sender.")
    from_email: str = Field(description="Email address of the sender.")
    date: str = Field(description="Date header value (RFC 2822 format).")


# ---------------------------------------------------------------------------
# Internal helpers — HTTP
# ---------------------------------------------------------------------------


_USER_AGENT = "lore-mcp-server/0.1.0"


def _http_get(url: str, timeout: int = 30) -> bytes:
    """Perform an HTTP GET and return the response body as bytes.

    Args:
        url: The URL to fetch.
        timeout: Request timeout in seconds. Defaults to 30.

    Returns:
        The response body as raw bytes.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ---------------------------------------------------------------------------
# Internal helpers — Atom feed (search)
# ---------------------------------------------------------------------------


_ATOM_NS = "{http://www.w3.org/2005/Atom}"


def _fetch_atom_page(mailing_list: str, query: str, offset: int = 0) -> list[LoreSearchResult]:
    """Fetch one page of Atom search results from lore.kernel.org.

    Args:
        mailing_list: Mailing list name (e.g. "kvm", "all").
        query: Xapian search query string.
        offset: Result offset for pagination. Defaults to 0.

    Returns:
        A list of LoreSearchResult objects for this page, or an empty
        list if no results or an error occurred.
    """
    params = {
        "q": query,   # Xapian search query
        "x": "A",     # output format: Atom
        "o": str(offset),  # result offset for pagination
    }
    url = f"{LORE_BASE}/{mailing_list}/?" + urllib.parse.urlencode(params)
    try:
        data = _http_get(url)
    except urllib.error.HTTPError as exc:
        log.warning("Atom feed HTTP error %d for query: %s", exc.code, query)
        return []
    except urllib.error.URLError as exc:
        log.warning("Atom feed URL error for query %s: %s", query, exc.reason)
        return []

    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []

    results: list[LoreSearchResult] = []
    # Atom entry structure:
    #   <entry>
    #     <author><name>...</name><email>...</email></author>
    #     <title>...</title>
    #     <updated>2026-04-20T15:47:30Z</updated>
    #     <link href="https://lore.kernel.org/all/{msgid}/"/>
    #   </entry>
    for entry in root.findall(f"{_ATOM_NS}entry"):
        title_el = entry.find(f"{_ATOM_NS}title")
        link_el = entry.find(f"{_ATOM_NS}link")
        updated_el = entry.find(f"{_ATOM_NS}updated")
        author_el = entry.find(f"{_ATOM_NS}author")

        author_name = ""
        author_email = ""
        if author_el:
            name_el = author_el.find(f"{_ATOM_NS}name")
            if name_el and name_el.text:
                author_name = name_el.text
            email_el = author_el.find(f"{_ATOM_NS}email")
            if email_el and email_el.text:
                author_email = email_el.text

        results.append(LoreSearchResult(
            url=link_el.get("href", "") if link_el else "",
            subject=title_el.text if title_el and title_el.text else "(no subject)",
            from_name=author_name,
            from_email=author_email,
            date=updated_el.text if updated_el and updated_el.text else "",
        ))
    return results


def _search_atom(mailing_list: str, query: str) -> list[LoreSearchResult]:
    """Paginate through all Atom search results.

    Args:
        mailing_list: Mailing list name (e.g. "kvm", "all").
        query: Xapian search query string.

    Returns:
        All matching LoreSearchResult objects across all pages.
    """
    all_results: list[LoreSearchResult] = []
    offset = 0
    while True:
        page = _fetch_atom_page(mailing_list, query, offset)
        if not page:
            break
        all_results.extend(page)
        offset += len(page)
    return all_results


# ---------------------------------------------------------------------------
# Internal helpers — message parsing
# ---------------------------------------------------------------------------


def _clean_msgid(raw: str | None) -> str | None:
    """Extract a bare message-id from angle-bracket format.

    Args:
        raw: Raw header value, possibly wrapped in angle brackets.

    Returns:
        The bare message-id string, or None if input is empty.
    """
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("<") and raw.endswith(">"):
        return raw[1:-1]
    return raw


def _get_msgid_from_url(url: str) -> str:
    """Extract a message-id from a lore.kernel.org URL.

    Args:
        url: Full lore.kernel.org message URL.

    Returns:
        The message-id extracted from the URL path.

    Raises:
        ValueError: If the URL path cannot be parsed.
    """
    # Path is /{list}/{msgid}/ — the message-id is always the last non-empty segment
    parts = urllib.parse.urlparse(url).path.strip("/").split("/")
    if not parts or not parts[-1]:
        raise ValueError(f"Cannot extract message-id from URL: {url}")
    return parts[-1]


def _get_body(msg: EmailMessage) -> str:
    """Extract the plain-text body from an EmailMessage.

    Args:
        msg: Parsed EmailMessage object.

    Returns:
        The plain-text body as a string, or empty string if none found.
    """
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        return ""
    payload = msg.get_payload(decode=True)
    if payload is None:
        return ""
    charset = msg.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


# ---------------------------------------------------------------------------
# Internal helpers — model conversion
# ---------------------------------------------------------------------------


def _msg_to_lore_message(msg: EmailMessage) -> LoreMessage:
    """Convert an EmailMessage to a LoreMessage model.

    Args:
        msg: Parsed EmailMessage object.

    Returns:
        A LoreMessage with all fields populated from the message.
    """
    name, addr = parseaddr(msg.get("From", ""))
    try:
        body = _get_body(msg)
    except Exception as exc:
        log.warning("Failed to decode body: %s", exc)
        body = ""
    return LoreMessage(
        message_id=_clean_msgid(msg.get("Message-ID")) or "",
        in_reply_to=_clean_msgid(msg.get("In-Reply-To")),
        subject=msg.get("Subject", ""),
        from_name=name,
        from_email=addr,
        date=msg.get("Date", ""),
        body=body,
    )


def _msg_to_thread_entry(msg: EmailMessage) -> LoreThreadEntry:
    """Convert an EmailMessage to a lightweight thread entry.

    Args:
        msg: Parsed EmailMessage object.

    Returns:
        A LoreThreadEntry with header fields only (no body).
    """
    name, addr = parseaddr(msg.get("From", ""))
    return LoreThreadEntry(
        message_id=_clean_msgid(msg.get("Message-ID")) or "",
        in_reply_to=_clean_msgid(msg.get("In-Reply-To")),
        subject=msg.get("Subject", ""),
        from_name=name,
        from_email=addr,
        date=msg.get("Date", ""),
    )


# ---------------------------------------------------------------------------
# Internal helpers — mbox / thread
# ---------------------------------------------------------------------------


def _split_mbox(mbox_bytes: bytes) -> list[EmailMessage]:
    """Split raw mbox bytes into individual EmailMessage objects.

    Args:
        mbox_bytes: Raw mbox content as bytes.

    Returns:
        A list of parsed EmailMessage objects.
    """
    # mailbox.mbox requires a file path, so write to a temporary file
    with tempfile.NamedTemporaryFile(suffix=".mbox") as tmp:
        tmp.write(mbox_bytes)
        tmp.flush()
        mbox = mailbox.mbox(tmp.name)
        return [email.message_from_bytes(msg.as_bytes()) for msg in mbox]


def _dedupe_msgs(msgs: list[EmailMessage]) -> list[EmailMessage]:
    """Remove duplicate messages by Message-ID.

    Args:
        msgs: List of EmailMessage objects, possibly with duplicates.

    Returns:
        Deduplicated list preserving original order.
    """
    seen: set[str] = set()
    unique: list[EmailMessage] = []
    for msg in msgs:
        msgid = _clean_msgid(msg.get("Message-ID")) or ""
        if msgid and msgid in seen:
            continue
        if msgid:
            seen.add(msgid)
        unique.append(msg)
    return unique


def _get_strict_thread(msgs: list[EmailMessage], root_msgid: str) -> list[EmailMessage]:
    """Filter messages to only those belonging to the thread rooted at root_msgid.

    Walks the References and In-Reply-To headers to build the set of
    message-ids that are part of the thread.

    Args:
        msgs: All messages from the mbox (may include unrelated messages).
        root_msgid: The message-id of the thread root.

    Returns:
        Only messages that belong to the specified thread.
    """
    # Build a set of all message-ids that belong to this thread
    thread_ids: set[str] = {root_msgid}
    # Multiple passes to catch transitive references
    changed = True
    while changed:
        changed = False
        for msg in msgs:
            msgid = _clean_msgid(msg.get("Message-ID")) or ""
            if msgid in thread_ids:
                continue
            # Check In-Reply-To
            irt = _clean_msgid(msg.get("In-Reply-To"))
            if irt and irt in thread_ids:
                thread_ids.add(msgid)
                changed = True
                continue
            # Check References
            refs = msg.get("References", "")
            for ref in refs.split():
                ref_id = _clean_msgid(ref)
                if ref_id and ref_id in thread_ids:
                    thread_ids.add(msgid)
                    changed = True
                    break

    return [m for m in msgs if _clean_msgid(m.get("Message-ID")) in thread_ids]


def _fetch_thread_mbox(msgid: str) -> list[EmailMessage]:
    """Fetch and parse the thread mbox for a given message-id.

    Results are cached so that lore_list_thread_structure followed by
    lore_get_thread (or vice versa) only downloads the mbox once.

    Args:
        msgid: Bare message-id (without angle brackets).

    Returns:
        Deduplicated list of EmailMessage objects from the thread.
    """
    data = _fetch_thread_mbox_gz(msgid)
    mbox_bytes = gzip.decompress(data)
    msgs = _split_mbox(mbox_bytes)
    return _dedupe_msgs(msgs)


@lru_cache(maxsize=32)
def _fetch_thread_mbox_gz(msgid: str) -> bytes:
    """Fetch the raw gzipped thread mbox (cached).

    Args:
        msgid: Bare message-id (without angle brackets).

    Returns:
        Raw gzipped mbox bytes from lore.kernel.org.
    """
    quoted = urllib.parse.quote_plus(msgid)
    url = f"{LORE_BASE}/all/{quoted}/t.mbox.gz"
    return _http_get(url)


@lru_cache(maxsize=64)
def _fetch_raw_message(msgid: str) -> EmailMessage:
    """Fetch a single raw message by message-id (cached).

    Args:
        msgid: Bare message-id (without angle brackets).

    Returns:
        Parsed EmailMessage object.
    """
    quoted = urllib.parse.quote_plus(msgid)
    url = f"{LORE_BASE}/all/{quoted}/raw"
    data = _http_get(url)
    return email.message_from_bytes(data)


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool
def lore_search(
    query: str,
    mailing_list: str = "all",
) -> list[LoreSearchResult]:
    """Search lore.kernel.org using Xapian query syntax.

    Returns lightweight search results (no message bodies) via the
    Atom feed API.  Use lore_get_message to fetch the full content of
    a specific result.

    The query supports public-inbox search prefixes:
      - bs:"term"  search subject + body
      - s:"term"   search subject only
      - f:addr     search by sender
      - d:YYYYMMDD..YYYYMMDD  date range
      - dfn:filename  search by diff filename

    Example queries:
      - 'bs:"guest_memfd" d:20260401..'
      - 's:"PATCH" f:alice@example.com d:2.weeks.ago..'

    Args:
        query: Xapian search query string.
        mailing_list: Mailing list name (e.g. "kvm", "linux-mm").
            Defaults to "all" which searches across all lists.

    Returns:
        A list of LoreSearchResult objects with URL, subject, author,
        and date for each matching message.
    """
    return _search_atom(mailing_list, query)


@mcp.tool
def lore_get_message(url: str) -> LoreMessage:
    """Fetch a single message from lore.kernel.org by URL.

    Args:
        url: Full URL of the message on lore.kernel.org, e.g.
            https://lore.kernel.org/kvm/20260420154720.29012-1-itazur@amazon.com/

    Returns:
        A LoreMessage with message headers and plain-text body.
    """
    msgid = _get_msgid_from_url(url)
    msg = _fetch_raw_message(msgid)
    return _msg_to_lore_message(msg)


@mcp.tool
def lore_get_thread(
    url: str,
    sort: bool = True,
) -> list[LoreMessage]:
    """Fetch all messages in a thread from lore.kernel.org.

    Given the URL of any message in a thread, retrieves the entire
    thread with strict filtering (only messages belonging to this
    thread are included).

    Args:
        url: URL of any message in the thread on lore.kernel.org.
        sort: Sort messages by received timestamp. Defaults to True.

    Returns:
        A list of LoreMessage objects, each with headers and body.
    """
    msgid = _get_msgid_from_url(url)
    msgs = _fetch_thread_mbox(msgid)
    msgs = _get_strict_thread(msgs, msgid)
    if sort:
        msgs.sort(key=lambda m: m.get("Date", ""))
    return [_msg_to_lore_message(m) for m in msgs]


@mcp.tool
def lore_list_thread_structure(
    url: str,
    sort: bool = True,
) -> list[LoreThreadEntry]:
    """List the structure of a thread without message bodies.

    Returns only headers and reply relationships for each message in
    the thread.  Use this to get an overview of a large thread before
    fetching specific messages with lore_get_message.

    Args:
        url: URL of any message in the thread on lore.kernel.org.
        sort: Sort messages by received timestamp. Defaults to True.

    Returns:
        A list of LoreThreadEntry objects with headers and in_reply_to
        (no message bodies).
    """
    msgid = _get_msgid_from_url(url)
    msgs = _fetch_thread_mbox(msgid)
    msgs = _get_strict_thread(msgs, msgid)
    if sort:
        msgs.sort(key=lambda m: m.get("Date", ""))
    return [_msg_to_thread_entry(m) for m in msgs]
