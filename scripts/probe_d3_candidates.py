"""D3 候选选择回归探针：100 条固定记忆 + 19900 条 bulk 噪声后仍能召回固定记忆。"""
import json
import os
import shutil
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sidecar"))

from graph import GraphStore  # noqa: E402

base = os.path.join(ROOT, ".tmp", f"d3probe-{os.getpid()}-{uuid.uuid4().hex[:8]}")
os.makedirs(base, exist_ok=True)
try:
    graph = GraphStore(base, "workspace")
    fixed_ids = []
    for i in range(100):
        fixed_ids.append(
            graph.upsert_entity(
                f"pnpm 依赖管理 固定记忆 {i}",
                "Concept",
                f"pnpm 依赖管理 配置 固定记忆条目 {i}",
            )
        )
    target_id = graph.upsert_entity(
        "pnpm 依赖管理配置",
        "Preference",
        "用户使用 pnpm 9.15 管理依赖，包管理器版本固定",
    )
    for i in range(19900):
        graph.upsert_entity(f"bulk data-{i}", "Data", f"bulk data-{i} synthetic node for scale testing")
    graph.save()
    result = graph.search_entities("pnpm 依赖管理 配置 版本", "workspace", top_k=5)
    ids = [eid for eid, _ in result]
    print("candidates top5:", [(eid, round(sim, 4)) for eid, sim in result])
    print("target_id:", target_id)
    ok = target_id in ids or any(sim > 0.4 for _, sim in result[:1])
    print("PASS" if ok else "FAIL", "target in top5 =", target_id in ids)
    print("nodes:", graph.count(include_archived=True))
    if not ok:
        sys.exit(1)
finally:
    shutil.rmtree(base, ignore_errors=True)
