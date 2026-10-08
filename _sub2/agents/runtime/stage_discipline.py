"""Tool-level guardrails for ARC's staged agent workflow."""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage

_FILE_WRITE_TOOLS = frozenset({"edit_file", "write_file"})
_VALIDATION_TOOLS = frozenset({"run_build", "run_tests"})
_MAX_DESIGN_WRITES = 8
_MAX_SKELETON_LINES = 160
_MAX_READ_LIMIT = 200
# During interface_design the agent must obtain context from traceability tools, not by
# scanning the workspace. Allow a small safety-valve quota of workspace reads (e.g. to
# inspect a file the traceability record points at) but block open-ended exploration.
_MAX_DESIGN_READS = 5

# Harness-provided server/runtime files that a leaf agent must NEVER rewrite.
# These are enforced as a hard write block (not just a prompt hint) because the
# framework's SPA-fallback / DB wiring lives here; an agent that "fixes" the SPA
# fallback by hard-coding the dist path (e.g. `res.sendFile(../../frontend/dist)`)
# was the root cause of the sheet `NotFoundError: Not Found` 404. Reuse the template
# wiring instead of editing these files.
_PROTECTED_HARNESS_FILES = frozenset(
    {
        "app.js",
        "init_db.js",
        "db_runtime.js",
        "seed_db.js",
        "test_harness.js",
        "prepare_e2e.js",
    }
)


def _is_protected_harness_file(path: str) -> bool:
    norm = (path or "").replace("\\", "/")
    if "backend" not in norm:
        return False
    base = norm.rsplit("/", 1)[-1]
    return base in _PROTECTED_HARNESS_FILES


class StageDisciplineState(TypedDict, total=False):
    """Run-local state used to prevent redundant file-tool loops."""

    arc_read_summaries: NotRequired[dict[str, str]]
    arc_written_paths: NotRequired[list[str]]


class StageDisciplineMiddleware(AgentMiddleware[StageDisciplineState, Any, Any]):
    """Enforce stage boundaries and stop post-write self-review loops."""

    state_schema = StageDisciplineState

    def __init__(self, *, stage: Literal["interface_design", "test_generation", "implementation"]) -> None:
        self._stage = stage
        self._read_ranges: dict[str, list[tuple[int, int]]] = {}
        self._written_paths: set[str] = set()
        self._failed_paths: set[str] = set()
        self._validation_failed = False
        self._design_write_count = 0
        self._design_read_count = 0

    def wrap_tool_call(self, request: ToolCallRequest, handler: Any) -> ToolMessage | Any:
        blocked = self._validate_tool_call(request)
        if blocked:
            return self._blocked(request, blocked)
        request = self._with_bounded_read(request)
        result = handler(request)
        self._record_result(request, result)
        return result

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Any) -> ToolMessage | Any:
        blocked = self._validate_tool_call(request)
        if blocked:
            return self._blocked(request, blocked)
        request = self._with_bounded_read(request)
        result = await handler(request)
        self._record_result(request, result)
        return result

    def _validate_tool_call(self, request: ToolCallRequest) -> str | None:
        name = str(request.tool_call.get("name", ""))
        args = request.tool_call.get("args", {}) or {}
        if name in {"execute", "delete"}:
            return f"`{name}` is disabled in ARC's staged file workflow."
        if self._stage == "test_generation" and name in _VALIDATION_TOOLS:
            return "TestGenerator only creates tests and its manifest; it must not run validation."
        if name == "read_file":
            if self._stage == "interface_design":
                return self._validate_design_read(args)
            return self._validate_read(args)
        if name in _FILE_WRITE_TOOLS:
            return self._validate_write(args)
        return None

    def _validate_design_read(self, args: dict[str, Any]) -> str | None:
        path = _discipline_path(args)
        # Skill instruction files are the only legitimate reads during DESIGN.
        if path.startswith("/skills/"):
            return None
        if path.startswith("/workspace/"):
            if self._design_read_count >= _MAX_DESIGN_READS:
                return (
                    f"Workspace reads are capped at {_MAX_DESIGN_READS} during interface_design. "
                    "Use get_interfaces_for_requirement / get_interface / search_interfaces to obtain "
                    "interface context and original source; do not explore the workspace filesystem."
                )
            return None
        return None

    def _validate_read(self, args: dict[str, Any]) -> str | None:
        path = _discipline_path(args)
        if not path:
            return None
        if path in self._written_paths and not self._path_unlocked(path):
            return (
                f"Read blocked: {path} was already written in this stage. Continue to the next action; "
                "re-read only after a file-operation or system-validation error."
            )
        offset = _as_nonnegative_int(args.get("offset"), default=0)
        limit = min(_as_nonnegative_int(args.get("limit"), default=100), _MAX_READ_LIMIT)
        previous = self._read_ranges.get(path, [])
        if not previous or self._path_unlocked(path):
            return None
        if any(_ranges_overlap(offset, offset + limit, start, end) for start, end in previous):
            return (
                f"Repeated read blocked: {path} is already in this stage's read cache. "
                "Use the earlier result; only a non-overlapping paginated range or a failure may justify another read."
            )
        return None

    def _validate_write(self, args: dict[str, Any]) -> str | None:
        path = _discipline_path(args)
        if not path:
            return None
        if _is_protected_harness_file(path):
            return (
                f"Write blocked: {path} is a harness-provided server/runtime file and must not be "
                "rewritten. Reuse the template's existing serving/DB wiring. If you need a route or "
                "endpoint, register it through the framework's declarative mechanism or add it to a "
                "leaf-owned module -- never by editing this file. This block also prevents authoring a "
                "custom SPA-fallback that hard-codes the dist path (the cause of the sheet "
                "`NotFoundError: Not Found` 404 during E2E)."
            )
        if self._stage == "implementation":
            # Implementation legitimately edits the same file many times while
            # wiring features; the repeated-write block was deadlocking agents
            # for ~32min and failing leaves. Allow free writes here -- outer
            # TDD / budget limits still cap runaway loops.
            return None
        if path in self._written_paths and not self._path_unlocked(path):
            return (
                f"Repeated write blocked: {path} was already changed in this stage. "
                "Wait for a file-operation or system-validation error before changing it again."
            )
        if self._stage == "test_generation" and not _is_test_asset(path):
            return (
                "TestGenerator may write only test files, test helpers/configuration, and the returned manifest; "
                f"{path} is not a test asset."
            )
        if self._stage == "interface_design":
            if path not in self._written_paths and self._design_write_count >= _MAX_DESIGN_WRITES:
                return (
                    f"InterfaceDesigner may materialize at most {_MAX_DESIGN_WRITES} small skeleton files. "
                    "Record remaining interfaces in the response for TDD."
                )
            content = str(args.get("content", args.get("new_string", "")) or "")
            if content.count("\n") + 1 > _MAX_SKELETON_LINES:
                return (
                    f"InterfaceDesigner may only materialize small skeletons (at most {_MAX_SKELETON_LINES} lines per write). "
                    "Record the complete business contract for TDD instead of implementing it now."
                )
        return None

    def _with_bounded_read(self, request: ToolCallRequest) -> ToolCallRequest:
        if request.tool_call.get("name") != "read_file":
            return request
        args = dict(request.tool_call.get("args", {}) or {})
        if not _discipline_path(args).startswith("/workspace/"):
            return request
        args["offset"] = _as_nonnegative_int(args.get("offset"), default=0)
        args["limit"] = min(_as_nonnegative_int(args.get("limit"), default=100), _MAX_READ_LIMIT)
        return request.override(tool_call={**request.tool_call, "args": args})

    def _record_result(self, request: ToolCallRequest, result: ToolMessage | Any) -> None:
        name = str(request.tool_call.get("name", ""))
        args = request.tool_call.get("args", {}) or {}
        path = _discipline_path(args)
        if name in _VALIDATION_TOOLS:
            if _tool_result_failed(result):
                self._validation_failed = True
                self._failed_paths.update(self._written_paths)
            return
        if _tool_result_failed(result):
            if path:
                self._failed_paths.add(path)
            return
        if name == "read_file" and path:
            offset = _as_nonnegative_int(args.get("offset"), default=0)
            limit = _as_nonnegative_int(args.get("limit"), default=100)
            self._read_ranges.setdefault(path, []).append((offset, offset + limit))
            self._cache_read_summary(request, path, offset, limit, result)
            if self._stage == "interface_design" and path.startswith("/workspace/"):
                self._design_read_count += 1
        if name in _FILE_WRITE_TOOLS and path:
            if path not in self._written_paths and self._stage == "interface_design":
                self._design_write_count += 1
            self._written_paths.add(path)
            self._cache_written_path(request, path)

    def _path_unlocked(self, path: str) -> bool:
        return self._validation_failed or path in self._failed_paths

    @staticmethod
    def _cache_read_summary(request: ToolCallRequest, path: str, offset: int, limit: int, result: ToolMessage | Any) -> None:
        if not isinstance(request.state, dict):
            return
        content = str(getattr(result, "content", "") or "")
        line_count = content.count("\n") + (1 if content else 0)
        request.state.setdefault("arc_read_summaries", {})[path] = (
            f"lines {offset}-{offset + limit - 1}; received {line_count} line(s), {len(content)} character(s)"
        )

    @staticmethod
    def _cache_written_path(request: ToolCallRequest, path: str) -> None:
        if isinstance(request.state, dict):
            written = request.state.setdefault("arc_written_paths", [])
            if path not in written:
                written.append(path)

    @staticmethod
    def _blocked(request: ToolCallRequest, message: str) -> ToolMessage:
        return ToolMessage(
            content=f"Error: ARC stage discipline: {message}",
            name=str(request.tool_call.get("name", "tool")),
            tool_call_id=str(request.tool_call.get("id", "")),
            status="error",
        )


def _discipline_path(args: dict[str, Any]) -> str:
    raw = str(args.get("file_path", "") or "").replace("\\", "/").strip()
    return raw if raw.startswith("/") else f"/{raw}" if raw else ""


def _as_nonnegative_int(value: Any, *, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _ranges_overlap(start: int, end: int, other_start: int, other_end: int) -> bool:
    return start < other_end and other_start < end


def _tool_result_failed(result: ToolMessage | Any) -> bool:
    if isinstance(result, ToolMessage) and result.status == "error":
        return True
    content = str(getattr(result, "content", "") or "")
    return "Exit Code: 0" not in content and ("Exit Code:" in content or content.lstrip().startswith("Error:"))


def _is_test_asset(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    name = normalized.rsplit("/", 1)[-1]
    test_segments = ("/test/", "/tests/", "/__tests__/", "/e2e/", "/__mocks__/")
    test_names = (".test.", ".spec.", "playwright.config.", "vitest.config.", "jest.config.", "setup-tests.", "setuptests.")
    return any(segment in normalized for segment in test_segments) or any(marker in name for marker in test_names)
