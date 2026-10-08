"""Debug: replicate merge_doctor's boot loop decision branch by branch."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, r"D:/比赛内容/_sub2/scripts")
if "yaml" not in sys.modules:
    import types
    _y = types.ModuleType("yaml")
    _y.safe_load = lambda *a, **k: {}
    _y.load = lambda *a, **k: {}
    sys.modules["yaml"] = _y
import parallel_compile as pc  # noqa: E402

backend = Path(sys.argv[1]) / "backend"

for i in range(3):
    r = subprocess.run(
        ["node", "--input-type=module", "-e",
         "import('./src/app.js').then(()=>process.exit(0)).catch(e=>{console.error(String(e&&e.stack||e));process.exit(1)})"],
        cwd=str(backend), capture_output=True, text=True, shell=False,
    )
    txt = (r.stderr or "") + (r.stdout or "")
    print(f"--- iter {i}: rc={r.returncode}")
    if r.returncode == 0:
        print("boot ok"); break
    print("first 300 of txt:", txt[:300].replace(chr(10), " | "))
    if "is not exported by" in txt or "does not provide an export named" in txt:
        print("BRANCH: not-exported -> _ensure_named_exports"); 
        pc._ensure_named_exports(backend)
        continue
    m = pc._BACKEND_MISSING.search(txt)
    print("BRANCH: missing-module match:", bool(m), "->", m.group(1)[:120] if m else None)
    if not m:
        print("BRANCH: break (no handler)"); break
    spec = m.group(1)
    stack = pc._BACKEND_REQUIRE_STACK.findall(txt)
    from_file = stack[0] if stack else str(backend / "src" / "app.js")
    print("from_file:", from_file[:100])
    target = pc._resolve_stub_path(backend, from_file, spec)
    print("resolved target:", target)
    if target:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(pc._stub_content(target, is_frontend=False), encoding="utf-8")
        print("STUBBED:", target)
