import sys, types, subprocess, os
from pathlib import Path

# stub yaml so parallel_compile imports without PyYAML
sys.modules.setdefault("yaml", types.ModuleType("yaml"))

# allow importing the project module
sys.path.insert(0, "D:/比赛内容/agent-arc-based/scripts")
import parallel_compile as pc

work = Path("D:/比赛内容/_mdtest/github_backend")
print("=== running merge_doctor on REAL extracted github backend ===")
pc.merge_doctor(work)

print("\n=== boot test (what the platform actually does, ESM auto-detect) ===")
r = subprocess.run(
    ["node", "--input-type=module", "-e",
     "import('./src/app.js').then(()=>{console.log('BOOT_OK');process.exit(0)}).catch(e=>{console.error('BOOT_FAIL', String(e&&e.stack||e));process.exit(1)})"],
    cwd=str(work), capture_output=True, text=True,
)
print("returncode:", r.returncode)
print("STDOUT:", r.stdout)
print("STDERR:", r.stderr[:2000])
