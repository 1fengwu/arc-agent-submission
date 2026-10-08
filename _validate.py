import sys, types, subprocess, importlib.util

# _pc.py only imports yaml at top-level; our functions don't use it at runtime.
# Register a dummy so we don't pollute the environment with a pip install.
_yaml = types.ModuleType("yaml")
_yaml.safe_load = lambda *a, **k: {}
_yaml.safe_dump = lambda *a, **k: ""
sys.modules["yaml"] = _yaml

spec = importlib.util.spec_from_file_location("pc", r"D:/比赛内容/_pc.py")
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)

from pathlib import Path

backend = Path(r"D:/比赛内容/_test_backend")

print("[1] running _rewrite_cjs_to_esm ...", flush=True)
pc._rewrite_cjs_to_esm(backend)
print("[2] running _ensure_named_exports ...", flush=True)
pc._ensure_named_exports(backend)

f = backend / "src/routes/REQ-2-2-1.js"
txt = f.read_text(encoding="utf-8", errors="ignore")
print("[3] REQ-2-2-1.js tail:")
print("\n".join(txt.splitlines()[-12:]))
print()
print("[4] checks:")
print("   has broken 'export const orgListRouter = orgListRouter;' :",
      "export const orgListRouter = orgListRouter;" in txt)
print("   has re-export 'export { orgListRouter' :", "export { orgListRouter" in txt)

print("[5] node boot check (import('./src/app.js')) ...", flush=True)
NODE = r"C:/Users/86153/.workbuddy/binaries/node/versions/22.22.2/node.exe"
r = subprocess.run(
    [NODE, "--input-type=module", "-e",
     "import('./src/app.js').then(()=>process.exit(0)).catch(e=>{console.error(String(e&&e.stack||e));process.exit(1)})"],
    cwd=str(backend), capture_output=True, text=True,
)
print("   returncode:", r.returncode)
out = (r.stderr or "") + (r.stdout or "")
print("   last 1500 chars of output:")
print(out[-1500:])
