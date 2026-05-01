"""lore-mcp-server: MCP tools for lore.kernel.org.

Built on top of liblore (https://pypi.org/project/liblore/) for all
interactions with lore.kernel.org / public-inbox.
"""

import logging
from email.message import EmailMessage

from fastmcp import FastMCP
from liblore import LoreNode
from liblore.utils import (
    get_clean_msgid,
    get_msgid_from_url,
    msg_get_author,
    msg_get_payload,
    msg_get_subject,
    parse_message,
)
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

mcp = FastMCP(name="lore-mcp-server")

# Cache mbox data for 10 minutes to avoid redundant fetches
CACHE_DIR = "/tmp/lore-mcp-cache"
CACHE_TTL = 600


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


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
# Internal helpers
# ---------------------------------------------------------------------------


def _get_node(mailing_list: str = "all") -> LoreNode:
    """Create a LoreNode for the given mailing list."""
    url = f"https://lore.kernel.org/{mailing_list}"
    node = LoreNode(url, cache_dir=CACHE_DIR, cache_ttl=CACHE_TTL)
    node.set_user_agent("lore-mcp-server", "0.1.0")
    return node


def _to_lore_message(msg: EmailMessage) -> LoreMessage:
    """Convert an EmailMessage to a LoreMessage model."""
    name, addr = msg_get_author(msg)
    return LoreMessage(
        message_id=get_clean_msgid(msg),
        in_reply_to=get_clean_msgid(msg, header="In-Reply-To"),
        subject=msg_get_subject(msg),
        from_name=name,
        from_email=addr,
        date=msg.get("Date", ""),
        body=msg_get_payload(msg),
    )


def _to_thread_entry(msg: EmailMessage) -> LoreThreadEntry:
    """Convert an EmailMessage to a lightweight thread entry."""
    name, addr = msg_get_author(msg)
    return LoreThreadEntry(
        message_id=get_clean_msgid(msg),
        in_reply_to=get_clean_msgid(msg, header="In-Reply-To"),
        subject=msg_get_subject(msg),
        from_name=name,
        from_email=addr,
        date=msg.get("Date", ""),
    )


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool
def lore_search(
    query: str,
    mailing_list: str = "all",
) -> list[LoreMessage]:
    """Search lore.kernel.org using Xapian query syntax.

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
        A list of LoreMessage objects for each matching message,
        deduplicated and sorted by date.
    """
    with _get_node(mailing_list) as node:
        msgs = node.get_thread_by_query(query)

    return [_to_lore_message(msg) for msg in msgs]


@mcp.tool
def lore_get_message(url: str) -> LoreMessage:
    """Fetch a single message from lore.kernel.org by URL.

    Args:
        url: Full URL of the message on lore.kernel.org, e.g.
            https://lore.kernel.org/kvm/20260420154720.29012-1-itazur@amazon.com/

    Returns:
        A LoreMessage with message headers and plain-text body.
    """
    msgid = get_msgid_from_url(url)
    with _get_node() as node:
        raw = node.get_message_by_msgid(msgid)

    msg = parse_message(raw)
    return _to_lore_message(msg)


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
    msgid = get_msgid_from_url(url)
    with _get_node() as node:
        msgs = node.get_thread_by_msgid(msgid, strict=True, sort=sort)

    return [_to_lore_message(msg) for msg in msgs]


@mcp.tool
def lore_list_thread_structure(
    url: str,
    sort: bool = True,
) -> list[LoreThreadEntry]:
    """List the structure of a thread without message bodies.

    Returns only headers and reply relationships for each message in
    the thread.  Use this to get an overview of a large thread before
    fetching specific messages with lore_get_message.

    The underlying mbox data is cached, so subsequent calls to
    lore_get_thread or lore_get_message for the same thread will be
    served from cache without additional network requests.

    Args:
        url: URL of any message in the thread on lore.kernel.org.
        sort: Sort messages by received timestamp. Defaults to True.

    Returns:
        A list of LoreThreadEntry objects with headers, in_reply_to,
        and reply_count (no message bodies).
    """
    msgid = get_msgid_from_url(url)
    with _get_node() as node:
        msgs = node.get_thread_by_msgid(msgid, strict=True, sort=sort)

    return [_to_thread_entry(msg) for msg in msgs]
