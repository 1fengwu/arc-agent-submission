import sys, types, tempfile, subprocess, importlib.util
from pathlib import Path

# stub yaml so we can import the module standalone
_yaml = types.ModuleType("yaml"); _yaml.safe_load = lambda *a, **k: {}; _yaml.safe_dump = lambda *a, **k: ""
sys.modules["yaml"] = _yaml

spec = importlib.util.spec_from_file_location("pc", r"D:/比赛内容/_pc.py")
pc = importlib.util.module_from_spec(spec); spec.loader.exec_module(pc)
NODE = r"C:/Users/86153/.workbuddy/binaries/node/versions/22.22.2/node.exe"

cases = {
    # 1) hybrid route: ESM import at top + CJS module.exports at bottom (the REQ-2-2-1 pattern)
    "routes/REQ-2-2-1.js": '''
import express from 'express'
const router = express.Router();
const orgListRouter = express.Router();
module.exports = router;
module.exports.orgListRouter = orgListRouter;
if (require.main === module) { run(); }
''',
    # 2) require used in EXPRESSION position (the REQ-4-1 pattern) + destructured require
    "routes/REQ-4-1.js": '''
import express from 'express'
const { listTree } = require('../services/REQ-4-1');
const result = await require('../services/REQ-4-1').readFile({ a: 1 });
module.exports = express.Router();
''',
    # 3) bare require + module.exports.NAME re-export with different identifier
    "routes/REQ-3-2-1.js": '''
const transfer = require('../services/REQ-3-2-1');
module.exports.transferRange = transfer;
''',
    # 4) service: pure CJS module.exports object — must be LEFT untouched (CJS interop)
    "services/REQ-3-2-1.js": '''
function transferRange(a,b){ return [a,b]; }
module.exports = { transferRange };
''',
    # 5) app.js framework file — must NOT be touched (stays CJS)
    "app.js": '''
const express = require('express');
const app = express();
module.exports = app;
''',
}

tmp = Path(tempfile.mkdtemp()) / "backend" / "src"
shutil = __import__("shutil")
(tmp / "routes").mkdir(parents=True, exist_ok=True)
(tmp / "services").mkdir(parents=True, exist_ok=True)
for rel, code in cases.items():
    (tmp / rel).write_text(code, encoding="utf-8")

backend = tmp.parent
pc._rewrite_cjs_to_esm(backend)
pc._ensure_named_exports(backend)

print("=== TRANSFORMED OUTPUT ===")
ok = True
for rel in cases:
    p = tmp / rel
    out = p.read_text(encoding="utf-8")
    print(f"\n----- {rel} -----")
    print(out.strip())
    # node --check (ESM auto-detect) for valid syntax
    r = subprocess.run([NODE, "--check", str(p)], capture_output=True, text=True)
    valid = r.returncode == 0
    ok = ok and valid
    print(f"[syntax: {'OK' if valid else 'FAIL'}] {r.stderr.strip()[:160]}")

print("\n=== SUMMARY ===", "ALL VALID ESM" if ok else "HAS SYNTAX ERRORS")
sys.exit(0 if ok else 1)
