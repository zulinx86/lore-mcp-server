# lore-mcp-server

MCP server for searching and reading [lore.kernel.org](https://lore.kernel.org) mailing list archives.

No external dependencies beyond [FastMCP](https://gofastmcp.com) — all
lore.kernel.org interactions use the standard library (`urllib`,
`email`, `mailbox`).

## Tools

### lore_search

Search lore.kernel.org using Xapian query syntax. Returns lightweight
results (no message bodies) via the Atom feed API.

```
lore_search(
    query='bs:"guest_memfd" d:20260401..',
    mailing_list="kvm",
)
```

Supports public-inbox search prefixes: `bs:` (subject+body), `s:` (subject),
`f:` (from), `d:` (date range), `dfn:` (diff filename), and more.

Use `lore_get_message` to fetch the full content of a specific result.

### lore_get_message

Fetch the full content of a single message by URL.

```
lore_get_message(url="https://lore.kernel.org/kvm/20260420154720.29012-1-itazur@amazon.com/")
```

### lore_get_thread

Fetch all messages in a thread by URL (with full bodies).

```
lore_get_thread(url="https://lore.kernel.org/kvm/20260420154720.29012-1-itazur@amazon.com/")
```

### lore_list_thread_structure

List the structure of a thread without message bodies. Returns only
headers and reply relationships (`in_reply_to`) for each message.
Use this to get an overview of a large thread before fetching specific
messages with `lore_get_message`.

The underlying mbox data is cached in-memory, so subsequent calls to
`lore_get_thread` or `lore_get_message` for the same thread are served
from cache without additional network requests.

```
lore_list_thread_structure(url="https://lore.kernel.org/kvm/20260420154720.29012-1-itazur@amazon.com/")
```

## Usage

### With uvx (recommended)

```json
{
  "mcpServers": {
    "lore": {
      "command": "uvx",
      "args": ["lore-mcp-server"]
    }
  }
}
```

### From source

```bash
git clone <repo-url>
cd lore-mcp-server
uv run lore-mcp-server
```

## License

Apache-2.0