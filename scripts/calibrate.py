"""水位线上界校准（M5）：生成合成节点 1k/5k/10k/20k/50k，每档跑 100 次召回测 P95。

上界本质是「图谱大到开始拖慢系统」的规模，所以从性能预算反推：
    high = 满足「召回 P95 < 150ms」的最大节点数 × 0.7

用法（仓库根目录，建议在启动 DSH 前运行）：
  py -3 scripts/calibrate.py                 # 校准并打印建议，不写任何文件
  py -3 scripts/calibrate.py --apply         # 额外写入两个 scope 的 maintenance.json（calibration 优先于 config）
  py -3 scripts/calibrate.py --sizes 1000,5000 --quick

实现说明：直接使用 embedded GraphStore（与 sidecar 内实现同一份代码），不启动 sidecar，
避免 LLM/面板等干扰；每档用独立临时目录，测完删除。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import shutil
import statistics
import sys
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sidecar"))

from graph import GraphStore  # noqa: E402

DEFAULT_SIZES = [1000, 5000, 10000, 20000, 50000]
P95_BUDGET_MS = 150.0
HIGH_FACTOR = 0.7
MIN_HIGH = 2000
QUERIES = [
    "上次我说过用什么包管理器",
    "这个项目的接口在哪里配置",
    "价格改成 80 了吗",
    "明天下午三点开会",
    "用户偏好使用 TypeScript",
    "数据库连接串怎么配",
]


def build_dataset(graph: GraphStore, size: int, seed: int) -> None:
    rng = random.Random(seed)
    topic_words = ["pnpm", "依赖", "配置", "接口", "数据库", "部署", "缓存", "日志", "测试", "版本"]
    for i in range(size):
        if i % 5 == 0:  # 20% 记忆风格节点
            word = topic_words[i % len(topic_words)]
            graph.upsert_entity(
                f"{word} 记忆 {i}",
                "Concept",
                f"用户提到 {word} 相关的项目知识，条目 {i}，版本 {rng.randint(1, 9)}",
            )
        else:  # 80% bulk 噪声
            graph.upsert_entity(
                f"bulk data-{i}",
                "Data",
                f"bulk synthetic node {i} calibration filler for scale testing only",
            )
    graph.save()


def measure_p95(graph: GraphStore, samples: int = 100) -> tuple[float, int]:
    latencies: list[float] = []
    hits = 0
    for i in range(samples):
        query = QUERIES[i % len(QUERIES)]
        started = time.perf_counter()
        result = graph.search_entities(query, "workspace", top_k=20)
        latencies.append((time.perf_counter() - started) * 1000.0)
        if result:
            hits += 1
    latencies.sort()
    index = max(0, math.ceil(len(latencies) * 0.95) - 1)
    return latencies[index], hits


def calibrate_sizes(sizes: list[int], samples: int, tmp_root: str) -> list[dict]:
    rows: list[dict] = []
    for size in sizes:
        data_home = os.path.join(tmp_root, f"size-{size}-{uuid.uuid4().hex[:6]}")
        os.makedirs(data_home, exist_ok=True)
        try:
            graph = GraphStore(data_home, "workspace")
            started = time.time()
            build_dataset(graph, size, seed=size)
            build_seconds = time.time() - started
            # 预热一次，排除索引惰性构建
            graph.search_entities("warmup pnpm 依赖", "workspace", top_k=20)
            p95, hits = measure_p95(graph, samples=samples)
            # 二次确认（热路径）
            p95_hot, _ = measure_p95(graph, samples=samples)
            rows.append(
                {
                    "size": size,
                    "p95_ms": round(p95_hot, 3),
                    "p95_cold_ms": round(p95, 3),
                    "hits": hits,
                    "samples": samples,
                    "build_seconds": round(build_seconds, 2),
                    "under_budget": p95_hot < P95_BUDGET_MS,
                }
            )
            print(
                f"[calibrate] size={size:>6} p95_hot={p95_hot:8.2f}ms p95_cold={p95:8.2f}ms "
                f"hits={hits}/{samples} build={build_seconds:.1f}s"
            )
        finally:
            shutil.rmtree(data_home, ignore_errors=True)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", default=",".join(str(size) for size in DEFAULT_SIZES))
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--quick", action="store_true", help="只跑 1k/5k/10k 且每档 50 次")
    parser.add_argument("--apply", action="store_true", help="把建议写入两个 scope 的 maintenance.json")
    parser.add_argument("--user-home", default=os.path.expanduser(os.path.join("~", ".dsh", "graph-memory")))
    parser.add_argument("--workspace-home", default=os.path.join(os.getcwd(), ".dsh", "graph-memory"))
    args = parser.parse_args()
    sizes = [int(part) for part in args.sizes.split(",") if part.strip()]
    samples = 50 if args.quick else args.samples
    if args.quick:
        sizes = [size for size in sizes if size <= 10000] or [1000, 5000, 10000]

    tmp_root = os.path.join(ROOT, ".tmp", f"calibrate-{uuid.uuid4().hex[:8]}")
    os.makedirs(tmp_root, exist_ok=True)
    try:
        rows = calibrate_sizes(sizes, samples, tmp_root)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    passing = [row for row in rows if row["under_budget"]]
    max_passing = max((row["size"] for row in passing), default=0)
    workspace_high = max(MIN_HIGH, int(max_passing * HIGH_FACTOR))
    if workspace_high <= MIN_HIGH and not passing:
        workspace_high = MIN_HIGH
    # user scope 天然量少：不超过 5000，也不低于 2000
    user_high = max(MIN_HIGH, min(5000, int(workspace_high * 0.25)))
    recommendation = {
        "p95_budget_ms": P95_BUDGET_MS,
        "sizes": rows,
        "max_passing_size": max_passing,
        "watermarks": {
            "workspace": {
                "high": workspace_high,
                "low": int(workspace_high * 0.8),
                "hard": int(workspace_high * 2.5),
            },
            "user": {
                "high": user_high,
                "low": int(user_high * 0.8),
                "hard": int(user_high * 2.5),
            },
        },
        "generated_at": time.time(),
        "note": "high = 满足 P95<150ms 的最大档 × 0.7；low=high×0.8；hard=high×2.5；夹逼下限 2000。",
    }
    out_path = os.path.join(ROOT, "docs", "calibration.json")
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(recommendation, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(json.dumps(recommendation["watermarks"], ensure_ascii=False, indent=2))
    print(f"[calibrate] 报告写入 {out_path}")

    if args.apply:
        for scope, home in (("user", args.user_home), ("workspace", args.workspace_home)):
            os.makedirs(home, exist_ok=True)
            maintenance_path = os.path.join(home, "maintenance.json")
            state = {}
            if os.path.isfile(maintenance_path):
                try:
                    with open(maintenance_path, "r", encoding="utf-8-sig") as fh:
                        state = json.load(fh) or {}
                except (OSError, json.JSONDecodeError):
                    state = {}
            state["calibration"] = dict(recommendation["watermarks"][scope])
            with open(maintenance_path, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            print(f"[calibrate] 已写入 {maintenance_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
