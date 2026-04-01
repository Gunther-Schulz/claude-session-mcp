# claude-session-mcp

Fork-aware MCP server for searching and navigating Claude Code session history.

## Why fork-aware?

Claude Code stores session history as JSONL files in `~/.claude/projects/`. These files are **trees**, not linear logs. Each record has a `uuid` and `parentUuid` — when a user retries or edits a prompt, two records share the same parent, creating a fork. Most tools ignore this and treat sessions as flat logs, giving incorrect results.

This server correctly parses the tree structure, detects forks, identifies primary vs abandoned branches, and stitches compaction boundaries back together.

## Installation

Requires [uv](https://docs.astral.sh/uv/) (available via most package managers, e.g. `pacman -S uv` on Arch/CachyOS, `brew install uv` on macOS).

### Install from GitHub

```bash
uv tool install git+https://github.com/Gunther-Schulz/claude-session-mcp
```

This creates an isolated environment and puts `claude-session-mcp` on your PATH. Update with:

```bash
uv tool upgrade claude-session-mcp
```

### Alternative: run without installing

```bash
uvx --from git+https://github.com/Gunther-Schulz/claude-session-mcp claude-session-mcp
```

This resolves dependencies on each launch (slower startup, but no install step).

## Configuration

Add to your Claude Code settings (`~/.claude/settings.json`):

```json
{
  "mcpServers": {
    "session-search": {
      "command": "claude-session-mcp"
    }
  }
}
```

If `claude-session-mcp` is not on your PATH (e.g. `~/.local/bin` isn't in the base PATH), use the full path instead:

```json
{
  "mcpServers": {
    "session-search": {
      "command": "/home/your-user/.local/bin/claude-session-mcp"
    }
  }
}
```

Then use the tools from within Claude Code conversations.

## Tools

### list_projects

List all Claude Code projects with session counts and sizes.

### list_sessions

List sessions for a project (or all projects), sorted by most recent first. Shows record count, date range, and subagent count. Supports pagination.

**Params:** `project`, `limit`, `offset`

### search

Full-text search across session history. Returns matches with 2 ancestor messages for conversational context, and indicates whether each match is on the primary branch or an abandoned fork.

**Params:** `query`, `project`, `max_results`, `include_subagents`

### get_tree

Get the conversation tree structure for a session: fork points, branch summaries, compaction boundaries, and leaf nodes. Use this to understand session structure before navigating.

**Params:** `session_id`, `project`

### get_thread

Get a specific conversation thread (root-to-leaf path). Filters to user/assistant messages only. Supports pagination for long threads. Empty `leaf_uuid` returns the primary thread.

**Params:** `session_id`, `leaf_uuid`, `project`, `offset`, `limit`, `include_tool_calls`

### get_forks

Get fork point details with diverging branches. Shows which branch the user continued with (primary) vs abandoned, with descendant counts and preview text.

**Params:** `session_id`, `project`, `fork_uuid`

## How it works

- **Tree parsing**: Builds `uuid → Record` and `parentUuid → [child UUIDs]` indexes from JSONL
- **Compact boundary stitching**: System records with `logicalParentUuid` are stitched back into the tree, reconnecting compacted conversations
- **Fork detection**: A parent with 2+ meaningful children (user/assistant type) is a fork point
- **Primary branch**: At each fork, the child with the latest timestamp is the user's final choice
- **Lazy loading**: Session files are only parsed when a tool needs the data
- **Lightweight metadata**: `list_sessions` avoids full JSON parsing for speed

## Acknowledgements

The core insight that Claude Code session files are trees (not linear logs) comes from [claude-session-tools](https://github.com/aurora-thesean/claude-session-tools) by aurora-thesean. That project's clear documentation of the `parentUuid` tree structure and fork semantics informed the design of this MCP server. Our implementation adds compact boundary stitching, primary branch detection, lazy loading, and the MCP interface, but the foundational understanding of the data model originated there.

## License

MIT
