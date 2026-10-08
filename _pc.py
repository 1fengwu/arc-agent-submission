#!/usr/bin/env python3
"""Parallel requirement-tree compiler for ARC (EXPERIMENTAL).

Splits a requirement tree into N leaf shards, runs the EXISTING (validated) ARC
compile engine in N independent OS processes (each with its own workspace and web
port), then merges the per-shard ADDITIVE artifacts into one master workspace.

Why the merge is safe-by-construction
-------------------------------------
Each leaf creates NAMESPACED files only:
  * backend/src/routes/<node_id>.js        (Express router, absolute paths)
  * backend/src/migrations/<node_id>.sql   (CREATE TABLE IF NOT EXISTS)
  * backend/src/routes/<node_id>.deps.json ({"pkg": "version"})
  * frontend/src/features/<node_id>/*       (components/hooks/api)
  * frontend/src/routes/<node_id>.jsx       (page component)

The merge copies ONLY those per-leaf files into the master workspace. It NEVER
copies a shard's app.js / init_db.js / package.json; instead it unions the
.deps.json entries into the master package.json and runs `npm install` once.
Leaves whose shard failed are reported and can be retried serially.

Frontend caveat: shared frontend shell files (App.jsx / main.jsx) are taken from
shard 0 only, because parallel leaves may each edit them. Keep frontend route
wiring minimal and place page bodies in the feature folder.

Usage
-----
  python scripts/parallel_compile.py <requirement_dir_or_yaml> \
      -o <master_workspace> --shards 4 --base-port 3100

VALIDATE FIRST (smoke test on a few leaves before the real 24h window):
  python scripts/parallel_compile.py <req> -o /tmp/smoke --shards 3 \
      --base-port 4100 --smoke 6
Then verify: backend/src/routes/ should contain 6 files and
  node -e "require('./backend/src/app.js')"  should load with no error.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / "template"


def load_tree(path: Path) -> tuple[dict, Path]:
    p = path.resolve()
    if p.is_dir():
        for c in ("requirements.yaml", "requirements.yml"):
            cand = p / c
            if cand.is_file():
                return yaml.safe_load(cand.read_text(encoding="utf-8")), p
        raise FileNotFoundError(f"No requirements.yaml in {p}")
    return yaml.safe_load(p.read_text(encoding="utf-8")), p.parent


def collect_leaves(node: dict, out: list) -> None:
    nid = str(node.get("id") or "").strip()
    children = [c for c in (node.get("children") or []) if isinstance(c, dict)]
    if not children and nid:
        out.append(nid)
    for c in children:
        collect_leaves(c, out)


def prune(node: dict, keep: set) -> dict | None:
    nid = str(node.get("id") or "").strip()
    children = [c for c in (node.get("children") or []) if isinstance(c, dict)]
    kept_children = []
    for c in children:
        pc = prune(c, keep)
        if pc is not None:
            kept_children.append(pc)
    if nid in keep:
        return {**node, "children": kept_children}
    if kept_children:
        return {**node, "children": kept_children}
    return None


def split_leaves(leaves: list, shards: int) -> list[set]:
    buckets = [set() for _ in range(shards)]
    for i, lid in enumerate(leaves):
        buckets[i % shards].add(lid)
    return buckets


async def run_shard(shard_dir: Path, ws: Path, port: int, req_type: str) -> int:
    cmd = [
        sys.executable, "main.py",
        str(shard_dir / "requirements.yaml"),
        "-o", str(ws), "-t", req_type, "--port", str(port), "--clean",
    ]
    # Isolate npm cache per shard so concurrent `npm install` calls do not
    # corrupt the shared %AppData%/npm-cache (which throws
    # "Cannot read properties of null (reading 'edgesOut')" under parallelism).
    env = dict(os.environ)
    env["npm_config_cache"] = str(shard_dir / "npm_cache")

    # CRITICAL: the platform injects ARCBENCH_TRACEABILITY_DIR pointing at the
    # SHARED master workspace (.arc/traceability). If we inherit it, all shards
    # race on the same node_states.json via atomic rename() and crash with
    #   FileNotFoundError: ...node_states.json.tmp -> node_states.json
    # followed by "Requirement node X not found in database" / DESIGN failures.
    # Force a per-shard traceability DB so each shard's compiler state is fully
    # isolated. Shards only need their own leaves; the merge step does not read
    # any shard's traceability DB (it unions code/test artifacts only).
    shard_arc = ws / ".arc" / "traceability"
    shard_arc.mkdir(parents=True, exist_ok=True)
    env["ARCBENCH_TRACEABILITY_DIR"] = str(shard_arc)
    # Recursion guard: tell the shard process to run the SERIAL engine. Without
    # this, the shard re-invokes main.py which would re-enter parallel_compile
    # and fork shards-of-shards forever.
    env["ARCBENCH_SHARD"] = "1"

    # Cross-shard non-leaf contract cache (ARC_NONLEAF_SHARE): a directory shared by
    # all shards (sitting next to the shared token_budget.json) where designed
    # non-leaf interface contracts are cached and reused, eliminating 4x duplicate
    # DESIGN spend on ROOT/composition nodes. Phases.py reads/writes it only when
    # ARC_NONLEAF_SHARE=1 is also set in the environment.
    try:
        env["ARC_NONLEAF_CACHE_DIR"] = str(ws.parent / ".nonleaf_cache")
    except Exception:
        pass

    # Cross-shard token budget: a single shared counter for THIS task so that all
    # 4 shards together stay within ARC_TOKEN_BUDGET tokens (the platform bills
    # the whole task's model usage). Without this, 4 independent shards would each
    # spend up to the limit => 4x the intended cost.
    try:
        from agents.model.token_budget import DEFAULT_PER_TASK_BUDGET

        # Per-task budget tiers (v16). IMPORTANT: the platform isolates the 4
        # shards, so the "shared" token_budget.json counter does NOT aggregate
        # across shards in practice -- each shard runs its own full budget. v14
        # therefore burned 4x26M = 104M/task. Effective cost per task is
        # budget_limit * shard_count, where shard_count is decided in main.py as
        # 1 if budget_limit < 8M else 4. So:
        #   - sheet  : simpler task -> 7.5M, and <8M forces SERIAL (1 shard), so
        #              sheet costs ~7.5M total (cheap, still plenty for simple task).
        #   - github : harder task -> 8M/shard * 4 shards = ~32M total (was 104M).
        # An explicit ARC_TOKEN_BUDGET override always wins. ARCBENCH_SHARDS must
        # NOT be pinned in .env, or it would force 4 shards even for sheet.
        _override = int(str(os.environ.get("ARC_TOKEN_BUDGET", "")).strip() or 0)
        _ttype = (os.environ.get("ARCBENCH_TASK_TYPE", "") or "").lower()
        if _override:
            budget_limit = _override
        elif "sheet" in _ttype:
            budget_limit = 7_500_000
        elif "github" in _ttype:
            budget_limit = 8_000_000
        else:
            budget_limit = DEFAULT_PER_TASK_BUDGET
        budget_file = ws.parent / "token_budget.json"
        # only initialise the shared counter ONCE so concurrent shards reuse it
        if not budget_file.exists():
            budget_file.write_text(json.dumps({"used": 0, "limit": budget_limit}), encoding="utf-8")
        env["ARC_TOKEN_BUDGET_FILE"] = str(budget_file)
        env["ARC_TOKEN_BUDGET"] = str(budget_limit)
    except Exception as _budget_err:
        print(f"[warn] token budget setup failed: {_budget_err}", flush=True)

    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(REPO_ROOT), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        text = line.decode("utf-8", "replace").rstrip()
        if text:
            print(f"[shard:{port}] {text}", flush=True)
    return await proc.wait()


def copy_additive(shard_ws: Path, master: Path, shard_id: str, deps: dict, take_shell_from_shard0: bool) -> None:
    # backend routes + deps manifests
    src_routes = shard_ws / "backend" / "src" / "routes"
    if src_routes.is_dir():
        dst_routes = master / "backend" / "src" / "routes"
        dst_routes.mkdir(parents=True, exist_ok=True)
        for f in src_routes.iterdir():
            if f.suffix == ".js" or f.name.endswith(".deps.json"):
                shutil.copy2(f, dst_routes / f.name)
                if f.name.endswith(".deps.json"):
                    try:
                        deps.update(json.loads(f.read_text(encoding="utf-8")))
                    except Exception:
                        pass
    # backend migrations
    src_mig = shard_ws / "backend" / "src" / "migrations"
    if src_mig.is_dir():
        dst_mig = master / "backend" / "src" / "migrations"
        dst_mig.mkdir(parents=True, exist_ok=True)
        for f in src_mig.glob("*.sql"):
            shutil.copy2(f, dst_mig / f.name)
    # frontend feature files (additive)
    src_feat = shard_ws / "frontend" / "src" / "features"
    if src_feat.is_dir():
        dst_feat = master / "frontend" / "src" / "features"
        for item in src_feat.iterdir():
            if item.is_dir():
                shutil.copytree(item, dst_feat / item.name, dirs_exist_ok=True)
    src_froutes = shard_ws / "frontend" / "src" / "routes"
    if src_froutes.is_dir():
        dst_froutes = master / "frontend" / "src" / "routes"
        dst_froutes.mkdir(parents=True, exist_ok=True)
        for f in src_froutes.iterdir():
            shutil.copy2(f, dst_froutes / f.name)
    # frontend shared shell: only take from shard 0 to avoid clobbering
    if take_shell_from_shard0:
        for shell in ("App.jsx", "main.jsx", "index.jsx", "App.tsx", "main.tsx"):
            sf = shard_ws / "frontend" / "src" / shell
            if sf.is_file():
                shutil.copy2(sf, master / "frontend" / "src" / shell)
    # Vitest (unit/integration) tests: namespaced per shard so they never
    # overwrite each other; vitest include is `tests/**` so it still collects them.
    src_tests = shard_ws / "backend" / "tests"
    if src_tests.is_dir():
        dst_tests = master / "backend" / "tests" / f"_shard_{shard_id}"
        for f in src_tests.rglob("*"):
            if f.is_file():
                rel = f.relative_to(src_tests)
                (dst_tests / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst_tests / rel)

    # E2E tests (PRIMARY graded layer for web-react-express): Playwright
    # testDir is `./test-e2e` and testMatch collects *.{js,jsx,ts,tsx}, so every
    # file here is a test. THIS STEP WAS MISSING and caused "0 tests" in v4
    # parallel runs -- the shards generated E2E tests but the merge dropped them,
    # leaving the final workspace with an empty test-e2e dir.
    src_e2e = shard_ws / "backend" / "test-e2e"
    if src_e2e.is_dir():
        dst_e2e = master / "backend" / "test-e2e"
        dst_e2e.mkdir(parents=True, exist_ok=True)
        for f in src_e2e.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(src_e2e)
            target = dst_e2e / rel
            if target.exists():
                target = dst_e2e / f"_shard{shard_id}_{rel.name}"
                print(f"WARN: E2E test name collision {rel}, renamed to {target.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)

    # Frontend vitest tests: also additive (frontend include is `tests/**`).
    src_ftests = shard_ws / "frontend" / "tests"
    if src_ftests.is_dir():
        dst_ftests = master / "frontend" / "tests"
        dst_ftests.mkdir(parents=True, exist_ok=True)
        for f in src_ftests.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(src_ftests)
            target = dst_ftests / rel
            if target.exists():
                target = dst_ftests / f"_shard{shard_id}_{rel.name}"
                print(f"WARN: frontend test name collision {rel}, renamed to {target.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)


def union_deps(master: Path, deps: dict) -> None:
    if not deps:
        return
    for pkg_file in (master / "backend" / "package.json", master / "frontend" / "package.json"):
        if not pkg_file.is_file():
            continue
        data = json.loads(pkg_file.read_text(encoding="utf-8"))
        data.setdefault("dependencies", {})
        changed = False
        for name, ver in deps.items():
            if name not in data["dependencies"]:
                data["dependencies"][name] = ver
                changed = True
        if changed:
            pkg_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Merge-doctor: repair the merged workspace so it BUILDS and BOOTS.
#
# A broken merge (v12) left the frontend/backend unable to start, so Playwright
# ran 0 tests -> 0 pass. We iteratively detect missing modules (and missing named
# exports) reported by the backend require check and the frontend build, then
# generate minimal stubs. This guarantees the graded suite at least EXECUTES --
# stubbed features may still fail their own tests, but the rest of the app's
# tests run instead of the entire suite collapsing to 0.
# ---------------------------------------------------------------------------
_FRONTEND_RESOLVE = re.compile(r"Could not resolve ['\"](.+?)['\"] from ['\"](.+?)['\"]")
_FRONTEND_NOT_EXPORTED = re.compile(r"['\"](.+?)['\"] is not exported by ['\"](.+?)['\"]")
_BACKEND_MISSING = re.compile(r"Cannot find module ['\"](.+?)['\"]", re.I)
_BACKEND_REQUIRE_STACK = re.compile(r"^- (.+\.js)$", re.M)


def _resolve_stub_path(workspace_root: Path, from_file: str, spec: str) -> Path | None:
    """Resolve the absolute stub path for a missing `spec` imported by `from_file`."""
    if not spec or spec.startswith("@") or ("/" not in spec and not spec.startswith(".")):
        # bare package name (e.g. 'lodash') -> not a stubbable local module
        return None
    base = Path(from_file)
    if not base.is_absolute():
        base = workspace_root / from_file
    if not base.is_file():
        base = workspace_root
    ref = (base.parent if base.is_file() else base) / spec
    try:
        ref = ref.resolve()
    except Exception:
        pass
    try:
        ref.relative_to(workspace_root.resolve())
    except Exception:
        return None
    return ref


def _esm_stub_body(names: list[str] | None = None) -> str:
    """Body of an ESM module stub usable both as a default import and named import.

    The graded platform runs the backend under Node 20.19, which auto-detects a
    `.js` file containing `export`/`import` as ESM. Therefore backend stubs MUST
    be ESM (a CommonJS `module.exports = new Proxy(...)` is invisible to a
    `import { name } from './x'` and also breaks once an `export` line is added).
    """
    parts = [
        "// AUTO-GENERATED by merge-doctor: placeholder for a missing module.",
        "const _mergeStub = new Proxy(function(){}, {",
        "  get: function(_t, prop){ if (prop === 'default' || prop === 'then') return undefined; "
        "return function(){ return Promise.resolve(null); }; },",
        "  apply: function(){ return Promise.resolve(null); }",
        "});",
        "export default _mergeStub;",
    ]
    for name in (names or []):
        parts.append(f"export const {name} = (..._a) => Promise.resolve(null);")
    return "\n".join(parts) + "\n"


def _stub_content(path: Path, is_frontend: bool) -> str:
    if is_frontend:
        return (
            "// AUTO-GENERATED by merge-doctor: placeholder for a missing module.\n"
            "const _stub: any = (props: any) => null;\n"
            "export default _stub;\n"
            "export const _mergeDoctorStub = _stub;\n"
        )
    # backend: ESM stub (see _esm_stub_body rationale).
    return _esm_stub_body()


def _add_frontend_named_export(frontend_root: Path, file: str, name: str) -> bool:
    rel = file
    if rel.startswith("./"):
        rel = rel[2:]
    cand = frontend_root / rel
    if not cand.is_file():
        cand = frontend_root / "src" / rel
    if not cand.is_file():
        # file genuinely missing (edge case) -> create it so the export exists
        cand = frontend_root / rel
    cand.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(cand, "a", encoding="utf-8") as fh:
            fh.write(f"\nexport const {name} = (props: any) => null;\n")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# v14 fixes for the merge-doctor BACKEND repair path.
#
# Root cause of the v13 0-score (infrastructure, not agent logic): the graded
# platform runs the backend as ESM (`node src/index.js` under package.json
# "type":"module"). Generated route files used CommonJS `require()` and
# merge-doctor stubs used `module.exports`, so the merged app crashed on boot
# ("require is not defined" / "'X' is not exported by 'Y'") and the evaluator
# never started -> 0 score. We now repair the backend as ESM, not CommonJS.
# ---------------------------------------------------------------------------
_CJS_REQUIRE = re.compile(r"const\s+([A-Za-z0-9_$]+)\s*=\s*require\(\s*['\"]([^'\"]+)['\"]\s*\)")
_CJS_REQUIRE_DESTRUCT = re.compile(r"const\s*\{([^}]+)\}\s*=\s*require\(\s*['\"]([^'\"]+)['\"]\s*\)")
_IMPORT_NAMED = re.compile(r"import\s*\{([^}]+)\}\s*from\s*['\"]([^'\"]+)['\"]")
_EXPORT_NAMED = re.compile(
    r"export\s+(?:const|function|class|let|var|async\s+function)\s+([A-Za-z0-9_$]+)"
)
# v15 fix: collect every top-level local binding so we NEVER re-declare a name
# that already exists when converting `module.exports.NAME = X`.
_DECL_RE = re.compile(r"(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)")
_EXPORT_DECL_RE = re.compile(
    r"export\s+(?:default\s+)?(?:const|let|var|function|class|async\s+function)\s+([A-Za-z_$][\w$]*)"
)


def _declared_names(text: str) -> set[str]:
    names = set(_DECL_RE.findall(text))
    names.update(_EXPORT_DECL_RE.findall(text))
    return names


def _module_export_assignment(m, declared) -> str:
    """Convert `module.exports.NAME = X` / `exports.NAME = X` to ESM.

    v14 bug: it emitted `export const NAME = NAME;` which re-declared an existing
    local binding -> `SyntaxError: Identifier 'NAME' has already been declared`.
    Fix: if X is an identifier, re-export the existing binding (`export { X as NAME }`,
    which collapses to `export { NAME }` when X === NAME). Only fall back to
    `export const NAME = X` for a complex RHS where NAME is not already declared.
    """
    name = m.group(1)
    expr = m.group(2).strip()
    if re.fullmatch(r"[A-Za-z_$][\w$]*", expr):
        return f"export {{ {expr} as {name} }};\n"
    if name in declared:
        return f"export {{ {name} }};\n"
    return f"export const {name} = {expr};\n"


def _is_leaf_file(p: Path, backend: Path) -> bool:
    """True for agent-generated leaf files (routes / services) that the merge
    doctor is allowed to normalise. Framework files (app.js, index.js,
    database/*, config) MUST stay CommonJS (they use `require`, `__dirname`,
    `module.exports`) -- converting them to ESM breaks `__dirname` at boot."""
    try:
        rel = p.resolve().relative_to(backend.resolve())
    except Exception:
        return False
    parts = rel.parts
    if len(parts) >= 3 and parts[0] == "src" and parts[1] in ("routes", "services"):
        return True
    return False


def _is_route_file(p: Path, backend: Path) -> bool:
    try:
        rel = p.resolve().relative_to(backend.resolve())
    except Exception:
        return False
    parts = rel.parts
    return len(parts) >= 3 and parts[0] == "src" and parts[1] == "routes"


def _rewrite_cjs_to_esm(backend: Path) -> bool:
    """Normalise generated backend ROUTE files (src/routes/*) from CommonJS to
    runnable ESM under Node 20.19's auto-detection.

    A `.js` file that contains `export` is auto-detected as ESM, where `require`
    / `module` / `__dirname` are undefined. Agent route files are hybrids:
      - `const express = require('express')`  -> must become `import express ...`
      - `module.exports = router`             -> must become `export default router`
      - `if (require.main === module) {...}`   -> `require`/`module` undefined
    ESM also forbids `import` after a non-import statement, so all converted
    `require`s are HOISTED to the file top.

    ONLY route files are touched. Service files keep CommonJS -- Node's CJS/ESM
    interop serves their named exports to ESM importers, and the doctor stubs are
    repaired separately by `_ensure_named_exports`. Framework files are never
    touched (their `__dirname` must survive).

    Returns True if any file was changed.
    """
    changed = False
    for p in backend.rglob("*.js"):
        if "node_modules" in str(p) or not p.is_file():
            continue
        if not _is_route_file(p, backend):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if "require(" not in text and "module.exports" not in text:
            continue

        declared = _declared_names(text)
        imports: list[str] = []
        seen: set[str] = set()

        def _add(stmt: str) -> str:
            if stmt not in seen:
                seen.add(stmt)
                imports.append(stmt)
            return ""  # remove the original require statement from its position

        def _d(m):
            names = ", ".join(n.strip() for n in m.group(1).split(","))
            return _add(f"import {{ {names} }} from '{m.group(2)}'")

        def _s(m):
            return _add(f"import {m.group(1)} from '{m.group(2)}'")

        def _bare(m):
            # v15 fix: a bare `require('x')` used in EXPRESSION position
            # (e.g. `await require('x').method()`) must become a NAMED import
            # (`import _md_req_N from 'x'`) substituted inline as `_md_req_N`,
            # NOT a side-effect `import 'x'` that deletes the callee and leaves
            # `await .method` (SyntaxError).
            spec = m.group(1)
            if spec not in mod_vars:
                mod_vars[spec] = f"_md_req_{len(mod_vars)}"
            var = mod_vars[spec]
            stmt = f"import {var} from '{spec}'"
            if stmt not in seen:
                seen.add(stmt)
                imports.append(stmt)
            return var

        mod_vars: dict[str, str] = {}
        new = _CJS_REQUIRE_DESTRUCT.sub(_d, text)
        new = _CJS_REQUIRE.sub(_s, new)
        new = re.sub(r"require\(\s*['\"]([^'\"]+)['\"]\s*\)\s*;?", _bare, new)

        # module.exports.default = X -> export default X (or drop if one exists already)
        new = re.sub(
            r"module\.exports\.default\s*=\s*([^\n;]+);?",
            lambda m: "" if "export default" in new else f"export default {m.group(1).strip()};\n",
            new,
        )
        # module.exports.NAME = X / exports.NAME = X -> re-export existing binding (v15)
        new = re.sub(
            r"module\.exports\.(?!default)([A-Za-z_$][\w$]*)\s*=\s*([^\n;]+);?",
            lambda m: _module_export_assignment(m, declared),
            new,
        )
        new = re.sub(
            r"(?<!module\.)exports\.([A-Za-z_$][\w$]*)\s*=\s*([^\n;]+);?",
            lambda m: _module_export_assignment(m, declared),
            new,
        )
        # module.exports = X  ->  export default X  (or drop if export default exists)
        if "module.exports" in new:
            if "export default" in new:
                new = re.sub(r"module\.exports\s*=\s*[^\n;]+;?\n?", "", new)
            else:
                new = re.sub(
                    r"module\.exports\s*=\s*([^\n;]+);?\n?",
                    lambda m: f"export default {m.group(1).strip()};\n",
                    new,
                )
        # neutralise the `if (require.main === module)` self-registration guard
        new = re.sub(r"require\.main\s*==?={1,3}\s*module", "false", new)
        new = re.sub(r"module\s*==?={1,3}\s*require\.main", "false", new)

        if imports:
            new = "\n".join(imports) + "\n" + new.lstrip("\n")
        if new != text:
            p.write_text(new, encoding="utf-8")
            changed = True
    return changed


def _ensure_named_exports(backend: Path) -> None:
    """Ensure every local named import resolves to a real (or stubbed) ESM export.

    v13 crashed at evaluator boot with:
      - github: route used CommonJS `require` but Node 20.19 auto-detects the file
        as ESM (it also has `export default`), so `require is not defined`.
      - sheet: route imports `{ transferRange }` from a service, but v13's stub was
        CommonJS (`module.exports = new Proxy`) -> "'transferRange' is not exported".
    Fix: scan every backend .js for local named/default imports, then (re)write the
    imported file as a clean ESM stub that exports *all* requested names plus a
    default. A CJS stub is fully replaced (appending `export` to it would create a
    broken hybrid that fails with `module is not defined` under auto-detect).
    """
    # target -> set of required names
    needed: dict[Path, set[str]] = {}
    for p in backend.rglob("*.js"):
        if "node_modules" in str(p) or not p.is_file():
            continue
        if not _is_leaf_file(p, backend):
            continue  # only leaf files (routes/services) import local modules via ESM
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        # named imports
        for m in _IMPORT_NAMED.finditer(text):
            spec = m.group(2)
            if not spec.startswith("."):
                continue
            tgt = (p.parent / spec).resolve()
            names = {n.strip().split(" as ")[0].strip() for n in m.group(1).split(",") if n.strip()}
            needed.setdefault(tgt, set()).update(names)
        # default imports: import X from './y'  -> require a default export to exist
        for m in re.finditer(r"import\s+([A-Za-z0-9_$]+)\s+from\s*['\"]([^'\"]+)['\"]", text):
            spec = m.group(2)
            if not spec.startswith("."):
                continue
            tgt = (p.parent / spec).resolve()
            needed.setdefault(tgt, set()).add("__default__")

    for tgt, names in needed.items():
        names.discard("")
        has_default = "__default__" in names
        names.discard("__default__")
        if tgt.is_file():
            try:
                t = tgt.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                t = ""
            # Already a valid ESM file with the needed exports? Skip untouched to
            # preserve any real implementation.
            if "export default" in t or "export {" in t or _EXPORT_NAMED.search(t):
                existing = set(_EXPORT_NAMED.findall(t))
                if "export default" in t:
                    existing.add("__default__")
                missing = {n for n in names if n not in existing}
                if not missing and (has_default or "__default__" in existing):
                    continue
                # append only the genuinely missing named exports (file is ESM already)
                if "import " in t or "export " in t:
                    declared_t = _declared_names(t)
                    try:
                        with open(tgt, "a", encoding="utf-8") as fh:
                            for n in missing:
                                if n in declared_t:
                                    # already a real local binding -> re-export it (v15: no re-declare)
                                    fh.write(f"\nexport {{ {n} }};")
                                else:
                                    fh.write(f"\nexport const {n} = (..._a) => Promise.resolve(null);\n")
                        if has_default and "__default__" not in existing:
                            with open(tgt, "a", encoding="utf-8") as fh:
                                fh.write("\nexport default (..._a) => Promise.resolve(null);\n")
                        continue
                    except Exception:
                        pass
            # Not ESM. A real implementation that happens to be CommonJS
            # (`module.exports = { fn }`) is served fine to ESM importers via Node's
            # CJS/ESM interop -- do NOT overwrite it (that would drop real logic).
            # Only the broken AUTO-GENERATED doctor stubs (CJS `new Proxy`) are
            # replaced with a proper ESM stub exporting the requested names.
            if "AUTO-GENERATED by merge-doctor" not in t:
                continue
        # Write (or overwrite) a clean ESM stub exporting the required names + default.
        # `_esm_stub_body` always provides `export default _mergeStub`, so never
        # append a second default (that is a SyntaxError: "already been declared").
        try:
            tgt.parent.mkdir(parents=True, exist_ok=True)
            tgt.write_text(_esm_stub_body(sorted(names)), encoding="utf-8")
        except Exception:
            pass


def merge_doctor(work_root: Path, max_iter: int = 25) -> None:
    if os.environ.get("ARC_MERGE_DOCTOR", "1").strip() in ("0", "false", "no", "off"):
        print("[merge-doctor] disabled (ARC_MERGE_DOCTOR=0)", flush=True)
        return
    print("[merge-doctor] repairing merged workspace for build/boot...", flush=True)
    backend = work_root / "backend"
    frontend = work_root / "frontend"
    seen: set = set()

    # 1) Backend: app.js must IMPORT cleanly as ESM. The graded platform runs
    #    `node src/index.js` under package.json "type":"module", so CommonJS
    #    require()/module.exports are NOT available. We mirror that by importing
    #    as ESM (NOT `require`), then iteratively repair.
    if (backend / "src" / "app.js").is_file():
        # v14: normalise any stray CommonJS require() in generated files to ESM
        # import (github v13 crashed with "require is not defined").
        _rewrite_cjs_to_esm(backend)
        # v14: ensure every named import resolves to a real (or stubbed) export,
        # so "'X' is not exported by 'Y'" (sheet v13) never reaches the grader.
        _ensure_named_exports(backend)
        for _ in range(max_iter):
            r = subprocess.run(
                ["node", "--input-type=module", "-e",
                 "import('./src/app.js').then(()=>process.exit(0)).catch(e=>{console.error(String(e&&e.stack||e));process.exit(1)})"],
                cwd=str(backend), capture_output=True, text=True, shell=False,
            )
            txt = (r.stderr or "") + (r.stdout or "")
            if r.returncode == 0:
                break
            # Missing named export: "'X' is not exported by 'Y'". Re-scan and fill
            # any gaps, then retry.
            if "is not exported by" in txt or "does not provide an export named" in txt:
                _ensure_named_exports(backend)
                continue
            m = _BACKEND_MISSING.search(txt)
            if not m:
                break
            spec = m.group(1)
            stack = _BACKEND_REQUIRE_STACK.findall(txt)
            from_file = stack[0] if stack else str(backend / "src" / "app.js")
            key = ("be", spec, from_file)
            if key in seen:
                break
            seen.add(key)
            target = _resolve_stub_path(backend, from_file, spec)
            if not target:
                break
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_stub_content(target, is_frontend=False), encoding="utf-8")
            print(f"[merge-doctor] stubbed backend module: {target.relative_to(work_root)}", flush=True)

    # 2) Frontend: build:fast must succeed (Vite/TS).
    if frontend.is_dir():
        for _ in range(max_iter):
            r = subprocess.run("npm run build:fast", cwd=str(frontend), shell=True,
                               capture_output=True, text=True)
            txt = (r.stderr or "") + (r.stdout or "")
            m = _FRONTEND_RESOLVE.search(txt)
            if m:
                spec, from_file = m.group(1), m.group(2)
                key = ("fe-file", spec, from_file)
                if key in seen:
                    break
                seen.add(key)
                target = _resolve_stub_path(frontend, from_file, spec)
                if not target:
                    break
                if target.suffix not in (".tsx", ".jsx", ".ts", ".js"):
                    target = target.with_suffix(".tsx")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(_stub_content(target, is_frontend=True), encoding="utf-8")
                print(f"[merge-doctor] stubbed frontend module: {target.relative_to(work_root)}", flush=True)
                continue
            m2 = _FRONTEND_NOT_EXPORTED.search(txt)
            if m2:
                name, file = m2.group(1), m2.group(2)
                key = ("fe-name", name, file)
                if key in seen:
                    break
                seen.add(key)
                if _add_frontend_named_export(frontend, file, name):
                    print(f"[merge-doctor] added missing export '{name}' to {file}", flush=True)
                    continue
                break
            break
    print("[merge-doctor] repair complete", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("requirement_path")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--shards", type=int, default=4)
    ap.add_argument("--base-port", type=int, default=3100)
    ap.add_argument("--type", default="web")
    ap.add_argument("--smoke", type=int, default=0, help="Limit to first N leaves for validation")
    args = ap.parse_args()

    tree, req_dir = load_tree(Path(args.requirement_path))
    leaves: list = []
    collect_leaves(tree, leaves)
    if args.smoke and args.smoke < len(leaves):
        leaves = leaves[: args.smoke]
    if not leaves:
        print("No leaf nodes found.")
        return 2
    print(f"Leaves: {len(leaves)} | shards: {args.shards}")

    buckets = split_leaves(leaves, args.shards)
    # Normalize output path: on Windows a leading "/d/..." (from a git-bash cwd)
    # resolves to "D:\d\..."; rewrite to a clean "D:/..." drive path.
    raw_out = args.output
    if os.name == "nt" and len(raw_out) > 2 and raw_out[0] == "/" and raw_out[2] == "/":
        raw_out = raw_out[1].upper() + ":" + raw_out[2:]
    work_root = Path(raw_out).resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    parallel_dir = work_root / ".parallel"
    if parallel_dir.exists():
        shutil.rmtree(parallel_dir)
    parallel_dir.mkdir(parents=True, exist_ok=True)

    # base scaffold from template (carries the additive auto-loaders)
    for sub in ("backend", "frontend"):
        src = TEMPLATE_DIR / sub
        if src.is_dir():
            shutil.copytree(src, work_root / sub, dirs_exist_ok=True)

    shard_tasks = []
    shard_meta = []
    for i, bucket in enumerate(buckets):
        if not bucket:
            continue
        shard_dir = parallel_dir / f"shard_{i}"
        shard_dir.mkdir(parents=True, exist_ok=True)
        if req_dir and req_dir.is_dir():
            for item in req_dir.iterdir():
                # Never copy the requirement spec itself; each shard gets its own
                # pruned requirements.yaml written below.
                if item.name.lower().endswith((".yaml", ".yml")):
                    continue
                # Copy EVERYTHING else, including the `reference/` image folder and
                # any other asset directories. Requirement nodes embed visual
                # references like `reference/worksheet-X.png`; if that folder is
                # missing inside the shard, _resolve_image_path() cannot find the
                # file and logs "Image not found", which silently degrades the
                # DESIGN of UI-bearing leaves. Both GitHub and Sheet ship a
                # reference/ folder of screenshots, so this matters for both tasks.
                if item.is_dir():
                    shutil.copytree(item, shard_dir / item.name, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, shard_dir / item.name)
        pruned = prune(tree, bucket)
        (shard_dir / "requirements.yaml").write_text(
            yaml.safe_dump(pruned, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        ws = parallel_dir / f"ws_{i}"
        shard_tasks.append(run_shard(shard_dir, ws, args.base_port + i, args.type))
        shard_meta.append((i, ws))

    async def _gather_all() -> list:
        return await asyncio.gather(*shard_tasks, return_exceptions=True)

    results = asyncio.run(_gather_all())

    deps: dict = {}
    merged_leaves = 0
    failed = []
    first_ok = None
    for (i, ws), res in zip(shard_meta, results):
        ok = not (isinstance(res, Exception) or (isinstance(res, int) and res != 0))
        if ok:
            if first_ok is None:
                first_ok = i
            merged_leaves += len(buckets[i])
        else:
            failed.append((i, ws))
    # SALVAGE: merge additive artifacts even from FAILED shards. A shard that
    # crashed mid-run (e.g. model API connection reset) may still hold hours of
    # completed route/migration/frontend work. Throwing it away because the
    # process exit code was non-zero cost us an entire 200 CNY run (0/47
    # merged). The shell files are only taken from a successful shard.
    for (i, ws), res in zip(shard_meta, results):
        copy_additive(ws, work_root, str(i), deps, take_shell_from_shard0=(i == first_ok))

    union_deps(work_root, deps)

    # reuse node_modules from a successful shard to skip full reinstall
    for i, ws in shard_meta:
        for sub in ("backend", "frontend"):
            nm = ws / sub / "node_modules"
            if nm.is_dir():
                dst = work_root / sub / "node_modules"
                if not dst.is_dir():
                    shutil.copytree(nm, dst)
                break
        break

    for sub in ("backend", "frontend"):
        if (work_root / sub).is_dir():
            # shell=True so Windows resolves the `npm.cmd` wrapper correctly.
            # --no-cache-dir avoids concurrent npm cache corruption under sharding.
            subprocess.run("npm install --no-cache-dir", cwd=str(work_root / sub), shell=True, check=False)
    fe_build = work_root / "frontend"
    if fe_build.is_dir():
        # Use the faster unminified build for the merge-phase bundle; minification
        # is irrelevant to E2E scoring and only costs time.
        subprocess.run("npm run build:fast", cwd=str(fe_build), shell=True, check=False)

    # Merge-doctor: repair missing modules so the artifact BUILDS and BOOTS.
    # A broken merge (v12) left the frontend/backend unable to start, so Playwright
    # ran 0 tests -> 0 pass. This guarantees the graded suite at least executes.
    try:
        merge_doctor(work_root)
    except Exception as _md_err:
        print(f"[merge-doctor] error (non-fatal): {_md_err}", flush=True)

    routes_dir = work_root / "backend" / "src" / "routes"
    if routes_dir.is_dir():
        for f in routes_dir.glob("*.js"):
            r = subprocess.run(["node", "--check", str(f)], capture_output=True)
            if r.returncode != 0:
                print(f"WARN: route file syntax error: {f.name}")

    print("\n=== Merge complete ===")
    print(f"Merged leaves: {merged_leaves}/{len(leaves)}")
    if failed:
        print(f"FAILED shards ({len(failed)}): rerun those leaves serially via standard `main.py` on the full tree (--resume).")
        for i, ws in failed:
            print(f"  shard {i} workspace: {ws}")
    else:
        print("All shards succeeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
