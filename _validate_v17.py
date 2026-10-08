"""v17 validation on the REAL failed v16 github workspace (not stubs).

1. Run the FIXED merge_doctor on a copy of the real merged backend.
   Expect: 18 extensionless stubs purged/moved to .js; boot gate result printed.
2. Independently boot `src/app.js` with real node + real node_modules.
"""
import shutil
import subprocess
import sys
from pathlib import Path

NODE = r"C:/Users/86153/.workbuddy/binaries/node/versions/22.22.2/node.exe"
WORK = Path(sys.argv[1] if len(sys.argv) > 1 else r"D:/比赛内容/_test_v17")

sys.path.insert(0, r"D:/比赛内容/_sub2/scripts")
# merge_doctor itself never parses YAML; stub the module so the import works.
if "yaml" not in sys.modules:
    _yaml_stub = type(__import__("types"))("yaml")
    _yaml_stub.safe_load = lambda *a, **k: {}
    _yaml_stub.load = lambda *a, **k: {}
    sys.modules["yaml"] = _yaml_stub
import parallel_compile as pc  # noqa: E402

backend = WORK / "backend"

# sanity: the broken state must be present before repair
extless = [p for p in backend.rglob("*") if p.is_file() and p.suffix == ""]
print(f"[pre] extensionless files present: {len(extless)}")
for p in extless[:5]:
    print("   -", p.relative_to(backend))

# 1) run the fixed merge_doctor
pc.merge_doctor(WORK)

# post state: every extensionless AUTO-GENERATED stub must either forward to its
# real .js sibling (neutralized) or have no sibling (orphan, harmless).
bad_shadow = []
for p in backend.rglob("*"):
    if not (p.is_file() and p.suffix == ""):
        continue
    head = p.read_text(encoding="utf-8", errors="ignore")[:400]
    if "AUTO-GENERATED" not in head:
        continue
    sib = p.with_name(p.name + ".js")
    if sib.is_file() and "module.exports = require" not in head:
        bad_shadow.append(str(p.relative_to(backend)))
print(f"[post] unfixed shadows: {len(bad_shadow)}")
if bad_shadow:
    print("!! STILL SHADOWING:", bad_shadow[:5])
    sys.exit(2)

# 2) independent real boot (same command the evaluator runs, ESM import path)
r = subprocess.run(
    [NODE, "--input-type=module", "-e",
     "import('./src/app.js').then(m=>{console.log('BOOT_OK app=',typeof m.default);process.exit(0)}).catch(e=>{console.error('BOOT_FAIL',String(e&&e.stack||e));process.exit(1)})"],
    cwd=str(backend), capture_output=True, text=True, shell=False,
)
print("[boot] rc=", r.returncode)
print((r.stdout or "")[-500:])
print((r.stderr or "")[-1200:])
sys.exit(0 if r.returncode == 0 else 1)
