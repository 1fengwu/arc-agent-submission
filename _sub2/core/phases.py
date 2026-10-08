from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Awaitable, Callable

from app_type_handler import create_app_type_handler
from agents.context.pipeline import context_pipeline
from core import sessions
from core.service import get_runtime
from core.path_compat import normalize_windows_extended_prefix_text
from core.visual_analysis import analyze_and_attach_visual_references
from app_type_handler.test_results import parse_test_results
from pydantic import BaseModel, Field
from agents.context.prompts.interface_designer import get_system_prompt as _id_sys_prompt, get_user_prompt as _id_user_prompt
from agents.context.prompts.test_generator import get_system_prompt as _tg_sys_prompt, get_user_prompt as _tg_user_prompt
from agents.context.prompts.common import stage_skill_activation_policy
from agents.runtime.factory import build_stage_agent
from agents.runtime.runners import ainvoke_stage_agent
from agents.runtime.contracts import AgentRuntimeContext
from agents.skills.selection import SKILLS_SOURCE, interface_design_skills, test_generation_skills
from agents.tools.traceability import build_traceability_tools
from agents.results import normalize_test_manifest_payload


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]
# Per-test-layer `run_tests` budget. Under breadth-first mode each leaf gets a
# small number of TDD attempts (default 2) so the agent can iterate on a failed
# E2E (e.g. create a missing component) instead of abandoning the leaf after one
# shot. The shared token budget is the real ceiling; in our runs it was NOT hit,
# so giving leaves 2-3 attempts trades a little breadth for real pass rate.
# Override with ARC_TDD_BUDGET (2 = default, 3 = more polish). ARC_BREADTH_FIRST=1
# caps this at 3 so one leaf can never monopolise the whole run.
import os as _os

_TDD_BUDGET_RAW = int(_os.environ.get("ARC_TDD_BUDGET", "2").strip() or 2)
if _os.environ.get("ARC_BREADTH_FIRST", "1").strip() == "1":
    _TDD_BUDGET_RAW = min(_TDD_BUDGET_RAW, 3)
TDD_RUN_TESTS_BUDGET = _TDD_BUDGET_RAW
ALLOWED_INTERFACE_TYPES = {"UI", "API", "FUNC", "DB"}
TDD_BATCH_ORDER = ("E2E",)


# ---------------------------------------------------------------------------
# ARC_NONLEAF_SHARE (opt-in, default OFF — enabled via ARC_NONLEAF_SHARE=1):
# Non-leaf interface contracts are identical across shards (same requirement
# node), so we design them ONCE and reuse the cached contract in every other
# shard. This removes the 4x duplicate token spend on ROOT/composition nodes.
# Safety: a visual_reference signature guard + try/except fallbacks guarantee we
# NEVER return an empty result — if the cache is missing, corrupt, or mismatched,
# the caller falls back to normal LLM design. The shared cache directory is
# provided by the parallel runner via ARC_NONLEAF_CACHE_DIR; in serial mode (no
# such dir) every read returns None and every write is a no-op, so behaviour is
# identical to the pre-share baseline.
# ---------------------------------------------------------------------------
def _nonleaf_cache_dir() -> "Path | None":
    d = _os.environ.get("ARC_NONLEAF_CACHE_DIR", "").strip()
    if not d:
        return None
    p = Path(d)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    return p


def _nonleaf_cache_path(node_id: str) -> "Path | None":
    root = _nonleaf_cache_dir()
    if root is None:
        return None
    return root / f"{node_id}.json"


def _read_nonleaf_cache(node_id: str, vr_sig: str) -> "list | None":
    p = _nonleaf_cache_path(node_id)
    if p is None or not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if data.get("vr_sig") != vr_sig:
        return None
    ifaces = data.get("interfaces")
    if not isinstance(ifaces, list) or not ifaces:
        return None
    return ifaces


def _write_nonleaf_cache(node_id: str, vr_sig: str, interfaces: list) -> None:
    p = _nonleaf_cache_path(node_id)
    if p is None:
        return
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_text(
            json.dumps({"vr_sig": vr_sig, "interfaces": interfaces}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(p)
    except Exception:
        pass


class _CombinedDesignResponse(BaseModel):
    """ARC_MERGE_DESIGN: one LLM call returns both interface contracts and the
    test manifest, so the full context is only sent once instead of twice."""
    interfaces: list[dict[str, Any]] = Field(default_factory=list)
    tests: list[dict[str, Any]] = Field(default_factory=list)
    files_written: list[str] = Field(default_factory=list)


class _BatchDesignLeaf(BaseModel):
    """One leaf's slice of an ARC_BATCH_GEN batch design response."""
    node_id: str = ""
    interfaces: list[dict[str, Any]] = Field(default_factory=list)
    tests: list[dict[str, Any]] = Field(default_factory=list)
    files_written: list[str] = Field(default_factory=list)


class _BatchDesignResponse(BaseModel):
    """ARC_BATCH_GEN: one LLM call designs several leaves at once, so the shared
    system prompt / skill policy / framework instructions are emitted once for the
    whole batch instead of once per leaf."""
    leaves: list[_BatchDesignLeaf] = Field(default_factory=list)

class WorkflowPhaseRunner:
    """Run ARC DESIGN and IMPLEMENT phases using the agent adapters."""

    def __init__(
        self,
        *,
        workspace_path: str,
        requirement_path: str,
        app_type: str,
        interface_designer: Any,
        test_generator: Any,
        test_driven_developer: Any,
        log_cb: LogCallback | None = None,
    ) -> None:
        self.workspace_path = str(Path(workspace_path).expanduser().resolve())
        self.requirement_path = requirement_path
        self.app_type = app_type
        self.interface_designer = interface_designer
        self.test_generator = test_generator
        self.test_driven_developer = test_driven_developer
        self.log_cb = log_cb
        self.app_handler = create_app_type_handler(
            app_type=app_type,
            workspace_path=self.workspace_path,
            requirement_path=requirement_path,
            interface_designer=interface_designer,
            log_cb=self._log,
        )
        self.test_driven_developer.app_handler = self.app_handler

    @property
    def traceability(self):
        return get_runtime().traceability

    async def run_design_phase(self, node_id: str, requirement_data: dict[str, Any]) -> bool:
        is_non_leaf = bool(requirement_data.get("children_ids"))
        requirement_data = await analyze_and_attach_visual_references(
            workspace_path=self.workspace_path,
            requirements_dir=str(Path(self.requirement_path).expanduser().resolve().parent),
            requirement_data=requirement_data,
            log_cb=self._log,
        )
        requirement_data = self.traceability.get_requirement(node_id) or requirement_data
        visual_reference = requirement_data.get("visual_reference") or []
        vr_sig = json.dumps(
            requirement_data.get("visual_reference") or [],
            ensure_ascii=False,
            sort_keys=True,
        )
        self._update_node_session(
            node_id,
            {
                "node_id": node_id,
                "phase_status": {"design": "pending", "test": "pending", "implement": "pending"},
                "requirement_snapshot": {
                    "name": requirement_data.get("name", ""),
                    "description": requirement_data.get("description", ""),
                    "visual_reference": requirement_data.get("visual_reference") or [],
                    "children_ids": requirement_data.get("children_ids") or [],
                    "dependencies": requirement_data.get("dependencies") or [],
                },
                "recent_failure_summary": "",
            },
        )

        if is_non_leaf and not visual_reference:
            self.traceability.clear_node_design_artifacts(node_id)
            context_pipeline.cache.invalidate_file_layers(node_id)
            context_pipeline.cache.invalidate_db_layers(node_id)
            self._update_node_session(
                node_id,
                {
                    "interfaces": [],
                    "materialized_files": [],
                    "test_artifacts": [],
                    "phase_status": {"design": "skipped", "test": "skipped"},
                },
            )
            await self._log(
                "InterfaceDesigner",
                "Skipping non-leaf DESIGN because this node has no visual reference; no UI/API/FUNC/DB interfaces are owned here.",
                status="info",
                node_id=node_id,
            )
            await self._log(
                "TestGenerator",
                "Skipping test generation for non-leaf node.",
                status="info",
                node_id=node_id,
            )
            return True

        await self._log("InterfaceDesigner", "Running interface design.", node_id=node_id)

        # ARC_NONLEAF_SHARE: reuse a previously designed non-leaf contract to avoid
        # 4x duplicate spend across shards. Falls back to normal design on any miss
        # (missing/corrupt/mismatched cache, or prepare failure) — never returns an
        # empty result, so this cannot produce a 0-score regression on its own.
        if is_non_leaf and _os.environ.get("ARC_NONLEAF_SHARE", "").strip() == "1":
            cached = _read_nonleaf_cache(node_id, vr_sig)
            if cached is not None:
                try:
                    prepared = self._prepare_interfaces(node_id, cached)
                except ValueError:
                    prepared = None
                if prepared is not None:
                    self._store_prepared_interfaces(node_id, prepared)
                    self._update_node_session(
                        node_id,
                        {
                            "interfaces": prepared,
                            "test_artifacts": [],
                            "phase_status": {"design": "completed", "test": "skipped"},
                        },
                    )
                    await self._log(
                        "InterfaceDesigner",
                        f"[nonleaf-share] Reused cached contract ({len(prepared)} defs) for non-leaf node; skipped duplicate LLM design.",
                        status="info",
                        node_id=node_id,
                    )
                    return True
                await self._log(
                    "InterfaceDesigner",
                    "[nonleaf-share] cached contract failed validation; falling back to normal design.",
                    status="warning",
                    node_id=node_id,
                )

        # ARC_MERGE_DESIGN (opt-in, default off): run interface design + test
        # generation in a SINGLE LLM call to avoid re-sending the full context
        # twice. Falls back to the separate calls below on any failure.
        if _os.environ.get("ARC_MERGE_DESIGN", "").strip() == "1":
            merged_ok = await self._run_design_merged(node_id, requirement_data, visual_reference)
            if merged_ok:
                return True
            await self._log(
                "Design",
                "Merged DESIGN call failed or returned empty; falling back to separate interface/test calls.",
                status="warning",
                node_id=node_id,
            )

        interface_result = await self.interface_designer.run(
            node_id=node_id,
            requirement_data=requirement_data,
        )
        interfaces = []
        for item in interface_result.get("interfaces", []):
            if not isinstance(item, dict):
                continue
            normalized = dict(item)
            normalized["file_path"] = normalize_workspace_relative_path(normalized.get("file_path"), self.workspace_path)
            interfaces.append(normalized)
        files_written = []
        for path in interface_result.get("files_written") or []:
            normalized_path = normalize_workspace_relative_path(path, self.workspace_path)
            if normalized_path:
                files_written.append(normalized_path)
        if not interfaces:
            await self._log(
                "InterfaceDesigner",
                "Interface design returned no current-node owned interface definitions.",
                status="warning",
                node_id=node_id,
            )

        try:
            prepared_interfaces = self._prepare_interfaces(node_id, interfaces)
        except ValueError as exc:
            await self._log("InterfaceDesigner", str(exc), status="error", node_id=node_id)
            return False
        context_pipeline.cache.invalidate_file_layers(node_id)
        context_pipeline.cache.invalidate_db_layers(node_id)
        self._update_node_session(
            node_id,
            {
                "interfaces": prepared_interfaces,
                "materialized_files": files_written,
                "phase_status": {"design": "prepared"},
            },
        )
        context_pipeline.cache.invalidate_db_layers(node_id)

        stored_tests: list[dict[str, Any]] = []
        if is_non_leaf:
            self.traceability.clear_node_design_artifacts(node_id)
            self._store_prepared_interfaces(node_id, prepared_interfaces)
            # Cache this non-leaf contract for reuse by other shards (ARC_NONLEAF_SHARE).
            _write_nonleaf_cache(node_id, vr_sig, interfaces)
            context_pipeline.cache.invalidate_file_layers(node_id)
            context_pipeline.cache.invalidate_db_layers(node_id)
            self._update_node_session(
                node_id,
                {
                    "interfaces": prepared_interfaces,
                    "test_artifacts": [],
                    "phase_status": {"design": "completed", "test": "skipped"},
                },
            )
            await self._log(
                "InterfaceDesigner",
                f"Stored {len(prepared_interfaces)} interface definition(s) into traceability DB.",
                node_id=node_id,
            )
            await self._log(
                "InterfaceDesigner",
                f"Interface artifact summary: {json.dumps(summarize_interface_artifacts(prepared_interfaces), ensure_ascii=False)}",
                node_id=node_id,
            )
            await self._log(
                "TestGenerator",
                "Skipping test generation for non-leaf node; composition nodes only define interfaces.",
                status="info",
                node_id=node_id,
            )
            return True

        await self._log(
            "InterfaceDesigner",
            f"Prepared {len(prepared_interfaces)} interface definition(s) for traceability storage.",
            node_id=node_id,
        )
        await self._log(
            "InterfaceDesigner",
            f"Interface artifact summary: {json.dumps(summarize_interface_artifacts(prepared_interfaces), ensure_ascii=False)}",
            node_id=node_id,
        )

        await self._log("TestGenerator", "Generating tests from agent-selected coverage strategy.", node_id=node_id)
        tests, _ = await self.test_generator.run(
            node_id=node_id,
            requirement_data=requirement_data,
        )
        if tests is None:
            await self._log(
                "TestGenerator",
                "DESIGN test generation did not return a valid test manifest.",
                status="error",
                node_id=node_id,
            )
            return False

        try:
            stored_tests = self._prepare_tests(node_id=node_id, tests=tests)
        except ValueError as exc:
            await self._log("TestGenerator", str(exc), status="error", node_id=node_id)
            return False

        self.traceability.clear_node_design_artifacts(node_id)
        self._store_prepared_interfaces(node_id, prepared_interfaces)
        self._store_prepared_tests(stored_tests)
        context_pipeline.cache.invalidate_file_layers(node_id)
        context_pipeline.cache.invalidate_db_layers(node_id)
        self._update_node_session(
            node_id,
            {
                "interfaces": prepared_interfaces,
                "test_artifacts": stored_tests,
                "phase_status": {"design": "completed", "test": "completed"},
            },
        )
        await self._log(
            "InterfaceDesigner",
            f"Stored {len(prepared_interfaces)} interface definition(s) into traceability DB.",
            node_id=node_id,
        )
        await self._log(
            "TestGenerator",
            f"Stored {len(stored_tests)} test mapping item(s) into traceability DB.",
            node_id=node_id,
        )
        await self._log(
            "TestGenerator",
            f"Test artifact summary: {json.dumps(summarize_test_artifacts(stored_tests), ensure_ascii=False)}",
            node_id=node_id,
        )
        return True

    async def _run_design_merged(
        self, node_id: str, requirement_data: dict[str, Any], visual_reference: list
    ) -> bool:
        """ARC_MERGE_DESIGN: one combined LLM call produces BOTH interface
        contracts and the test manifest (instead of two separate calls that each
        re-send the full context). Falls back to the standard two-call path if it
        returns nothing usable. Default OFF — only active when ARC_MERGE_DESIGN=1.
        """
        workspace_root = self.workspace_path
        app_type = (self.app_type or context_pipeline.config.app_type or "web").strip().lower()
        selected_skills = sorted(
            set(interface_design_skills(requirement_data) + test_generation_skills(requirement_data))
        )
        context_pipeline.configure(workspace_dir=workspace_root, app_type=app_type)
        static_context, dynamic_context = context_pipeline.build_agent_context_split(
            node_id=node_id, agent_type="InterfaceDesigner"
        )
        context_text = "\n\n".join(p.strip() for p in (static_context, dynamic_context) if p.strip())
        interface_contract = context_pipeline.get_interface_contract_context(node_id)
        system_prompt = "\n\n".join(
            [
                _id_sys_prompt(),
                _tg_sys_prompt(),
                stage_skill_activation_policy(selected_skills),
                "You must produce BOTH the interface contracts AND the test manifest in a single response.",
            ]
        )
        message = (
            _id_user_prompt(node_id=node_id, requirement_data=requirement_data, dynamic_context=context_text)
            + "\n\n---\n\n"
            + _tg_user_prompt(
                node_id=node_id,
                requirement_data=requirement_data,
                dynamic_context=context_text,
                interface_contract=interface_contract,
                test_intent="",
                replace_test_id=None,
            )
        )
        try:
            agent = build_stage_agent(
                name="design_merged",
                stage="interface_design",
                model=_os.environ.get("MODEL", "deepseek-v4-flash"),
                system_prompt=system_prompt,
                response_format=_CombinedDesignResponse,
                workspace_root=workspace_root,
                writable_roots=[workspace_root],
                skills=[SKILLS_SOURCE] if selected_skills else [],
                permitted_skill_names=selected_skills,
                memory=[],
                tools=build_traceability_tools(node_id=node_id, log_cb=self._log),
            )
            raw = await ainvoke_stage_agent(
                agent,
                message=message,
                context=AgentRuntimeContext(
                    node_id=node_id,
                    phase="DESIGN",
                    app_type=app_type,
                    workspace_root=workspace_root,
                    requirement_path=self.requirement_path,
                ),
                thread_id=f"{node_id}:DESIGN:Merged",
                label="DesignMerged",
                log_cb=self._log,
            )
        except Exception as exc:  # pragma: no cover - defensive
            await self._log("DesignMerged", f"Combined call raised: {exc}", status="error", node_id=node_id)
            return False

        interfaces = [dict(i) for i in (raw.get("interfaces") or []) if isinstance(i, dict)]
        tests_raw = raw.get("tests") or []
        tests = normalize_test_manifest_payload({"tests": tests_raw})
        if not interfaces or not tests:
            await self._log(
                "DesignMerged",
                f"Combined call returned interfaces={len(interfaces)} tests={len(tests)}; not enough to proceed.",
                status="warning",
                node_id=node_id,
            )
            return False

        files_written = [
            normalize_workspace_relative_path(str(p), self.workspace_path)
            for p in (raw.get("files_written") or [])
            if p
        ]
        try:
            prepared_interfaces = self._prepare_interfaces(node_id, interfaces)
        except ValueError as exc:
            await self._log("DesignMerged", str(exc), status="error", node_id=node_id)
            return False
        try:
            stored_tests = self._prepare_tests(node_id=node_id, tests=tests)
        except ValueError as exc:
            await self._log("DesignMerged", str(exc), status="error", node_id=node_id)
            return False
        self.traceability.clear_node_design_artifacts(node_id)
        self._store_prepared_interfaces(node_id, prepared_interfaces)
        self._store_prepared_tests(stored_tests)
        # Cache this (possibly non-leaf) contract for reuse by other shards
        # (ARC_NONLEAF_SHARE). Only meaningful when the node is non-leaf, but writing
        # it unconditionally is harmless and keeps the hot path simple.
        if bool(requirement_data.get("children_ids")):
            vr_sig = json.dumps(
                requirement_data.get("visual_reference") or [],
                ensure_ascii=False,
                sort_keys=True,
            )
            _write_nonleaf_cache(node_id, vr_sig, interfaces)
        context_pipeline.cache.invalidate_file_layers(node_id)
        context_pipeline.cache.invalidate_db_layers(node_id)
        self._update_node_session(
            node_id,
            {
                "interfaces": prepared_interfaces,
                "materialized_files": [f for f in files_written if f],
                "test_artifacts": stored_tests,
                "phase_status": {"design": "completed", "test": "completed"},
            },
        )
        await self._log(
            "DesignMerged",
            f"Stored {len(prepared_interfaces)} interface(s) and {len(stored_tests)} test(s) from combined call.",
            node_id=node_id,
        )
        return True

    async def run_design_batch(self, node_ids: list[str]) -> dict[str, bool]:
        """ARC_BATCH_GEN: design several leaves in ONE LLM call.

        The shared cost (system prompts, skill-activation policy, framework
        instructions, one round-trip) is paid once for the whole batch instead of
        once per leaf, which is the dominant repeated overhead in DESIGN.

        Compliance: this is a generation-efficiency change ONLY. Every interface
        contract and test manifest is still produced by the model at runtime from
        each node's own requirement; nothing about any leaf's answer is pre-baked
        or hard-coded (contest rule §3.8 forbids pre-fabricated task answers).

        Returns {node_id: ok}. Nodes reported False stay PENDING so the caller
        retries them through the standard per-leaf DESIGN path.
        """
        result: dict[str, bool] = {str(n): False for n in node_ids}
        workspace_root = self.workspace_path
        app_type = (self.app_type or context_pipeline.config.app_type or "web").strip().lower()
        selected_skills: set[str] = set()
        node_payloads: list[tuple[str, dict[str, Any]]] = []
        for raw_id in node_ids:
            node_id = str(raw_id or "").strip()
            if not node_id:
                continue
            requirement_data = self.traceability.get_requirement(node_id) or {}
            if not requirement_data:
                continue
            node_payloads.append((node_id, requirement_data))
            selected_skills.update(interface_design_skills(requirement_data))
            selected_skills.update(test_generation_skills(requirement_data))
        if not node_payloads:
            return result

        selected = sorted(selected_skills)
        context_pipeline.configure(workspace_dir=workspace_root, app_type=app_type)
        system_prompt = "\n\n".join(
            [
                _id_sys_prompt(),
                _tg_sys_prompt(),
                stage_skill_activation_policy(selected),
                (
                    "You are designing MULTIPLE requirement leaves in ONE response.\n"
                    "Return exactly one entry per node_id in `leaves`, each carrying that\n"
                    "node's OWN interface contracts and test manifest. Never mix nodes: an\n"
                    "interface or test belongs solely to the node_id it is listed under.\n"
                    "If a node cannot be designed, omit it or return empty lists for it."
                ),
            ]
        )
        sections: list[str] = []
        for node_id, requirement_data in node_payloads:
            static_context, dynamic_context = context_pipeline.build_agent_context_split(
                node_id=node_id, agent_type="InterfaceDesigner"
            )
            context_text = "\n\n".join(p.strip() for p in (static_context, dynamic_context) if p.strip())
            interface_contract = context_pipeline.get_interface_contract_context(node_id)
            sections.append(
                f"===== BEGIN NODE {node_id} =====\n"
                + _id_user_prompt(node_id=node_id, requirement_data=requirement_data, dynamic_context=context_text)
                + "\n\n---\n\n"
                + _tg_user_prompt(
                    node_id=node_id,
                    requirement_data=requirement_data,
                    dynamic_context=context_text,
                    interface_contract=interface_contract,
                    test_intent="",
                    replace_test_id=None,
                )
                + f"\n===== END NODE {node_id} ====="
            )
        message = (
            f"Design the following {len(node_payloads)} requirement node(s): "
            f"{', '.join(n for n, _ in node_payloads)}.\n\n"
            + "\n\n".join(sections)
        )
        primary_node = node_payloads[0][0]
        try:
            agent = build_stage_agent(
                name="design_batch",
                stage="interface_design",
                model=_os.environ.get("MODEL", "deepseek-v4-flash"),
                system_prompt=system_prompt,
                response_format=_BatchDesignResponse,
                workspace_root=workspace_root,
                writable_roots=[workspace_root],
                skills=[SKILLS_SOURCE] if selected else [],
                permitted_skill_names=selected,
                memory=[],
                tools=build_traceability_tools(node_id=primary_node, log_cb=self._log),
            )
            raw = await ainvoke_stage_agent(
                agent,
                message=message,
                context=AgentRuntimeContext(
                    node_id=primary_node,
                    phase="DESIGN",
                    app_type=app_type,
                    workspace_root=workspace_root,
                    requirement_path=self.requirement_path,
                ),
                thread_id=f"batch:DESIGN:{len(node_payloads)}",
                label="DesignBatch",
                log_cb=self._log,
            )
        except Exception as exc:  # pragma: no cover - defensive
            await self._log(
                "DesignBatch",
                f"Batch design call raised: {exc}; all leaves fall back to individual DESIGN.",
                status="error",
                node_id=primary_node,
            )
            return result

        by_node: dict[str, dict[str, Any]] = {}
        for entry in (raw.get("leaves") or []):
            if not isinstance(entry, dict):
                continue
            nid = str(entry.get("node_id") or "").strip()
            if nid:
                by_node[nid] = entry

        for node_id, _requirement_data in node_payloads:
            entry = by_node.get(node_id)
            if not entry:
                await self._log(
                    "DesignBatch",
                    "Batch response contained no entry for this leaf; retrying it individually.",
                    status="warning",
                    node_id=node_id,
                )
                continue
            interfaces = [dict(i) for i in (entry.get("interfaces") or []) if isinstance(i, dict)]
            tests = normalize_test_manifest_payload({"tests": entry.get("tests") or []})
            if not interfaces or not tests:
                await self._log(
                    "DesignBatch",
                    f"Batch returned interfaces={len(interfaces)} tests={len(tests)}; retrying this leaf individually.",
                    status="warning",
                    node_id=node_id,
                )
                continue
            files_written = [
                normalize_workspace_relative_path(str(p), self.workspace_path)
                for p in (entry.get("files_written") or [])
                if p
            ]
            try:
                prepared_interfaces = self._prepare_interfaces(node_id, interfaces)
                stored_tests = self._prepare_tests(node_id=node_id, tests=tests)
            except ValueError as exc:
                await self._log("DesignBatch", str(exc), status="error", node_id=node_id)
                continue
            self.traceability.clear_node_design_artifacts(node_id)
            self._store_prepared_interfaces(node_id, prepared_interfaces)
            self._store_prepared_tests(stored_tests)
            context_pipeline.cache.invalidate_file_layers(node_id)
            context_pipeline.cache.invalidate_db_layers(node_id)
            self._update_node_session(
                node_id,
                {
                    "interfaces": prepared_interfaces,
                    "materialized_files": [f for f in files_written if f],
                    "test_artifacts": stored_tests,
                    "phase_status": {"design": "completed", "test": "completed"},
                },
            )
            result[node_id] = True

        await self._log(
            "DesignBatch",
            (
                f"[batch-gen] designed {sum(1 for v in result.values() if v)}/{len(node_payloads)} "
                f"leaf(s) in one call (batch size {len(node_payloads)})."
            ),
            node_id=primary_node,
        )
        return result

    async def run_test_generation_phase(
        self,
        node_id: str,
        requirement_data: dict[str, Any],
        *,
        intent: str,
        replace_test_id: str | None = None,
    ) -> bool:
        normalized_intent = str(intent or "").strip()
        if not normalized_intent:
            await self._log("TestGenerator", "A test intent is required.", status="error", node_id=node_id)
            return False
        if requirement_data.get("children_ids"):
            await self._log(
                "TestGenerator",
                "Intent-based test generation is only available for leaf requirement nodes.",
                status="error",
                node_id=node_id,
            )
            return False

        normalized_replace_test_id = str(replace_test_id or "").strip()
        existing_tests = self.traceability.list_tests(req_id=node_id)
        existing_by_id = {
            str(test.get("test_id", "") or "").strip(): test
            for test in existing_tests
            if str(test.get("test_id", "") or "").strip()
        }
        existing_test = existing_by_id.get(normalized_replace_test_id)
        if normalized_replace_test_id and existing_test is None:
            await self._log(
                "TestGenerator",
                f"Test regeneration received unregistered test id: {normalized_replace_test_id}",
                status="error",
                node_id=node_id,
            )
            return False
        action = f"Regenerating test {normalized_replace_test_id}" if normalized_replace_test_id else "Adding tests"
        await self._log("TestGenerator", f"{action} for intent: {normalized_intent}", node_id=node_id)
        tests, _ = await self.test_generator.run(
            node_id=node_id,
            requirement_data=requirement_data,
            test_intent=normalized_intent,
            replace_test_id=normalized_replace_test_id or None,
        )
        if tests is None:
            await self._log("TestGenerator", "Test generation did not return a valid manifest.", status="error", node_id=node_id)
            return False
        try:
            prepared_tests = self._prepare_tests(node_id=node_id, tests=tests)
        except ValueError as exc:
            await self._log("TestGenerator", str(exc), status="error", node_id=node_id)
            return False

        existing_ids = set(existing_by_id)
        if normalized_replace_test_id:
            generated_ids = {str(test.get("test_id", "") or "").strip() for test in prepared_tests}
            if generated_ids != {normalized_replace_test_id}:
                await self._log(
                    "TestGenerator",
                    f"Test regeneration must return only the selected test id: {normalized_replace_test_id}",
                    status="error",
                    node_id=node_id,
                )
                return False
            original_path = str(existing_test.get("file_path", "") or "").strip()
            generated_path = str(prepared_tests[0].get("file_path", "") or "").strip() if prepared_tests else ""
            if original_path != generated_path:
                await self._log(
                    "TestGenerator",
                    f"Test regeneration must preserve the selected test file path: {original_path}",
                    status="error",
                    node_id=node_id,
                )
                return False
        duplicate_ids = sorted(
            test_id
            for test_id in (str(test.get("test_id", "") or "").strip() for test in prepared_tests)
            if test_id in existing_ids and test_id != normalized_replace_test_id
        )
        if duplicate_ids:
            await self._log(
                "TestGenerator",
                f"Test generation would overwrite existing test id(s): {', '.join(duplicate_ids)}",
                status="error",
                node_id=node_id,
            )
            return False

        self._store_prepared_tests(prepared_tests)
        context_pipeline.cache.invalidate_file_layers(node_id)
        context_pipeline.cache.invalidate_db_layers(node_id)
        await self._log(
            "TestGenerator",
            f"Stored {len(prepared_tests)} test mapping item(s) for the requested generation.",
            node_id=node_id,
        )
        return True

    async def run_implement_phase(
        self,
        node_id: str,
        requirement_data: dict[str, Any],
        test_ids: list[str] | None = None,
    ) -> bool:
        is_non_leaf = bool(requirement_data.get("children_ids"))
        selected_test_ids = list(dict.fromkeys(str(test_id or "").strip() for test_id in test_ids or [] if str(test_id or "").strip()))
        if selected_test_ids and is_non_leaf:
            await self._log(
                "TestDrivenDeveloper",
                "Selected-test TDD is only available for leaf requirement nodes.",
                status="error",
                node_id=node_id,
            )
            return False
        if is_non_leaf:
            interfaces = self.traceability.list_interfaces(req_id=node_id)
            self._mark_interfaces_implemented(interfaces)
            self._update_node_session(
                node_id,
                {
                    "phase_status": {"implement": "completed"},
                    "result_state": "CONVERGED",
                },
            )
            await self._log(
                "TestDrivenDeveloper",
                "Non-leaf node completed directly after interface materialization; no TDD batch was scheduled.",
                node_id=node_id,
            )
            return True

        del requirement_data
        if not selected_test_ids:
            self._update_node_session(node_id, {"phase_status": {"implement": "in_progress"}})
        interfaces = self.traceability.list_interfaces(req_id=node_id)
        # ARC_SCAFFOLD_STUBS: pre-create empty route stubs so IMPLEMENT only fills
        # handler bodies instead of creating files + boilerplate from scratch.
        # Compliance: stubs contain NO business logic (empty router, no routes).
        await self._prefill_route_stubs(node_id, interfaces)
        tests = self.traceability.list_tests(req_id=node_id)
        if selected_test_ids:
            tests_by_id = {
                str(test.get("test_id", "") or "").strip(): test
                for test in tests
                if str(test.get("test_id", "") or "").strip()
            }
            unknown_test_ids = [test_id for test_id in selected_test_ids if test_id not in tests_by_id]
            if unknown_test_ids:
                await self._log(
                    "TestDrivenDeveloper",
                    f"Selected-test TDD received unregistered test id(s): {', '.join(unknown_test_ids)}",
                    status="error",
                    node_id=node_id,
                )
                return False
            tests = [tests_by_id[test_id] for test_id in selected_test_ids]
        if not tests:
            await self._log(
                "TestDrivenDeveloper",
                "No node-local tests were registered; skipping TDD implementation for this node.",
                node_id=node_id,
            )
            if not selected_test_ids:
                self._mark_interfaces_implemented(interfaces)
                self._update_node_session(node_id, {"phase_status": {"implement": "completed"}})
            return True

        final_ok = await self._run_tdd_for_node(
            node_id=node_id,
            tests=tests,
        )
        if final_ok and not selected_test_ids:
            self._mark_interfaces_implemented(interfaces)
        if not selected_test_ids:
            self._update_node_session(
                node_id,
                {"phase_status": {"implement": "completed" if final_ok else "failed"}},
            )
        return final_ok

    async def _prefill_route_stubs(self, node_id: str, interfaces: list[dict[str, Any]]) -> int:
        """ARC_SCAFFOLD_STUBS (opt-in, default off): pre-create empty Express route
        modules for a leaf's interfaces so the IMPLEMENT agent only fills handler
        bodies instead of authoring files + boilerplate from scratch.

        Compliance boundary: stubs export an EMPTY router (no routes, no logic).
        They cannot shadow real routes or return wrong responses, and contain no
        task-specific answer. The agent still does 100% of the real implementation.
        """
        if _os.environ.get("ARC_SCAFFOLD_STUBS", "").strip() != "1":
            return 0
        routes_dir = _os.path.join(self.workspace_path, "src", "routes")
        if not _os.path.isdir(routes_dir):
            return 0
        created = 0
        for iface in interfaces or []:
            if not isinstance(iface, dict):
                continue
            fpath = str(iface.get("file_path") or "").strip().replace("\\", "/")
            if not fpath:
                continue
            # Only pre-fill the auto-loaded, conflict-free routes directory.
            norm = fpath.lower()
            if not (norm.startswith("src/routes/") or "/src/routes/" in norm):
                continue
            target = (
                fpath
                if _os.path.isabs(fpath)
                else _os.path.join(self.workspace_path, fpath)
            )
            if _os.path.exists(target):
                continue
            try:
                parent = _os.path.dirname(target)
                if parent:
                    _os.makedirs(parent, exist_ok=True)
            except OSError:
                continue
            iid = str(iface.get("interface_id") or "").strip() or "unknown"
            iname = str(iface.get("name") or "").strip()
            itype = str(iface.get("type") or "").strip().upper()
            # NOTE: the interface contract carries `interface_id/name/type/file_path`
            # but NOT the HTTP method/path, so we cannot pre-declare concrete routes
            # without guessing. We pre-create a fill-ready module (express + empty
            # router + a commented handler template) so the agent only fills bodies.
            stub = (
                "const express = require('express');\n"
                "const router = express.Router();\n\n"
                f"// ARC scaffold stub for {iid}"
                + (f" ({itype})" if itype else "")
                + (f": {iname}" if iname else "")
                + "\n"
                "// Pre-created by the build; contains NO business logic.\n"
                "// TODO(agent): implement the route handler(s) for this interface.\n"
                "// Example shape:\n"
                "//   router.get('/path', async (req, res) => { /* implement */ });\n\n"
                "module.exports = router;\n"
            )
            try:
                with open(target, "w", encoding="utf-8") as fh:
                    fh.write(stub)
                created += 1
            except OSError:
                continue
        if created:
            await self._log(
                "Scaffold",
                f"Pre-filled {created} empty route stub(s) for leaf {node_id} (agent fills bodies).",
                status="info",
                node_id=node_id,
            )
        return created

    async def _run_tdd_for_node(
        self,
        *,
        node_id: str,
        tests: list[dict[str, Any]],
    ) -> bool:
        previous_failure_summary = str(sessions.load_node_session(node_id).get("recent_failure_summary", "") or "")
        groups: dict[str, list[dict[str, Any]]] = {}
        for test in tests:
            test_type = str(test.get("type", "") or "").strip()
            if not test_type:
                continue
            normalized_type = test_type.lower()
            groups.setdefault(normalized_type, []).append(test)

        ordered_types = [test_type for test_type in TDD_BATCH_ORDER if groups.get(test_type.lower())]
        if not ordered_types:
            return True

        usage_by_type = {test_type: 0 for test_type in ordered_types}
        result_by_type: dict[str, str] = {}
        await self._log(
            "TestDrivenDeveloper",
            "Running leaf TDD sessions in ordered layers with independent budgets: " + " -> ".join(ordered_types) + ".",
            node_id=node_id,
        )
        active_test_type: str | None = None

        async def run_requested_tests(requested_type: str | None = None, requested_files: list[str] | None = None) -> str:
            requested = str(requested_type or "").strip()
            if active_test_type is None:
                return (
                    "Exit Code: 1\n"
                    "STDERR:\n"
                    "No active TDD test layer is currently scheduled.\n"
                )
            if requested.lower() in {"", "all", "current", "next"}:
                selected_type = active_test_type
            else:
                selected_type = canonical_test_type(requested)
                if selected_type is None or selected_type not in ordered_types:
                    return (
                        "Exit Code: 1\n"
                        "STDERR:\n"
                        f"Unsupported current-node test_type={requested!r}. "
                        f"Available ordered layers: {', '.join(ordered_types)}.\n"
                    )
                if selected_type != active_test_type:
                    return (
                        "Exit Code: 1\n"
                        "STDERR:\n"
                        f"The active TDD layer is `{active_test_type}`, but run_tests requested `{selected_type}`. "
                        "The system attempts layers in Unit -> Integration -> E2E order with independent budgets.\n"
                    )

            selected_files = [
                path
                for value in (requested_files or collect_test_files(groups[selected_type.lower()]))
                if (path := normalize_workspace_relative_path(value, self.workspace_path))
            ]
            registered_files = {
                str(item.get("file_path", "") or "").strip()
                for item in groups[selected_type.lower()]
                if str(item.get("file_path", "") or "").strip()
            }
            unknown = [path for path in selected_files if path not in registered_files]
            if unknown:
                return (
                    "Exit Code: 1\n"
                    "STDERR:\n"
                    f"run_tests({selected_type}) may only execute registered {selected_type} tests for the current node. "
                    f"Unknown files: {', '.join(unknown)}\n"
                )
            used = usage_by_type[selected_type]
            if used >= TDD_RUN_TESTS_BUDGET:
                await self._log(
                    "TestDrivenDeveloper",
                    f"`run_tests` {selected_type} budget exhausted at {used}/{TDD_RUN_TESTS_BUDGET}.",
                    status="error",
                    node_id=node_id,
                )
                return (
                    "Exit Code: 1\n"
                    "STDERR:\n"
                    f"run_tests budget exhausted for {selected_type}: {used}/{TDD_RUN_TESTS_BUDGET}.\n"
                )
            usage_by_type[selected_type] = used + 1
            await self._log(
                "TestDrivenDeveloper",
                f"`run_tests` {selected_type} usage {usage_by_type[selected_type]}/{TDD_RUN_TESTS_BUDGET}.",
                node_id=node_id,
            )
            output = await self.app_handler.run_test_group(selected_type, selected_files)
            await self._log(
                "TestDrivenDeveloper",
                (
                    "run_tests raw output\n"
                    f"test_type={selected_type}\n"
                    f"attempt={usage_by_type[selected_type]}/{TDD_RUN_TESTS_BUDGET}\n"
                    f"test_files={json.dumps(selected_files, ensure_ascii=False)}\n"
                    "----- BEGIN RAW TEST OUTPUT -----\n"
                    f"{output.rstrip()}\n"
                    "----- END RAW TEST OUTPUT -----"
                ),
                status="debug",
                node_id=node_id,
            )
            parsed_result = parse_test_results(output)
            exit_code = int(parsed_result.get("exit_code", -1))
            passed = exit_code == 0
            await self._log(
                "TestDrivenDeveloper",
                (
                    f"`run_tests` {selected_type} {'passed' if passed else 'failed'} "
                    f"with Exit Code: {exit_code} "
                    f"on attempt {usage_by_type[selected_type]}/{TDD_RUN_TESTS_BUDGET}: "
                    f"{', '.join(selected_files)}"
                ),
                status="ok" if passed else "error",
                node_id=node_id,
            )
            result_by_type[selected_type] = output
            next_index = ordered_types.index(selected_type) + 1
            next_type = ordered_types[next_index] if next_index < len(ordered_types) else None
            if passed and next_type:
                output += (
                    "\n\nARC_TEST_LAYER_STATUS:\n"
                    f"- {selected_type} passed.\n"
                    f"- The system will advance to the next test layer: {next_type}.\n"
                    "- Do not return IMPLEMENTED until all scheduled layers have been attempted and passed.\n"
                )
            elif passed:
                output += (
                    "\n\nARC_TEST_LAYER_STATUS:\n"
                    f"- {selected_type} passed.\n"
                    "- This is the last scheduled test layer. You may return IMPLEMENTED only if all earlier scheduled layers also passed.\n"
                )
            return output

        output = ""
        session_count = 0
        max_sessions = max(1, TDD_RUN_TESTS_BUDGET * len(ordered_types))
        for ordered_type in ordered_types:
            active_test_type = ordered_type
            previous_failure_summary = str(sessions.load_node_session(node_id).get("recent_failure_summary", "") or "")
            while parse_test_results(result_by_type.get(ordered_type, "")).get("exit_code") != 0:
                used_before = usage_by_type.get(ordered_type, 0)
                if used_before >= TDD_RUN_TESTS_BUDGET:
                    break
                if session_count >= max_sessions:
                    await self._log(
                        "TestDrivenDeveloper",
                        f"TDD stopped after {session_count} agent session(s); continuing layer summary with collected results.",
                        status="error",
                        node_id=node_id,
                    )
                    break

                session_count += 1
                if used_before > 0:
                    await self._log(
                        "TestDrivenDeveloper",
                        (
                            f"Resuming TDD agent session {session_count} for `{ordered_type}`; "
                            f"run_tests usage is {used_before}/{TDD_RUN_TESTS_BUDGET}."
                        ),
                        node_id=node_id,
                    )
                output = await self.test_driven_developer.run(
                    node_id=node_id,
                    test_files=collect_test_files(tests),
                    test_type=ordered_type,
                    node_tests=tests,
                    previous_failure_summary=previous_failure_summary,
                    run_tests_budget=None,
                    run_tests_usage=None,
                    run_tests_executor=run_requested_tests,
                )

                latest_result = result_by_type.get(ordered_type, "")
                previous_failure_summary = (
                    self.test_driven_developer.get_last_verifier_report()
                    or summarize_batch_output(latest_result or output)
                )
                used_after = usage_by_type.get(ordered_type, 0)
                if parse_test_results(latest_result).get("exit_code") == 0:
                    break
                if used_after >= TDD_RUN_TESTS_BUDGET:
                    break
                if used_after == used_before:
                    await self._log(
                        "TestDrivenDeveloper",
                        (
                            f"TDD agent session ended without calling run_tests for `{ordered_type}`; "
                            "moving to the next scheduled layer with a fresh budget."
                        ),
                        status="error",
                        node_id=node_id,
                    )
                    break
            active_test_type = None
            if parse_test_results(result_by_type.get(ordered_type, "")).get("exit_code") != 0:
                await self._log(
                    "TestDrivenDeveloper",
                    f"Advancing past `{ordered_type}` without a passing result; the next scheduled layer will start with its own budget.",
                    status="warning",
                    node_id=node_id,
                )

        final_ok = True
        failure_summaries: list[str] = []
        failed_types: list[str] = []
        for test_type in ordered_types:
            latest_result = result_by_type.get(test_type, "")
            group_passed = parse_test_results(latest_result).get("exit_code") == 0
            status_by_test_id = {
                str(test.get("test_id", "")).strip(): group_passed
                for test in groups[test_type.lower()]
                if str(test.get("test_id", "")).strip()
            }
            self.traceability.set_test_pass_statuses(status_by_test_id)
            if group_passed:
                await self._log(
                    "TestDrivenDeveloper",
                    f"TDD batch `{test_type}` passed after {usage_by_type.get(test_type, 0)}/{TDD_RUN_TESTS_BUDGET} run_tests call(s).",
                    node_id=node_id,
                )
                continue
            final_ok = False
            failed_types.append(test_type)
            failure_summary = (
                summarize_batch_output(latest_result)
                if latest_result
                else self.test_driven_developer.get_last_verifier_report()
                or summarize_batch_output(output)
            )
            failure_summaries.append(f"{test_type}: {failure_summary}")
            used = usage_by_type.get(test_type, 0)
            detail = "budget exhausted" if used >= TDD_RUN_TESTS_BUDGET else "agent session ended before this layer passed"
            await self._log(
                "TestDrivenDeveloper",
                f"TDD batch `{test_type}` did not pass after {used}/{TDD_RUN_TESTS_BUDGET} run_tests call(s); {detail}.",
                status="error",
                node_id=node_id,
            )

        context_pipeline.cache.invalidate_db_layers(node_id)
        context_pipeline.cache.invalidate_file_layers(node_id)
        failure_summary = "\n\n".join(failure_summaries)
        self._update_node_session(
            node_id,
            {
                "recent_failure_summary": failure_summary,
                "tdd_handoff": {
                    "last_test_type": failed_types[-1] if failed_types else ordered_types[-1],
                    "last_failed_output_summary": failure_summary,
                    "modified_files": [],
                },
            },
        )
        if not final_ok:
            return False

        unexpected_types = sorted(set(groups) - {item.lower() for item in TDD_BATCH_ORDER})
        if unexpected_types:
            await self._log(
                "TestDrivenDeveloper",
                f"Ignoring unsupported test batch type(s): {', '.join(unexpected_types)}.",
                status="warning",
                node_id=node_id,
            )

        return True

    def _prepare_interfaces(self, node_id: str, interfaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
        prepared: list[dict[str, Any]] = []
        for interface in interfaces:
            interface_id = str(interface.get("interface_id", "")).strip()
            if not interface_id:
                continue
            existing = self.traceability.get_interface(interface_id)
            if existing:
                try:
                    existing_content = json.loads(str(existing.get("content") or "{}"))
                except json.JSONDecodeError:
                    existing_content = {}
                if isinstance(existing_content, dict):
                    interface = {**existing_content, **interface}
            interface_type = str(interface.get("type") or (existing or {}).get("type") or "").strip().upper()
            if interface_type not in ALLOWED_INTERFACE_TYPES:
                raise ValueError(
                    f"Generated interface `{interface_id}` has invalid `type` {interface.get('type')!r}. "
                    "Interface type must be one of UI, API, FUNC, or DB."
                )
            normalized = {
                **interface,
                "interface_id": interface_id,
                "req_id": node_id,
                "type": interface_type,
                "file_path": (
                    normalize_workspace_relative_path(interface.get("file_path"), self.workspace_path)
                    or ((existing or {}).get("file_path") if existing else "")
                ),
                "first_line": str(interface.get("first_line") or (existing or {}).get("first_line") or "").strip(),
                "callers": normalize_string_list(interface.get("callers")) or normalize_string_list((existing or {}).get("callers")),
                "callees": normalize_string_list(interface.get("callees")) or normalize_string_list((existing or {}).get("callees")),
                "_existing_req_ids": list(existing.get("req_ids", [])) if existing else [],
                "_existing_implemented": bool(existing.get("implemented")) if existing else False,
            }
            prepared.append(normalized)
        return prepared

    def _store_prepared_interfaces(self, node_id: str, interfaces: list[dict[str, Any]]) -> None:
        for interface in interfaces:
            interface_id = str(interface.get("interface_id", "")).strip()
            if not interface_id:
                continue
            req_ids = normalize_string_list(interface.get("_existing_req_ids"))
            if node_id not in req_ids:
                req_ids.append(node_id)
            self.traceability.upsert_interface(
                interface_id=interface_id,
                req_ids=req_ids,
                type=str(interface.get("type", "") or "").strip().upper(),
                content=json.dumps(_strip_internal_fields(interface), ensure_ascii=False),
                file_path=str(interface.get("file_path", "") or "").strip() or None,
                first_line=str(interface.get("first_line", "") or "").strip() or None,
                implemented=bool(interface.get("_existing_implemented")),
                callers=normalize_string_list(interface.get("callers")),
                callees=normalize_string_list(interface.get("callees")),
            )
            self._register_interface_edges(node_id, interface_id, interface)

    def _prepare_tests(
        self,
        *,
        node_id: str,
        tests: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        stored: list[dict[str, Any]] = []
        generated_ids: set[str] = set()
        for test in tests:
            if not isinstance(test, dict):
                continue
            raw_test_id = str(test.get("test_id", "")).strip()
            if not raw_test_id:
                continue
            file_path = normalize_workspace_relative_path(test.get("file_path"), self.workspace_path)
            test_type = str(test.get("type", "") or "").strip()
            if not test_type:
                raise ValueError(f"Generated test `{raw_test_id}` is missing `type`.")
            if not file_path:
                raise ValueError(f"Generated test `{raw_test_id}` is missing `file_path`.")
            validation_error = self.app_handler.validate_test_path(test_type, file_path)
            if validation_error:
                raise ValueError(f"Generated test `{raw_test_id}` has an invalid path. {validation_error}")
            if raw_test_id in generated_ids:
                raise ValueError(f"Generated duplicate test id `{raw_test_id}`.")
            generated_ids.add(raw_test_id)
            stored_item = {
                **test,
                "test_id": raw_test_id,
                "req_id": node_id,
                "type": test_type,
                "file_path": file_path,
                "interface_ids": normalize_string_list(test.get("interface_ids")),
                "first_line": str(test.get("first_line", "")).strip(),
            }
            stored.append(stored_item)
        return stored

    def _store_prepared_tests(self, tests: list[dict[str, Any]]) -> None:
        for test in tests:
            self.traceability.upsert_test(
                test_id=str(test.get("test_id", "") or "").strip(),
                req_id=str(test.get("req_id", "") or "").strip(),
                interface_ids=normalize_string_list(test.get("interface_ids")),
                type=str(test.get("type", "") or "").strip(),
                file_path=str(test.get("file_path", "") or "").strip() or None,
                first_line=str(test.get("first_line", "") or "").strip() or None,
                passed=None,
            )

    def _register_interface_edges(self, node_id: str, interface_id: str, interface: dict[str, Any]) -> None:
        for caller_id in normalize_string_list(interface.get("callers")):
            caller = self.traceability.get_interface(caller_id)
            if not caller:
                continue
            for source_req_id in caller.get("req_ids", []):
                if source_req_id and source_req_id != node_id:
                    self.traceability.insert_call_edge(
                        source_req_id=source_req_id,
                        target_req_id=node_id,
                        from_interface_id=caller_id,
                        to_interface_id=interface_id,
                        edge_type="cross_req",
                    )
        for callee_id in normalize_string_list(interface.get("callees")):
            callee = self.traceability.get_interface(callee_id)
            if not callee:
                continue
            for target_req_id in callee.get("req_ids", []):
                if target_req_id and target_req_id != node_id:
                    self.traceability.insert_call_edge(
                        source_req_id=node_id,
                        target_req_id=target_req_id,
                        from_interface_id=interface_id,
                        to_interface_id=callee_id,
                        edge_type="cross_req",
                    )

    def _mark_interfaces_implemented(self, interfaces: list[dict[str, Any]]) -> None:
        for interface in interfaces:
            interface_id = str(interface.get("interface_id", "") or "").strip()
            if interface_id:
                self.traceability.set_interface_implemented(interface_id, True)

    def _update_node_session(self, node_id: str, patch: dict[str, Any]) -> None:
        sessions.merge_node_session(node_id, patch)
        context_pipeline.cache.invalidate_db_layers(node_id)

    async def _log(
        self,
        agent_name: str,
        message: str,
        status: str | None = None,
        node_id: str | None = None,
    ) -> None:
        if self.log_cb is None:
            return
        result = self.log_cb(agent_name, message, status, node_id)
        if hasattr(result, "__await__"):
            await result


def collect_test_files(tests: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for test in tests:
        file_path = str(test.get("file_path", "")).strip()
        if file_path and file_path not in seen:
            seen.append(file_path)
    return seen


def canonical_test_type(value: str) -> str | None:
    normalized = str(value or "").strip().lower()
    for test_type in TDD_BATCH_ORDER:
        if normalized == test_type.lower():
            return test_type
    return None


def summarize_interface_artifacts(interfaces: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(interfaces),
        "items": [
            {
                "id": str(item.get("interface_id", "") or "").strip(),
                "type": str(item.get("type", "") or "").strip(),
                "path": str(item.get("file_path", "") or "").strip(),
                "responsibility": str(item.get("responsibility", "") or item.get("name", "") or "").strip(),
            }
            for item in interfaces
            if isinstance(item, dict)
        ],
    }


def summarize_test_artifacts(tests: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(tests),
        "items": [
            {
                "id": str(item.get("test_id", "") or "").strip(),
                "type": str(item.get("type", "") or "").strip(),
                "path": str(item.get("file_path", "") or "").strip(),
                "interfaces": normalize_string_list(item.get("interface_ids")),
            }
            for item in tests
            if isinstance(item, dict)
        ],
    }


def normalize_workspace_relative_path(value: Any, workspace_path: str) -> str:
    path = normalize_windows_extended_prefix_text(value)
    if not path:
        return ""
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    if path == "/workspace":
        return ""
    if path.startswith("/workspace/"):
        return path[len("/workspace/") :].lstrip("/")

    workspace = normalize_windows_extended_prefix_text(Path(workspace_path).expanduser().resolve()).rstrip("/")
    if path == workspace:
        return ""
    if path.startswith(workspace + "/"):
        return path[len(workspace) + 1 :].lstrip("/")
    return path.lstrip("/")


def summarize_batch_output(batch_output: str, max_lines: int = 30) -> str:
    lines = [line for line in (batch_output or "").splitlines() if line.strip()]
    if len(lines) > max_lines:
        lines = ["...[truncated]", *lines[-max_lines:]]
    return "\n".join(lines)


def normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def _strip_internal_fields(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if not str(key).startswith("_")}
