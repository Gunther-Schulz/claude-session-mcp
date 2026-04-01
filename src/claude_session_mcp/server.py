"""MCP server with tools for searching and navigating Claude Code session history."""

from mcp.server.fastmcp import FastMCP

from .session_tree import ProjectIndex

mcp = FastMCP("claude-session-search")
_index = ProjectIndex()


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes}B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    return f"{size_bytes / (1024 * 1024):.1f}MB"


@mcp.tool()
def list_projects() -> str:
    """List all Claude Code projects that have session history.

    Returns project slugs (derived from directory paths), session counts, and total size.
    """
    projects = _index.list_projects()
    if not projects:
        return "No projects found in ~/.claude/projects/"

    lines = []
    lines.append(f"{'Project':<60} {'Sessions':>8} {'Size':>10}")
    lines.append("-" * 80)
    for p in projects:
        lines.append(
            f"{p['slug']:<60} {p['session_count']:>8} {_format_size(p['total_size_bytes']):>10}"
        )
    lines.append(f"\n{len(projects)} project(s) total")
    return "\n".join(lines)


@mcp.tool()
def list_sessions(project: str = "", limit: int = 20, offset: int = 0) -> str:
    """List sessions for a project, sorted by most recent first.

    Args:
        project: Project slug to filter by. Empty for all projects.
        limit: Maximum sessions to return (default 20).
        offset: Skip first N sessions for pagination.
    """
    sessions = _index.list_sessions(project_slug=project, limit=limit, offset=offset)
    if not sessions:
        return "No sessions found."

    lines = []
    for s in sessions:
        lines.append(f"Session: {s.session_id}")
        lines.append(f"  Project:    {s.project_slug}")
        lines.append(f"  Records:    {s.record_count}")
        lines.append(f"  Date range: {s.first_timestamp} to {s.last_timestamp}")
        if s.has_subagents:
            lines.append(f"  Subagents:  {s.subagent_count}")
        lines.append("")

    lines.append(f"{len(sessions)} session(s) shown (offset={offset}, limit={limit})")
    return "\n".join(lines)


@mcp.tool()
def search(query: str, project: str = "", max_results: int = 10, include_subagents: bool = False) -> str:
    """Full-text search across Claude Code session history.

    Searches conversation text (user and assistant messages) with fork awareness.
    Returns matches with surrounding conversational context and branch info.

    Args:
        query: Search text (case-insensitive substring match).
        project: Project slug to filter by. Empty for all projects.
        max_results: Maximum results (default 10, max 50).
        include_subagents: Also search subagent session files.
    """
    max_results = min(max_results, 50)
    hits = _index.search_all(
        query=query,
        project_slug=project,
        max_results=max_results,
        include_subagents=include_subagents,
    )

    if not hits:
        return f"No results found for '{query}'."

    lines = []
    for i, hit in enumerate(hits, 1):
        branch = "primary" if hit.is_primary_branch else "fork (abandoned)"
        lines.append(f"=== Match {i}/{len(hits)} ===")
        lines.append(f"Session: {hit.session_id}")
        lines.append(f"Project: {hit.project_slug}")
        lines.append(f"Branch:  {branch}")
        lines.append(f"Time:    {hit.record.timestamp}")
        lines.append(f"Type:    {hit.record.record_type}")

        if hit.ancestors:
            lines.append("")
            lines.append("Context:")
            for a in hit.ancestors:
                preview = a.text[:120].replace("\n", " ")
                lines.append(f"  [{a.record_type[0]}] {preview}")

        lines.append("")
        # Show match text with query highlighted in context
        text = hit.record.text
        idx = text.lower().find(query.lower())
        if idx >= 0:
            start = max(0, idx - 80)
            end = min(len(text), idx + len(query) + 80)
            snippet = text[start:end]
            if start > 0:
                snippet = "..." + snippet
            if end < len(text):
                snippet = snippet + "..."
            lines.append(f"Match: {snippet}")
        else:
            lines.append(f"Match: {text[:200]}")

        lines.append("")

    lines.append(f"{len(hits)} result(s) found.")
    return "\n".join(lines)


@mcp.tool()
def get_tree(session_id: str, project: str = "") -> str:
    """Get the conversation tree structure for a session.

    Shows fork points, branch summaries, compaction boundaries, and leaf nodes.
    Use this to understand the structure before navigating with get_thread or get_forks.

    Args:
        session_id: Session UUID (or prefix).
        project: Optional project slug to narrow lookup.
    """
    tree = _index.find_session(session_id, project_slug=project)
    if tree is None:
        return f"Session '{session_id}' not found."

    records = tree.records
    forks = tree.get_fork_points()
    leaves = tree.get_leaf_nodes()

    # Count by type
    type_counts: dict[str, int] = {}
    compact_count = 0
    for r in records.values():
        type_counts[r.record_type] = type_counts.get(r.record_type, 0) + 1
        if r.subtype == "compact_boundary":
            compact_count += 1

    lines = []
    lines.append(f"Session: {tree.session_id}")
    lines.append(f"File:    {tree.filepath}")
    lines.append(f"Records: {len(records)} total ({', '.join(f'{c} {t}' for t, c in sorted(type_counts.items()))})")
    if compact_count:
        lines.append(f"Compaction boundaries: {compact_count}")
    lines.append(f"Fork points: {len(forks)}")
    lines.append(f"Leaf nodes: {len(leaves)}")
    lines.append("")

    # Show fork details
    if forks:
        lines.append("Forks:")
        for i, fork in enumerate(forks, 1):
            parent = fork.parent_record
            parent_preview = parent.text[:80].replace("\n", " ") if parent.text else f"[{parent.record_type}]"
            lines.append(f"  Fork {i} at {parent.timestamp}")
            lines.append(f"    Parent: [{parent.record_type}] {parent_preview}")
            for j, branch in enumerate(fork.branches, 1):
                primary_tag = " (primary)" if branch.is_primary else " (abandoned)"
                preview = branch.preview_text[:80].replace("\n", " ") if branch.preview_text else ""
                lines.append(
                    f"    Branch {j}{primary_tag}: {branch.descendant_count} descendants"
                    f" [{branch.first_type}] {preview}"
                )
            lines.append("")

    # Show leaf nodes
    if leaves:
        lines.append("Leaf nodes (thread endpoints):")
        for leaf_uuid in leaves[:20]:
            leaf = records.get(leaf_uuid)
            if leaf:
                depth = len(tree.get_ancestors(leaf_uuid))
                primary = "primary" if tree.is_on_primary_branch(leaf_uuid) else "fork"
                preview = leaf.text[:60].replace("\n", " ") if leaf.text else ""
                lines.append(f"  {leaf_uuid[:12]}... depth={depth} ({primary}) [{leaf.record_type}] {preview}")
        if len(leaves) > 20:
            lines.append(f"  ... and {len(leaves) - 20} more")

    return "\n".join(lines)


@mcp.tool()
def get_thread(
    session_id: str,
    leaf_uuid: str = "",
    project: str = "",
    offset: int = 0,
    limit: int = 50,
    include_tool_calls: bool = False,
) -> str:
    """Get a conversation thread (root-to-leaf path through the tree).

    Only includes user and assistant messages. Supports pagination for long threads.

    Args:
        session_id: Session UUID (or prefix).
        leaf_uuid: UUID of the leaf node. Empty for the primary (latest) thread.
        project: Optional project slug.
        offset: Skip first N messages for pagination.
        limit: Max messages to return (default 50, max 200).
        include_tool_calls: Show tool names used in assistant messages.
    """
    tree = _index.find_session(session_id, project_slug=project)
    if tree is None:
        return f"Session '{session_id}' not found."

    limit = min(limit, 200)

    if leaf_uuid:
        thread = tree.get_thread_to_leaf(leaf_uuid)
    else:
        thread = tree.get_primary_thread()

    if not thread:
        return "No messages in thread."

    total = len(thread)
    page = thread[offset:offset + limit]

    lines = []
    lines.append(f"Thread: {tree.session_id}")
    if leaf_uuid:
        lines.append(f"Leaf: {leaf_uuid}")
    else:
        lines.append("Branch: primary")
    lines.append(f"Messages {offset + 1}-{offset + len(page)} of {total}")
    if offset + limit < total:
        lines.append(f"(use offset={offset + limit} for next page)")
    lines.append("")

    for i, record in enumerate(page, offset + 1):
        role = record.record_type.upper()
        lines.append(f"[{i}] {role} ({record.timestamp})")

        if record.text:
            # Truncate very long messages
            text = record.text
            if len(text) > 1000:
                text = text[:1000] + f"\n... [{len(record.text) - 1000} chars truncated]"
            lines.append(text)
        elif record.is_compact_summary:
            lines.append("[compact summary]")

        if include_tool_calls and record.tool_names:
            lines.append(f"[Tools: {', '.join(record.tool_names)}]")

        lines.append("")

    return "\n".join(lines)


@mcp.tool()
def get_forks(session_id: str, project: str = "", fork_uuid: str = "") -> str:
    """Get fork points in a session with diverging branch details.

    Shows where conversations branched (retries, edits) and which branch
    the user continued with (primary) vs abandoned.

    Args:
        session_id: Session UUID (or prefix).
        project: Optional project slug.
        fork_uuid: Show only this specific fork point. Empty for all forks.
    """
    tree = _index.find_session(session_id, project_slug=project)
    if tree is None:
        return f"Session '{session_id}' not found."

    forks = tree.get_fork_points()

    if fork_uuid:
        forks = [f for f in forks if f.parent_uuid.startswith(fork_uuid)]
        if not forks:
            return f"No fork found at UUID '{fork_uuid}'."

    if not forks:
        return "No fork points in this session."

    lines = []
    for i, fork in enumerate(forks, 1):
        parent = fork.parent_record
        lines.append(f"Fork {i} of {len(forks)}")
        lines.append(f"Parent UUID: {parent.uuid}")
        lines.append(f"Timestamp:   {parent.timestamp}")

        parent_text = parent.text[:200].replace("\n", " ") if parent.text else ""
        lines.append(f"Last shared: [{parent.record_type}] {parent_text}")
        lines.append("")

        for j, branch in enumerate(fork.branches, 1):
            primary_tag = " [PRIMARY]" if branch.is_primary else " [abandoned]"
            preview = branch.preview_text[:200].replace("\n", " ")
            lines.append(f"  Branch {j}{primary_tag} ({branch.descendant_count} descendants)")
            lines.append(f"    First: [{branch.first_type}] {branch.first_timestamp}")
            lines.append(f"    Preview: {preview}")
            lines.append(f"    UUID: {branch.first_uuid}")
            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)
