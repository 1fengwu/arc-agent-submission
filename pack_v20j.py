import os, zipfile

SRC = r"D:\比赛内容\_sub2"
OUT = r"D:\比赛内容\agent-submission-v20j.zip"
EXCLUDE_DIRS = {".git", "__pycache__", "node_modules"}
EXCLUDE_EXT = {".pyc"}

count = 0
with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
    for root, dirs, files in os.walk(SRC):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for f in files:
            if os.path.splitext(f)[1].lower() in EXCLUDE_EXT:
                continue
            fp = os.path.join(root, f)
            rel = os.path.relpath(fp, SRC)
            z.write(fp, rel)
            count += 1
print("FILES:", count)

with zipfile.ZipFile(OUT) as z:
    names = z.namelist()
    env_names = [n for n in names if n.endswith(".env")]
    print("ENV_ENTRIES:", env_names)
    assert env_names, "no .env found in package"
    data = z.read(env_names[0]).decode("utf-8", "replace")
    for key in ("MODEL=deepseek-v4-flash", "ARC_SERIAL=0", "ARC_BATCH_GEN=1", "ARC_TOKEN_BUDGET=0", "ARC_TDD_BUDGET=1"):
        print(key, "->", key in data)
    pyc = [n for n in names if n.endswith(".pyc")]
    print("PYC_COUNT:", len(pyc))
    print("WORKFLOW_PRESENT:", any(n.endswith("core/workflow.py") for n in names))
    print("DISCIPLINE_PRESENT:", any(n.endswith("agents/runtime/stage_discipline.py") for n in names))
    # verify the new module-completeness skill rules are inside the package
    tdd = next((n for n in names if n.endswith("skills/tdd-test-failure-repair/SKILL.md")), None)
    lfd = next((n for n in names if n.endswith("skills/leaf-full-design/SKILL.md")), None)
    assert tdd and lfd, "skill files missing"
    tdd_txt = z.read(tdd).decode("utf-8", "replace")
    lfd_txt = z.read(lfd).decode("utf-8", "replace")
    print("TDD_COMPLETENESS_GATE:", "Module-completeness gate" in tdd_txt)
    print("LFD_DANGLING_IMPORT:", "No dangling imports" in lfd_txt)
