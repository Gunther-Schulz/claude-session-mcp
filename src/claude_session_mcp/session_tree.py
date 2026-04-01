"""Fork-aware tree parser for Claude Code session JSONL files."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path


# Record types that participate in the conversation tree
TREE_TYPES = {"user", "assistant", "system"}

# Record types meaningful for display/search (not system noise)
MEANINGFUL_TYPES = {"user", "assistant"}

PROJECTS_DIR = Path.home() / ".claude" / "projects"


@dataclass
class Record:
    uuid: str
    parent_uuid: str | None
    logical_parent_uuid: str | None
    record_type: str
    subtype: str | None
    timestamp: str
    session_id: str
    is_compact_summary: bool
    is_meta: bool
    text: str
    tool_names: list[str] = field(default_factory=list)


@dataclass
class SearchHit:
    record: Record
    ancestors: list[Record]
    session_id: str
    project_slug: str
    is_primary_branch: bool


@dataclass
class BranchSummary:
    first_uuid: str
    first_timestamp: str
    first_type: str
    descendant_count: int
    is_primary: bool
    preview_text: str


@dataclass
class ForkPoint:
    parent_uuid: str
    parent_record: Record
    branches: list[BranchSummary]


@dataclass
class SessionMeta:
    session_id: str
    filepath: Path
    project_slug: str
    record_count: int
    first_timestamp: str
    last_timestamp: str
    has_subagents: bool
    subagent_count: int


def extract_text(record_data: dict) -> str:
    """Extract searchable plain text from a JSONL record.

    Handles multiple content locations:
    - message.content (list of blocks or string)
    - top-level content (string, for system/compact records)
    - toolUseResult.content (list of blocks, for tool results in user records)
    """
    texts = []

    # Check message.content
    message = record_data.get("message")
    if isinstance(message, dict):
        content = message.get("content", "")
        texts.extend(_extract_from_content(content))

    # Check top-level content (compact boundary records, etc.)
    top_content = record_data.get("content")
    if isinstance(top_content, str) and top_content:
        texts.append(top_content)

    # Check toolUseResult.content for agent/tool results in user records
    tool_result = record_data.get("toolUseResult")
    if isinstance(tool_result, dict):
        tr_content = tool_result.get("content")
        if tr_content:
            texts.extend(_extract_from_content(tr_content))

    return "\n".join(texts)


def _extract_from_content(content) -> list[str]:
    """Extract text from a content field (string or list of blocks)."""
    if isinstance(content, str):
        return [content] if content else []
    if isinstance(content, list):
        texts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                t = block.get("text", "")
                if t:
                    texts.append(t)
            elif block_type == "tool_result":
                # Tool results can contain nested content
                inner = block.get("content", "")
                if isinstance(inner, str) and inner:
                    texts.append(inner[:500])  # Truncate large tool output
                elif isinstance(inner, list):
                    for sub in inner:
                        if isinstance(sub, dict) and sub.get("type") == "text":
                            t = sub.get("text", "")
                            if t:
                                texts.append(t[:500])
        return texts
    return []


def extract_tool_names(record_data: dict) -> list[str]:
    """Extract tool names from tool_use blocks in assistant messages."""
    message = record_data.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    names = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = block.get("name", "")
            if name:
                names.append(name)
    return names


def parse_record(data: dict) -> Record | None:
    """Parse a raw JSON dict into a Record, or None if not a tree record."""
    uuid = data.get("uuid")
    record_type = data.get("type", "")
    if not uuid or record_type not in TREE_TYPES:
        return None

    # Subtype can be at top level or in message
    subtype = data.get("subtype")
    if not subtype:
        msg = data.get("message")
        if isinstance(msg, dict):
            subtype = msg.get("subtype")

    return Record(
        uuid=uuid,
        parent_uuid=data.get("parentUuid"),
        logical_parent_uuid=data.get("logicalParentUuid"),
        record_type=record_type,
        subtype=subtype,
        timestamp=data.get("timestamp", ""),
        session_id=data.get("sessionId", ""),
        is_compact_summary=bool(data.get("isCompactSummary")),
        is_meta=bool(data.get("isMeta")),
        text=extract_text(data),
        tool_names=extract_tool_names(data),
    )


class SessionTree:
    """Lazy-loaded tree representation of a single session JSONL file."""

    def __init__(self, filepath: Path):
        self.filepath = filepath
        self.session_id = filepath.stem
        self._records: dict[str, Record] | None = None
        self._children: dict[str, list[str]] | None = None
        self._roots: list[str] | None = None
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._records = {}
        self._children = defaultdict(list)
        self._roots = []

        try:
            with open(self.filepath, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    record = parse_record(data)
                    if record is None:
                        continue

                    self._records[record.uuid] = record

                    if record.parent_uuid:
                        self._children[record.parent_uuid].append(record.uuid)
                    elif record.logical_parent_uuid:
                        # Compact boundary: stitch into tree via logical parent
                        self._children[record.logical_parent_uuid].append(record.uuid)
                    else:
                        self._roots.append(record.uuid)
        except OSError:
            pass

        self._loaded = True

    @property
    def records(self) -> dict[str, Record]:
        self._ensure_loaded()
        return self._records  # type: ignore

    @property
    def children(self) -> dict[str, list[str]]:
        self._ensure_loaded()
        return self._children  # type: ignore

    @property
    def roots(self) -> list[str]:
        self._ensure_loaded()
        return self._roots  # type: ignore

    def get_fork_points(self) -> list[ForkPoint]:
        """Find all points where the conversation forked.

        A fork is where a parent has 2+ children of meaningful types.
        """
        self._ensure_loaded()
        forks = []
        for parent_uuid, child_uuids in self._children.items():
            # Filter to meaningful children
            meaningful = [
                uid for uid in child_uuids
                if uid in self._records and self._records[uid].record_type in MEANINGFUL_TYPES
            ]
            if len(meaningful) < 2:
                continue

            parent = self._records.get(parent_uuid)
            if parent is None:
                continue

            # Build branch summaries
            branches = []
            for child_uuid in meaningful:
                child = self._records[child_uuid]
                desc_count = self._count_descendants(child_uuid)
                branches.append(BranchSummary(
                    first_uuid=child_uuid,
                    first_timestamp=child.timestamp,
                    first_type=child.record_type,
                    descendant_count=desc_count,
                    is_primary=False,
                    preview_text=child.text[:200] if child.text else "",
                ))

            # Primary branch = latest timestamp (the one the user continued with)
            if branches:
                branches.sort(key=lambda b: b.first_timestamp)
                branches[-1].is_primary = True

            forks.append(ForkPoint(
                parent_uuid=parent_uuid,
                parent_record=parent,
                branches=branches,
            ))

        forks.sort(key=lambda f: f.parent_record.timestamp)
        return forks

    def _count_descendants(self, uuid: str) -> int:
        """Count all descendants of a node."""
        count = 0
        visited = set()
        stack = [uuid]
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            children = self._children.get(current, [])
            count += len(children)
            stack.extend(children)
        return count

    def get_ancestors(self, uuid: str) -> list[Record]:
        """Walk from uuid to root, return oldest-first list."""
        self._ensure_loaded()
        ancestors = []
        visited = set()
        current = uuid
        while current:
            if current in visited:
                break
            visited.add(current)
            record = self._records.get(current)
            if record is None:
                break
            ancestors.append(record)
            # Walk up: prefer parentUuid, fall back to logicalParentUuid
            current = record.parent_uuid or record.logical_parent_uuid
        ancestors.reverse()
        return ancestors

    def get_leaf_nodes(self) -> list[str]:
        """Return UUIDs that have no children."""
        self._ensure_loaded()
        all_parents = set(self._children.keys())
        return [
            uuid for uuid in self._records
            if uuid not in all_parents
        ]

    def get_primary_thread(self) -> list[Record]:
        """Get the main conversation thread.

        Starting from the first root, at each fork follow the branch
        with the latest timestamp (the user's final choice).
        Filters to meaningful types only.
        """
        self._ensure_loaded()
        if not self._roots:
            return []

        thread = []
        visited = set()
        current = self._roots[0]

        while current:
            if current in visited:
                break
            visited.add(current)

            record = self._records.get(current)
            if record is None:
                break
            thread.append(record)

            children = self._children.get(current, [])
            if not children:
                break

            # At fork: choose child with latest timestamp
            best = None
            best_ts = ""
            for child_uuid in children:
                child = self._records.get(child_uuid)
                if child and child.timestamp > best_ts:
                    best = child_uuid
                    best_ts = child.timestamp
            current = best

        return [r for r in thread if r.record_type in MEANINGFUL_TYPES]

    def get_thread_to_leaf(self, leaf_uuid: str) -> list[Record]:
        """Get root-to-leaf thread for a specific leaf node."""
        ancestors = self.get_ancestors(leaf_uuid)
        return [r for r in ancestors if r.record_type in MEANINGFUL_TYPES]

    def is_on_primary_branch(self, uuid: str) -> bool:
        """Check if a record is on the primary (latest-timestamp) branch."""
        self._ensure_loaded()
        # Walk from uuid to root, checking at each level
        visited = set()
        current = uuid
        while current:
            if current in visited:
                break
            visited.add(current)
            record = self._records.get(current)
            if record is None:
                break
            parent_uuid = record.parent_uuid or record.logical_parent_uuid
            if not parent_uuid:
                break
            # Check siblings
            siblings = self._children.get(parent_uuid, [])
            if len(siblings) > 1:
                # Find the latest-timestamp sibling
                latest_uuid = max(
                    siblings,
                    key=lambda uid: self._records[uid].timestamp if uid in self._records else "",
                )
                if latest_uuid != current:
                    return False
            current = parent_uuid
        return True

    def search(self, query: str, max_results: int = 10) -> list[tuple[Record, list[Record]]]:
        """Search for text across all meaningful records.

        Returns list of (matching_record, ancestor_context) tuples.
        ancestor_context has up to 2 meaningful ancestors for context.
        """
        self._ensure_loaded()
        query_lower = query.lower()
        results = []

        for record in self._records.values():
            if record.record_type not in MEANINGFUL_TYPES:
                continue
            if record.is_meta or record.is_compact_summary:
                continue
            if query_lower not in record.text.lower():
                continue

            # Get 2 meaningful ancestors for context
            ancestors = self.get_ancestors(record.uuid)
            meaningful_ancestors = [
                a for a in ancestors[:-1]  # Exclude the match itself
                if a.record_type in MEANINGFUL_TYPES and not a.is_meta and not a.is_compact_summary
            ]
            context = meaningful_ancestors[-2:] if len(meaningful_ancestors) >= 2 else meaningful_ancestors

            results.append((record, context))
            if len(results) >= max_results:
                break

        return results


class ProjectIndex:
    """Index of all Claude Code projects and their sessions."""

    def list_projects(self) -> list[dict]:
        """List all projects with session counts and sizes."""
        if not PROJECTS_DIR.exists():
            return []

        projects = []
        for project_dir in sorted(PROJECTS_DIR.iterdir()):
            if not project_dir.is_dir():
                continue
            sessions = list(project_dir.glob("*.jsonl"))
            if not sessions:
                continue
            total_size = sum(s.stat().st_size for s in sessions)
            projects.append({
                "slug": project_dir.name,
                "session_count": len(sessions),
                "total_size_bytes": total_size,
            })
        return projects

    def list_sessions(
        self,
        project_slug: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> list[SessionMeta]:
        """List sessions with lightweight metadata (no full parse).

        Sorted by most recent first (file mtime).
        """
        if not PROJECTS_DIR.exists():
            return []

        session_files: list[tuple[Path, str]] = []

        if project_slug:
            project_dir = PROJECTS_DIR / project_slug
            if project_dir.is_dir():
                for f in project_dir.glob("*.jsonl"):
                    session_files.append((f, project_slug))
        else:
            for project_dir in PROJECTS_DIR.iterdir():
                if not project_dir.is_dir():
                    continue
                for f in project_dir.glob("*.jsonl"):
                    session_files.append((f, project_dir.name))

        # Sort by mtime descending (most recent first)
        session_files.sort(key=lambda x: x[0].stat().st_mtime, reverse=True)

        # Apply pagination
        page = session_files[offset:offset + limit]

        results = []
        for filepath, slug in page:
            meta = self._quick_metadata(filepath, slug)
            if meta:
                results.append(meta)
        return results

    def _quick_metadata(self, filepath: Path, project_slug: str) -> SessionMeta | None:
        """Get lightweight metadata without full JSON parse."""
        try:
            # Count lines and get first/last timestamps
            first_ts = ""
            last_ts = ""
            count = 0

            with open(filepath, "r") as f:
                for line in f:
                    count += 1
                    line = line.strip()
                    if not line:
                        continue
                    if not first_ts or not last_ts:
                        try:
                            data = json.loads(line)
                            ts = data.get("timestamp", "")
                            if ts and not first_ts:
                                first_ts = ts
                        except json.JSONDecodeError:
                            continue
                    # We'll get last_ts from the final line below

            # Get last timestamp from last valid line
            with open(filepath, "rb") as f:
                # Read last 4KB
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 4096))
                tail = f.read().decode("utf-8", errors="replace")
                for tail_line in reversed(tail.strip().split("\n")):
                    try:
                        data = json.loads(tail_line)
                        last_ts = data.get("timestamp", "")
                        if last_ts:
                            break
                    except json.JSONDecodeError:
                        continue

            # Check for subagents
            subagent_dir = filepath.parent / filepath.stem / "subagents"
            has_subagents = subagent_dir.is_dir()
            subagent_count = 0
            if has_subagents:
                subagent_count = len(list(subagent_dir.glob("*.jsonl")))

            return SessionMeta(
                session_id=filepath.stem,
                filepath=filepath,
                project_slug=project_slug,
                record_count=count,
                first_timestamp=first_ts,
                last_timestamp=last_ts,
                has_subagents=has_subagents,
                subagent_count=subagent_count,
            )
        except OSError:
            return None

    def find_session(self, session_id: str, project_slug: str = "") -> SessionTree | None:
        """Find a session by ID (or prefix)."""
        if not PROJECTS_DIR.exists():
            return None

        dirs = [PROJECTS_DIR / project_slug] if project_slug else sorted(PROJECTS_DIR.iterdir())

        for project_dir in dirs:
            if not project_dir.is_dir():
                continue
            for f in project_dir.glob("*.jsonl"):
                if f.stem == session_id or f.stem.startswith(session_id):
                    return SessionTree(f)

            # Check subagent files
            for session_dir in project_dir.iterdir():
                if not session_dir.is_dir():
                    continue
                subagent_dir = session_dir / "subagents"
                if not subagent_dir.is_dir():
                    continue
                for f in subagent_dir.glob("*.jsonl"):
                    if f.stem == session_id or f.stem.startswith(session_id):
                        return SessionTree(f)

        return None

    def search_all(
        self,
        query: str,
        project_slug: str = "",
        max_results: int = 10,
        include_subagents: bool = False,
    ) -> list[SearchHit]:
        """Search across all sessions."""
        if not PROJECTS_DIR.exists():
            return []

        hits: list[SearchHit] = []

        # Collect session files sorted by mtime (newest first)
        session_files: list[tuple[Path, str]] = []

        dirs = [PROJECTS_DIR / project_slug] if project_slug else sorted(PROJECTS_DIR.iterdir())
        for project_dir in dirs:
            if not project_dir.is_dir():
                continue
            slug = project_dir.name
            for f in project_dir.glob("*.jsonl"):
                session_files.append((f, slug))

            if include_subagents:
                for session_dir in project_dir.iterdir():
                    if not session_dir.is_dir():
                        continue
                    subagent_dir = session_dir / "subagents"
                    if subagent_dir.is_dir():
                        for f in subagent_dir.glob("*.jsonl"):
                            session_files.append((f, slug))

        session_files.sort(key=lambda x: x[0].stat().st_mtime, reverse=True)

        for filepath, slug in session_files:
            if len(hits) >= max_results:
                break

            tree = SessionTree(filepath)
            remaining = max_results - len(hits)
            results = tree.search(query, max_results=remaining)

            for record, context in results:
                hits.append(SearchHit(
                    record=record,
                    ancestors=context,
                    session_id=tree.session_id,
                    project_slug=slug,
                    is_primary_branch=tree.is_on_primary_branch(record.uuid),
                ))

        return hits
