import sys, types, re, shutil, subprocess, importlib.util
from pathlib import Path

_yaml = types.ModuleType("yaml"); _yaml.safe_load = lambda *a, **k: {}; _yaml.safe_dump = lambda *a, **k: ""
sys.modules["yaml"] = _yaml
spec = importlib.util.spec_from_file_location("pc", r"D:/比赛内容/_pc.py")
pc = importlib.util.module_from_spec(spec); spec.loader.exec_module(pc)
import importlib.util
NODE = r"C:/Users/86153/.workbuddy/binaries/node/versions/22.22.2/node.exe"

SRC = Path(r"D:/比赛内容/runlogs/v14_github/template/backend")
DST = Path(r"D:/比赛内容/_test_e2e")
if DST.exists(): shutil.rmtree(DST)
shutil.copytree(SRC, DST)
# reuse installed deps
if (Path(r"D:/比赛内容/_test_backend/node_modules")).exists():
    shutil.copytree(r"D:/比赛内容/_test_backend/node_modules", DST / "node_modules", dirs_exist_ok=True)

# ---- revert v14 corruption back to plausible ORIGINAL agent code ----
selfref = re.compile(r"export const (\w+) = \1;")
orphan_await = re.compile(r"await\s*\.(\w+)")
orphan_assign = re.compile(r"=\s*\.(\w+)\(")

for f in DST.rglob("*.js"):
    if "node_modules" in str(f): continue
    t = f.read_text(encoding="utf-8", errors="ignore")
    # infer the service spec referenced by this file (for reconstructing deleted requires)
    m = re.search(r"['\"](\.\./services/[^'\"]+)['\"]", t)
    spec_path = m.group(1) if m else None
    # revert v14 self-ref duplicate export bug -> module.exports.NAME = NAME
    t = selfref.sub(lambda mm: f"module.exports.{mm.group(1)} = {mm.group(1)};", t)
    if spec_path:
        t = orphan_await.sub(lambda mm: f"await require('{spec_path}').{mm.group(1)}", t)
        t = orphan_assign.sub(lambda mm: f"= require('{spec_path}').{mm.group(1)}(", t)
    f.write_text(t, encoding="utf-8")

print("[1] reverted v14 corruption; running FIXED transform ...")
pc._rewrite_cjs_to_esm(DST)
pc._ensure_named_exports(DST)

print("[2] per-file ESM syntax check ...")
bad = []
for f in sorted(DST.rglob("src/**/*.js")):
    if "node_modules" in str(f): continue
    r = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True)
    if r.returncode != 0:
        bad.append((str(f), r.stderr.strip()[:200]))
for b in bad:
    print("  BAD", b[0]); print("    ", b[1])
print(f"  {len(bad)} files with syntax errors")

print("[3] full backend boot (import('./src/app.js')) ...")
r = subprocess.run([NODE, "--input-type=module", "-e",
    "import('./src/app.js').then(()=>process.exit(0)).catch(e=>{console.error(String(e&&e.stack||e));process.exit(1)})"],
    cwd=str(DST), capture_output=True, text=True)
print("   returncode:", r.returncode)
out = (r.stderr or "") + (r.stdout or "")
print("   last 2000 chars:")
print(out[-2000:])
