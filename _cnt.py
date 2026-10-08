def load_yaml(p):
    try:
        import yaml
        return yaml.safe_load(open(p, encoding="utf-8").read())
    except Exception as e:
        return None

def collect_leaves(node, out):
    nid = str(node.get("id") or "").strip()
    ch = [c for c in (node.get("children") or []) if isinstance(c, dict)]
    if not ch and nid:
        out.append(nid)
    for c in ch:
        collect_leaves(c, out)

for name, p in [("GitHub", "D:/比赛内容/arcbench-hackathon-requirements/hackathon--github/requirements.yaml"),
                ("Sheet", "D:/比赛内容/arcbench-hackathon-requirements/hackathon--sheet/requirements.yaml")]:
    t = load_yaml(p)
    if t is None:
        print(name, "YAML parse unavailable (yaml not installed in this python)")
        continue
    leaves = []
    root = t if isinstance(t, dict) else (t[0] if t else {})
    collect_leaves(root, leaves)
    print(f"{name}: leaves={len(leaves)}  (shards=4 -> ~{len(leaves)//4} leaves/shard)")
