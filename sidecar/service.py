"""MemoryService：JSON-RPC 方法的业务编排层。

职责：
- 装配 per-scope 的 GraphStore / Journal / DayKeeper / Pruner；
- 实现 §3.1 全部公开方法与 §3.2 内部方法；
- 所有跨模块调用只经过 docs/CONTRACT.md §4 约定的接口。
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import re
import time
from typing import Any, Optional

import noise
import retrieval
import supersede
import budget as budget_mod
from base import (
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    STATUS_COMPRESSED,
    STATUS_PENDING_FORGET,
    Config,
    GraphMemoryError,
    append_jsonl,
    estimate_tokens,
    log,
    new_id,
    normalize_space,
    read_json,
    today_str,
    write_json_atomic,
)
from daykeeper import DayKeeper
from extract import extract_memories
from graph import GraphStore
from journal import SQLiteJournal
from llm_bridge import LlmBridge
from prune import Pruner
from weight import activate as weight_activate
from weight import current_w, score as weight_score

INJECTION_STATE_FILE = "sidecar.json"


class Clock:
    """可注入伪时钟（验收测试用它模拟 90 天，不需要真实 sleep）。"""

    def __init__(self) -> None:
        self._offset = 0.0

    def now(self) -> float:
        return time.time() + self._offset

    def set_now(self, ts: float) -> None:
        self._offset = float(ts) - time.time()

    def advance(self, seconds: float) -> None:
        self._offset += float(seconds)

    def reset(self) -> None:
        self._offset = 0.0


def _format_item(item: dict[str, Any]) -> str:
    updated = item.get("updated") or ""
    if isinstance(updated, (int, float)):
        updated = time.strftime("%m-%d", time.localtime(updated))
    elif isinstance(updated, str) and len(updated) >= 10:
        updated = updated[5:10]
    flag = " | 已更新" if item.get("recently_updated") else ""
    return f"[#{item.get('id')} | w={float(item.get('w') or 0):.1f} | {updated} 更新{flag}] {item.get('text')}"


# 改口语句里的显式替换目标：「换成 yarn」「改用 Podman」「现在用 Cursor」
_REPLACEMENT_TARGET = re.compile(r"(?:改成|换成|改用|转用|现在用|统一用)\s*([A-Za-z0-9_\-.\u4e00-\u9fff]{1,20})")


def _replacement_target(utterance: str) -> str:
    """从「换成 X / 改用 X」里取出新值；取不到返回空串。"""
    match = _REPLACEMENT_TARGET.search(utterance or "")
    if not match:
        return ""
    return match.group(1).strip().rstrip("，。；、)）")


def _touched_in_turn(node: dict[str, Any], turn_id: str) -> bool:
    """该节点是否由本轮对话创建/更新（改口只能覆盖「此前」的说法）。"""
    turns = ((node.get("properties") or {}).get("source_turn_ids") or [])
    return bool(turn_id) and turn_id in turns


class MemoryService:
    def __init__(self, config: Config, *, peer: Optional[Any] = None) -> None:
        self.config = config
        self.clock = Clock()
        self.peer = peer
        self.llm = LlmBridge(config, peer)
        self.scopes = ("user", "workspace")
        self.paths: dict[str, str] = {}
        self.graphs: dict[str, Any] = {}
        self.journals: dict[str, Any] = {}
        self.daykeepers: dict[str, DayKeeper] = {}
        self.pruners: dict[str, Pruner] = {}
        self.workspace_root = os.path.abspath(
            os.environ.get("GRAPH_MEMORY_WORKSPACE_ROOT") or os.getcwd()
        )
        self.noise_words = noise.load_noise_words(config.get("noise.wordsPath") or self._noise_path())
        self.last_injection: dict[str, Any] = {}
        self.started_at = time.time()
        self._background: set[asyncio.Task] = set()
        for scope in self.scopes:
            home = self._home_for(scope)
            self.paths[scope] = home
            os.makedirs(home, exist_ok=True)
            self.graphs[scope] = self._make_graph(scope, home)
            self.journals[scope] = SQLiteJournal(os.path.join(home, "journal.db"), clock=self.clock.now)
            self.daykeepers[scope] = DayKeeper(home, scope, config, self.graphs[scope], self.llm, clock=self.clock.now)
            self.pruners[scope] = Pruner(
                home, scope, config, self.graphs[scope], self.journals[scope], self.llm, clock=self.clock.now
            )
        try:
            state = read_json(os.path.join(self.paths["workspace"], INJECTION_STATE_FILE), {})
            if isinstance(state, dict):
                self.last_injection = state.get("last_injection") or {}
        except Exception:  # noqa: BLE001
            self.last_injection = {}
        self._restore_budget()

    # ------------------------------------------------------------------ #
    # 注入预算（可调 [3000, 2_000_000]，默认 3000）
    # ------------------------------------------------------------------ #
    def _restore_budget(self) -> None:
        """面板写入的预算优先于 config，保证重启后仍然生效。"""
        stored = budget_mod.load_runtime_budget(self.paths["user"])
        value = budget_mod.clamp_budget(
            stored if stored is not None else self.config.get("injection.totalHardTokens")
        )
        self.config.data.setdefault("injection", {})["totalHardTokens"] = value

    def caps(self) -> dict[str, int]:
        return budget_mod.injection_caps(self.config)

    def total_budget(self) -> int:
        return budget_mod.total_budget(self.config)

    def budget_report(self) -> dict[str, Any]:
        total = self.total_budget()
        price = budget_mod.pricing(self.config)
        injected = int(((self.last_injection or {}).get("tokens") or {}).get("total") or 0)
        return {
            "max_tokens": total,
            "min": budget_mod.BUDGET_MIN,
            "max": budget_mod.BUDGET_MAX,
            "default": budget_mod.BUDGET_DEFAULT,
            "caps": self.caps(),
            "pricing": price,
            "cost_per_message": budget_mod.cost_per_message(total, price),
            "cost_last_injection": budget_mod.cost_per_message(injected, price),
            "last_injection_tokens": injected,
        }

    async def budget_state(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.budget_report()

    async def set_budget(self, params: dict[str, Any]) -> dict[str, Any]:
        raw = params.get("max_tokens", params.get("maxTokens"))
        value = budget_mod.clamp_budget(raw)
        self.config.data.setdefault("injection", {})["totalHardTokens"] = value
        budget_mod.save_runtime_budget(self.paths["user"], value, updated_at=self.now())
        log("info", "injection budget updated", max_tokens=value, caps=self.caps())
        return self.budget_report()

    # ------------------------------------------------------------------ #
    # 基础设施
    # ------------------------------------------------------------------ #
    def now(self) -> float:
        return self.clock.now()

    def _noise_path(self) -> str:
        return os.path.join(self.config.base_dir, "config", "noiseWords.yaml")

    def _home_for(self, scope: str) -> str:
        if scope == "user":
            return os.path.abspath(self.config.get("paths.userDataHome") or os.path.join(os.path.expanduser("~"), ".dsh", "graph-memory"))
        return os.path.abspath(self.config.get("paths.workspaceDataHome") or os.path.join(self.workspace_root, ".dsh", "graph-memory"))

    def _make_graph(self, scope: str, home: str) -> Any:
        backend = (self.config.get("graph.backend") or "embedded").lower()
        if backend == "lightrag":
            from graph_lightrag import LightRAGStore

            url = self.config.get("graph.lightragUrl")
            if not url:
                log("warning", "graph.backend=lightrag but graph.lightragUrl missing; falling back to embedded")
            else:
                return LightRAGStore(
                    home,
                    scope,
                    base_url=url,
                    api_key=self.config.get("graph.lightragApiKey"),
                    timeout=float(self.config.get("graph.lightragTimeoutMs") or 5000) / 1000.0,
                    clock=self.clock.now,
                )
        return GraphStore(home, scope, embedding_dim=int(self.config.get("graph.embeddingDim") or 384), clock=self.clock.now)

    def journal_for(self, scope: str) -> Any:
        return self.journals.get(scope) or self.journals["workspace"]

    def graph_for(self, scope: str) -> Any:
        return self.graphs.get(scope) or self.graphs["workspace"]

    def _normalize_scope(self, raw: Any, *, default: str = "workspace") -> str:
        if isinstance(raw, str) and raw in self.scopes:
            return raw
        return default

    def _scope_list(self, raw: Any) -> list[str]:
        if isinstance(raw, str) and raw == "auto":
            return list(self.scopes)
        if isinstance(raw, (list, tuple)):
            return [s for s in raw if s in self.scopes] or list(self.scopes)
        if isinstance(raw, str) and raw in self.scopes:
            return [raw]
        return list(self.scopes)

    def _save(self, *scopes: str) -> None:
        for scope in scopes or self.scopes:
            graph = self.graphs.get(scope)
            if graph is not None:
                try:
                    graph.save()
                except Exception as exc:  # noqa: BLE001
                    log("error", "graph save failed", scope=scope, error=str(exc))

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)

        def _done(t: asyncio.Task) -> None:
            self._background.discard(t)
            try:
                t.result()
            except Exception as exc:  # noqa: BLE001
                log("error", "background task failed", error=str(exc))

        task.add_done_callback(_done)

    # ------------------------------------------------------------------ #
    # retrieve / activate（R5）
    # ------------------------------------------------------------------ #
    async def retrieve(self, params: dict[str, Any]) -> dict[str, Any]:
        query = str(params.get("query") or "").strip()
        scopes = self._scope_list(params.get("scope"))
        explicit = bool(params.get("explicit"))
        if not query:
            return {"items": [], "injected_ids": [], "tokens": {"memory": 0}, "top1_sim": 0.0, "high_weight_count": 0}
        try:
            started = time.perf_counter()
            result = retrieval.retrieve(self, query, scopes, explicit=explicit, top_k=int(self.config.get("graph.topK") or 20))
            # 运行期自适应水位线：记录检索延迟，prune.py 按每日 P95 调整 high（§2.6）
            try:
                latency_ms = (time.perf_counter() - started) * 1000.0
                self.pruners["workspace"].on_retrieval_p95(latency_ms, len(result.get("items") or []))
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            log("warning", "retrieval failed", error=str(exc))
            return {"items": [], "injected_ids": [], "tokens": {"memory": 0}, "top1_sim": 0.0, "high_weight_count": 0}
        budget = params.get("budget") or {}
        mem_max = int(budget.get("mem_max") or self.caps()["memoryMaxTokens"])
        items: list[dict[str, Any]] = []
        used = 0
        for item in result.get("items") or []:
            text = _format_item(item)
            cost = estimate_tokens(text)
            item = dict(item)
            item["rendered"] = text
            item["token_cost"] = cost
            if items and used + cost > mem_max:
                break
            items.append(item)
            used += cost
            if used >= mem_max:
                break
        return {
            "items": items,
            "injected_ids": [item["id"] for item in items],
            "tokens": {"memory": used},
            "top1_sim": float(result.get("top1_sim") or 0.0),
            "high_weight_count": int(result.get("high_weight_count") or 0),
            "scopes": scopes,
        }

    async def activate(self, params: dict[str, Any]) -> dict[str, Any]:
        ids = [str(x) for x in (params.get("ids") or []) if x]
        reason = str(params.get("reason") or "injected")
        updated: list[dict[str, Any]] = []
        if not ids:
            return {"updated": []}
        now = self.now()
        max_weight = float(self.config.get("weight.maxWeight") or 10.0)
        gain = float(self.config.get("weight.baseActivationGain") or 0.5)
        for entity_id in ids:
            for scope in self.scopes:
                graph = self.graphs[scope]
                node = graph.get_entity(entity_id)
                if node is None:
                    continue
                props = node.get("properties") or {}
                w_before = current_w(node, now)
                new_props = weight_activate(
                    node,
                    now,
                    pinned=bool(props.get("pinned")),
                    tool_boosted=reason in ("tool", "pin", "memory_pin"),
                    workspace_hit=(scope == "workspace" and reason == "injected"),
                    max_weight=max_weight,
                    gain=gain,
                )
                graph.update_properties(entity_id, new_props)
                self.journals[scope].log_activation(scope, entity_id, reason, w_before, float(new_props.get("w") or 0))
                updated.append({"id": entity_id, "w": float(new_props.get("w") or 0), "scope": scope})
                self._save(scope)
                break
        return {"updated": updated}

    # ------------------------------------------------------------------ #
    # ingest（写入闭环）
    # ------------------------------------------------------------------ #
    async def ingest(self, params: dict[str, Any]) -> dict[str, Any]:
        turn_text = str(params.get("turn_text") or "")
        turn_id = str(params.get("turn_id") or new_id("turn"))
        scope = self._normalize_scope(params.get("scope"))
        pinned = bool(params.get("pinned"))
        tool_boosted = bool(params.get("tool_boosted"))
        session_id = str(params.get("session_id") or "")
        max_chars = int(self.config.get("ingest.maxTurnChars") or 12000)
        if len(turn_text) > max_chars:
            turn_text = turn_text[:max_chars]
        graph = self.graphs[scope]
        journal = self.journals[scope]
        now = self.now()
        date = today_str(now)
        result: dict[str, Any] = {
            "accepted": True,
            "turn_id": turn_id,
            "scope": scope,
            "node_ids": [],
            "edges": [],
            "superseded": [],
            "dropped": None,
            "chunk_id": None,
        }
        if not turn_text.strip():
            result["accepted"] = False
            result["dropped"] = {"reason": "empty"}
            return result
        # 当天原文：无论是否进图都要保留（day_context 与补召回需要）
        try:
            await self.daykeepers[scope].append_turn(turn_text, turn_id, now)
            result["chunk_id"] = graph.add_chunk(turn_text[:4000], scope, turn_id, date, kind="turn")
        except Exception as exc:  # noqa: BLE001
            log("warning", "day buffer append failed", error=str(exc))
        # 闸门 1~3
        filtered = noise.filter_turn(
            turn_text,
            noise_words=self.noise_words,
            journal=journal,
            scope=scope,
        )
        if filtered.get("alarm") and self.peer is not None:
            try:
                self.peer.notify("warning", {"kind": "credential", "scope": scope, "turn_id": turn_id})
            except Exception:  # noqa: BLE001
                pass
        result["dropped"] = {
            "dropped": bool(filtered.get("dropped")),
            "reason": filtered.get("reason") or "",
            "sanitized": bool(filtered.get("sanitized")),
        }
        if filtered.get("dropped"):
            journal.log_ingest(scope, turn_id, "dropped", 0, 0, reason=filtered.get("reason") or "")
            self._save(scope)
            return result
        cleaned = str(filtered.get("text") or "")
        # 抽取（重试 2 次 + 死信）
        retries = int(self.config.get("ingest.retries") or 2)
        backoff = self.config.get("ingest.retryBackoffMs") or [200, 800]
        candidates: list[dict[str, Any]] = []
        last_error = ""
        for attempt in range(retries + 1):
            try:
                candidates = await extract_memories(cleaned, scope=scope, llm=self.llm)
                last_error = ""
                break
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                if attempt < retries:
                    delay = float(backoff[min(attempt, len(backoff) - 1)]) / 1000.0
                    await asyncio.sleep(delay)
        if last_error:
            journal.enqueue_dead_letter(scope, {"turn_text": turn_text, "turn_id": turn_id, "scope": scope}, last_error)
            result["accepted"] = False
            result["dead_letter"] = True
            self._save(scope)
            return result
        # 闸门 4 + 改口检测 + 图写入
        node_ids: list[str] = []
        edges: list[list[str]] = []
        superseded: list[dict[str, Any]] = []
        max_candidates = 24
        min_sim = float(self.config.get("supersede.minSim") or 0.62)
        max_sim = float(self.config.get("supersede.maxSim") or 0.95)
        dedupe_sim = float(self.config.get("graph.dedupeSim") or 0.95)
        negation_threshold = float(self.config.get("supersede.negationConfidenceThreshold") or 0.6)
        default_threshold = float(self.config.get("supersede.confidenceThreshold") or 0.7)
        for candidate in candidates[:max_candidates]:
            name = normalize_space(str(candidate.get("name") or ""))[:120]
            description = str(candidate.get("description") or "").strip()
            etype = str(candidate.get("type") or "Concept")
            if not name or not description:
                continue
            existing = graph.find_by_name(name)
            # 先做改口判定（同名的新描述也可能是否定式改口），再做同名合并/去重/新建
            neighbors = graph.search_entities(f"{name}\n{description}", scope, top_k=5)
            best_id, best_sim = (neighbors[0][0], neighbors[0][1]) if neighbors else ("", 0.0)
            # 改口只覆盖「本轮之前」的说法：同一轮里刚建/刚更新的节点不能当旧说法。
            prior_id, prior_sim = next(
                ((eid, sim) for eid, sim in neighbors if not _touched_in_turn(graph.get_entity(eid) or {}, turn_id)),
                ("", 0.0),
            )
            if prior_id and prior_sim >= min_sim:
                target = graph.get_entity(prior_id)
                if target is not None:
                    verdict = await supersede.judge_supersede(
                        target.get("description", ""), description, cleaned, self.llm
                    )
                    threshold = (
                        negation_threshold
                        if (supersede.detect_negation(cleaned) or supersede.detect_negation(description))
                        else default_threshold
                    )
                    confidence = float(verdict.get("confidence") or 0.0)
                    if verdict.get("is_same_attribute") and verdict.get("is_contradiction") and confidence >= threshold:
                        change = await supersede.apply_supersede(
                            self,
                            target,
                            description,
                            confidence=confidence,
                            source_turn_id=turn_id,
                            scope=scope,
                        )
                        superseded.append(change)
                        node_ids.append(prior_id)
                        continue
            if best_id and best_sim > dedupe_sim:
                # >0.95：不新建，只对已有节点 activate（闸门 4）
                await self.activate({"ids": [best_id], "reason": "dedupe"})
                node_ids.append(best_id)
                graph.update_properties(best_id, {"source_turn_ids": self._merge_ids(graph.get_entity(best_id), turn_id)})
                continue
            if existing is not None:
                # 同名且未判定为矛盾：LightRAG 语义的合并（描述追加、权重不动）
                graph.upsert_entity(name, etype, description, scope, properties={"source_turn_ids": [turn_id]})
                node_ids.append(existing["id"])
                continue
            properties: dict[str, Any] = {
                "w": float(self.config.get("weight.pinWeight") if (pinned or tool_boosted) else (self.config.get("weight.defaultWeight") or 1.0)),
                "pinned": bool(pinned or tool_boosted),
                "source_turn_ids": [turn_id],
                "source_chunk_ids": [result["chunk_id"]] if result.get("chunk_id") else [],
            }
            entity_id = graph.upsert_entity(name, etype, description, scope, properties=properties)
            node_ids.append(entity_id)
        # 改口兜底：抽取不出实体、但整句含明确否定/改口信号时（如“现在不喜欢了”），
        # 用原句对最近邻做一次矛盾判定并覆盖式改写（C 用例 Day2 路径，红线 R6 同步向量）。
        if not superseded and supersede.detect_negation(cleaned):
            fallback = await self._supersede_from_utterance(cleaned, scope, turn_id)
            if fallback:
                superseded.extend(fallback)
                node_ids.extend(str(change.get("node_id")) for change in fallback if change.get("node_id"))
        # 关系：同一轮抽取出的实体两两建立共现边（限制规模）
        unique_ids = list(dict.fromkeys(node_ids))
        for i in range(min(len(unique_ids), 8)):
            for j in range(i + 1, min(len(unique_ids), 8)):
                rel_id = graph.upsert_relation(
                    unique_ids[i],
                    unique_ids[j],
                    "同一轮对话中共同出现。",
                    scope,
                    keywords="co-occurrence",
                )
                edges.append([unique_ids[i], unique_ids[j], rel_id])
        journal.log_ingest(scope, turn_id, "ingested", len(unique_ids), len(edges))
        self._save(scope)
        result["node_ids"] = unique_ids
        result["edges"] = edges
        result["superseded"] = superseded
        return result

    @staticmethod
    def _merge_ids(node: Optional[dict[str, Any]], turn_id: str) -> list[str]:
        current = list(((node or {}).get("properties") or {}).get("source_turn_ids") or [])
        if turn_id not in current:
            current.append(turn_id)
        return current[-50:]

    async def _supersede_from_utterance(self, utterance: str, scope: str, turn_id: str) -> list[dict[str, Any]]:
        """抽取阶段没有产出实体时的改口兜底（C 用例 Day2：“现在不喜欢了”）。

        只在整句含否定/改口信号时启用；对最近邻做 LLM 矛盾判定，通过后走标准六步改写。
        """
        if not supersede.detect_negation(utterance):
            return []
        graph = self.graphs[scope]
        config_min_sim = float(self.config.get("supersede.minSim") or 0.62)
        threshold = float(self.config.get("supersede.negationConfidenceThreshold") or 0.6)
        # 兜底允许比常规阈值更低的相似度：否定句本身信息少，检索只用于选候选，最终由 LLM 判定
        fallback_min_sim = min(0.25, config_min_sim * 0.5)
        neighbors = graph.search_entities(utterance, scope, top_k=5)
        for entity_id, sim in neighbors:
            if sim < fallback_min_sim:
                continue
            node = graph.get_entity(entity_id)
            if node is None:
                continue
            if _touched_in_turn(node, turn_id):
                continue  # 本轮刚写入的节点不是「旧说法」
            status = (node.get("properties") or {}).get("status")
            if status not in (None, "active", "compressed"):
                continue
            old_description = str(node.get("description") or "")
            if not old_description:
                continue
            verdict = await supersede.judge_supersede(old_description, utterance, utterance, self.llm)
            confidence = float(verdict.get("confidence") or 0.0)
            if not (verdict.get("is_same_attribute") and verdict.get("is_contradiction") and confidence >= threshold):
                continue
            new_value = await self._compose_rewritten_description(node, utterance)
            if not new_value or new_value == old_description:
                continue
            change = await supersede.apply_supersede(
                self,
                node,
                new_value,
                confidence=confidence,
                source_turn_id=turn_id,
                scope=scope,
            )
            log("info", "utterance-level supersede applied", scope=scope, node_id=entity_id, sim=round(sim, 3), confidence=confidence)
            return [change]
        return []

    async def _compose_rewritten_description(self, node: dict[str, Any], utterance: str) -> str:
        """把“现在不喜欢了”这类无宾语的否定句改写成可独立理解的新描述。

        先用 LLM 生成（purpose="rewrite"），失败/异常时用确定性规则兜底；结果必须包含
        旧描述里的核心对象或明确新值，否则退回原句 + 旧对象拼接。
        """
        old_description = str(node.get("description") or "").strip()
        name = str(node.get("name") or "").strip()
        prompt = (
            "你是记忆改写器。旧记忆描述与用户最新一句话如下。请输出一句不超过 60 字的中文新描述，"
            "保留旧描述中的核心对象，体现最新说法（否定/替换/取消），只输出新描述本身。\n"
            f"<<<OLD>>>{old_description}\n<<<NEW>>>{utterance}"
        )
        candidate = ""
        try:
            candidate = (await self.llm.complete(prompt, max_tokens=120, purpose="rewrite")).strip()
        except Exception:  # noqa: BLE001
            candidate = ""
        candidate = candidate.strip().strip('"').strip("'").splitlines()[0].strip() if candidate else ""
        # 提示词回显防护：模型（或降级路径）把提示词原文当回答时一律丢弃，避免
        # 「你是记忆改写器…<<<OLD>>>…」被写进图谱描述。
        if candidate and ("<<<OLD>>>" in candidate or "<<<NEW>>>" in candidate or "记忆改写器" in candidate):
            candidate = ""
        if candidate and 2 <= len(candidate) <= 200 and candidate != old_description:
            return candidate
        # 确定性兜底：喜欢→不喜欢 / 不再 X / 明确替换目标 / 旧对象 + 最新说法
        if "不喜欢" in utterance and "喜欢" in old_description and "不喜欢" not in old_description:
            return old_description.replace("喜欢", "不喜欢", 1)
        if "不再" in utterance and "喜欢" in old_description:
            return old_description.replace("喜欢", "不再喜欢", 1)
        replaced = _replacement_target(utterance)
        if replaced:
            return f"{name or old_description}已改用{replaced}"[:200]
        core = name or old_description
        return f"{core}：{utterance}（已改口）"[:200]

    # ------------------------------------------------------------------ #
    # update / forget / set_weight
    # ------------------------------------------------------------------ #
    async def update(self, params: dict[str, Any]) -> dict[str, Any]:
        query = str(params.get("query") or "").strip()
        new_value = str(params.get("new_value") or "").strip()
        scope = self._normalize_scope(params.get("scope"))
        if not query or not new_value:
            raise GraphMemoryError("query and new_value are required", code="invalid_params")
        graph = self.graphs[scope]
        found = graph.search_entities(query, scope, top_k=1)
        if not found:
            raise GraphMemoryError("no matching memory", code="memory_not_found")
        node = graph.get_entity(found[0][0])
        if node is None:
            raise GraphMemoryError("no matching memory", code="memory_not_found")
        change = await supersede.apply_supersede(
            self, node, new_value, confidence=1.0, source_turn_id="tool_update", scope=scope
        )
        self._save(scope)
        return {"node_id": change.get("node_id") or node["id"], "before": change.get("before"), "after": change.get("after")}

    async def forget(self, params: dict[str, Any]) -> dict[str, Any]:
        target = str(params.get("id") or params.get("query") or "")
        scope = self._normalize_scope(params.get("scope"))
        graph = self.graphs[scope]
        node: Optional[dict[str, Any]] = None
        if target.startswith("ent_"):
            node = graph.get_entity(target)
        else:
            found = graph.search_entities(target, scope, top_k=1)
            node = graph.get_entity(found[0][0]) if found else None
        if node is None:
            raise GraphMemoryError("no matching memory", code="memory_not_found")
        before = copy.deepcopy(node.get("properties") or {})
        after = {"status": STATUS_PENDING_FORGET, "w": 0.0, "updated_at": self.now()}
        graph.update_properties(node["id"], dict(after))
        self.journals[scope].log_change(scope, node["id"], "forget", {"properties": before}, {"properties": after})
        self._save(scope)
        return {"node_id": node["id"], "status": STATUS_PENDING_FORGET, "note": "将在后续维护轮次按 compress→prune 两阶段归档（可回滚）"}

    async def set_weight(self, params: dict[str, Any]) -> dict[str, Any]:
        entity_id = str(params.get("id") or "")
        weight_value = float(params.get("w") or 0.0)
        weight_value = max(0.0, min(float(self.config.get("weight.maxWeight") or 10.0), weight_value))
        for scope in self.scopes:
            graph = self.graphs[scope]
            node = graph.get_entity(entity_id)
            if node is None:
                continue
            before = copy.deepcopy(node.get("properties") or {})
            after = {"w": weight_value, "last_activated_at": self.now(), "updated_at": self.now()}
            graph.update_properties(entity_id, dict(after))
            self.journals[scope].log_change(scope, entity_id, "weight", {"properties": before}, {"properties": after})
            self._save(scope)
            return {"id": entity_id, "w": weight_value, "scope": scope}
        raise GraphMemoryError("no matching memory", code="memory_not_found")

    # ------------------------------------------------------------------ #
    # compress / prune / maintenance（R4）
    # ------------------------------------------------------------------ #
    async def compress(self, params: dict[str, Any]) -> dict[str, Any]:
        scope = self._normalize_scope(params.get("scope"))
        result = await self.pruners[scope].compress()
        self._save(scope)
        self.journals[scope].log_prune_report(scope, "compress", result)
        return result

    async def prune(self, params: dict[str, Any]) -> dict[str, Any]:
        scope = self._normalize_scope(params.get("scope"))
        dry_run = params.get("dry_run")
        if dry_run is None:
            dry_run = bool(self.config.get("prune.dryRunDefault", True))
        result = await self.pruners[scope].prune(dry_run=bool(dry_run))
        if not dry_run:
            self._save(scope)
        self.journals[scope].log_prune_report(scope, "prune", result)
        return result

    async def round_tick(self, params: dict[str, Any]) -> dict[str, Any]:
        scope = self._normalize_scope(params.get("scope"))
        round_no = self.pruners[scope].round_tick()
        return {"scope": scope, "round": round_no}

    async def maintenance(self, params: dict[str, Any]) -> dict[str, Any]:
        """每个 turn 结束调用：推进轮次并按门控自动执行 compress / prune。"""
        scope = self._normalize_scope(params.get("scope"))
        pruner = self.pruners[scope]
        round_no = pruner.round_tick()
        actions: list[dict[str, Any]] = []
        gate = pruner.gate()
        try:
            state = pruner.state()
            phase = str(state.get("phase") or "none")
            action = str(gate.get("action") or "idle")
            if action in ("compress", "force") and phase != "compressed":
                compressed = await pruner.compress()
                actions.append({"action": "compress", "result": compressed})
                try:
                    self.journals[scope].log_prune_report(scope, "compress", compressed)
                except Exception:  # noqa: BLE001
                    pass
                self._save(scope)
            elif action in ("prune", "force") and phase == "compressed":
                pruned = await pruner.prune(dry_run=False)
                actions.append({"action": "prune", "result": pruned})
                try:
                    self.journals[scope].log_prune_report(scope, "prune", pruned)
                except Exception:  # noqa: BLE001
                    pass
                self._save(scope)
        except Exception as exc:  # noqa: BLE001
            log("warning", "maintenance failed", scope=scope, error=str(exc))
            actions.append({"action": "error", "error": str(exc)})
        return {"scope": scope, "round": round_no, "gate": gate, "actions": actions}

    # ------------------------------------------------------------------ #
    # day_pack（恒定 day_context，R8）
    # ------------------------------------------------------------------ #
    async def day_pack(self, params: dict[str, Any]) -> dict[str, Any]:
        query = str(params.get("query") or "").strip()
        scopes = self._scope_list(params.get("scope"))
        day_text = ""
        details: list[str] = []
        history = ""
        tokens = {"day": 0, "detail": 0, "history": 0, "total": 0}
        for scope in scopes:
            keeper = self.daykeepers[scope]
            try:
                # 压缩/跨天合并可能调用 LLM：放到后台，绝不阻塞本步 300ms 注入预算
                self._spawn(keeper.rollover_if_needed(self.now()))
                self._spawn(keeper.maybe_compress())
                packed = await keeper.day_pack(query)
            except Exception as exc:  # noqa: BLE001
                log("warning", "day_pack failed", scope=scope, error=str(exc))
                continue
            if not day_text and packed.get("day_text"):
                day_text = packed["day_text"]
            if packed.get("detail_text"):
                details.append(packed["detail_text"])
            if not history and packed.get("history_text"):
                history = packed["history_text"]
            tokens["day"] = max(tokens["day"], int((packed.get("tokens") or {}).get("day") or 0))
            tokens["detail"] = max(tokens["detail"], int((packed.get("tokens") or {}).get("detail") or 0))
            tokens["history"] = max(tokens["history"], int((packed.get("tokens") or {}).get("history") or 0))
        detail_text = "\n".join(details)[:2000]
        total = tokens["day"] + tokens["detail"] + tokens["history"]
        return {
            "day_text": day_text,
            "detail_text": detail_text,
            "history_text": history,
            "tokens": {**tokens, "total": total},
        }

    # ------------------------------------------------------------------ #
    # Journal / 面板 / 统计
    # ------------------------------------------------------------------ #
    async def journal_list(self, params: dict[str, Any]) -> dict[str, Any]:
        scope = self._normalize_scope(params.get("scope"))
        return {"entries": self.journals[scope].list_entries(scope, params.get("target_id"))}

    async def journal_rollback(self, params: dict[str, Any]) -> dict[str, Any]:
        change_id = int(params.get("change_id") or 0)
        for scope in self.scopes:
            entry = self.journals[scope].get_change(change_id)
            if entry is not None:
                restored = self.journals[scope].rollback(change_id, self.graphs[scope], self.now())
                self._save(scope)
                # 契约：{restored:{id,before,after}}；journal 可能多包一层，这里统一摊平
                if isinstance(restored, dict) and "restored" in restored and isinstance(restored["restored"], dict):
                    restored = restored["restored"]
                return {"restored": restored, "scope": scope}
        raise GraphMemoryError("change not found or not rollback-able", code="rollback_unavailable")

    async def stats(self, params: dict[str, Any]) -> dict[str, Any]:
        scopes: dict[str, Any] = {}
        for scope in self.scopes:
            graph = self.graphs[scope]
            pruner_state = self.pruners[scope].state()
            day_state = self.daykeepers[scope].state()
            scopes[scope] = {
                "nodes": graph.count(include_archived=True),
                "nodes_live": graph.count(),
                "edges": graph.relation_count(),
                "chunks": len(getattr(graph, "chunks", {})),
                "watermarks": {
                    "high": pruner_state.get("high"),
                    "low": pruner_state.get("low"),
                    "hard": pruner_state.get("hard"),
                },
                "phase": pruner_state.get("phase"),
                "round": pruner_state.get("round"),
                "day_tokens": day_state.get("day_tokens"),
                "history_tokens": day_state.get("history_tokens"),
                "data_home": self.paths[scope],
            }
        return {
            "scopes": scopes,
            "injection_last": self.last_injection,
            "sidecar": {
                "version": "0.1.0",
                "pid": os.getpid(),
                "started_at": self.started_at,
                "llm_mode": self.llm.mode,
            },
            "graph_backend": (self.config.get("graph.backend") or "embedded"),
        }

    async def panel_state(self, params: dict[str, Any]) -> dict[str, Any]:
        stats = await self.stats({})
        caps = self.caps()
        payload: dict[str, Any] = {
            "stats": stats,
            "injection_last": self.last_injection,
            "budget": self.budget_report(),
            "injection_config": {
                "total_hard_tokens": self.total_budget(),
                "system_max_tokens": caps["systemMaxTokens"],
                "memory_default_tokens": caps["memoryDefaultTokens"],
                "memory_high_sim_tokens": caps["memoryHighSimTokens"],
                "memory_boost_tokens": caps["memoryBoostTokens"],
                "memory_max_tokens": caps["memoryMaxTokens"],
                "day_max_tokens": caps["dayMaxTokens"],
                "day_text_max_tokens": caps["dayTextMaxTokens"],
                "detail_max_tokens": caps["detailMaxTokens"],
                "history_max_tokens": caps["historyMaxTokens"],
                "high_sim_threshold": float(self.config.get("injection.highSimThreshold") or 0.8),
                "high_weight_threshold": float(self.config.get("injection.highWeightThreshold") or 5),
            },
            "alarms": [],
            "journal_recent": [],
            "prune_recent": [],
            "nodes": {},
            "dead_letters": [],
        }
        for scope in self.scopes:
            journal = self.journals[scope]
            try:
                payload["alarms"] = journal.recent_alarms(scope, limit=20) or []
                payload["prune_recent"] = journal.prune_history(scope)[:10]
                payload["journal_recent"] = journal.list_entries(scope, limit=30)
                payload["dead_letters"] = journal.dead_letters(scope) if scope == "workspace" else payload["dead_letters"]
            except Exception:  # noqa: BLE001
                continue
        payload["nodes"] = {
            scope: [
                {
                    "id": n["id"],
                    "name": n.get("name"),
                    "type": n.get("type"),
                    "description": n.get("description"),
                    "properties": n.get("properties"),
                }
                for n in self.graphs[scope].list_entities(scope)[:500]
            ]
            for scope in self.scopes
        }
        return payload

    async def injection_record(self, params: dict[str, Any]) -> dict[str, Any]:
        self.last_injection = {
            "ts": self.now(),
            "session_id": params.get("session_id"),
            "turn": params.get("turn"),
            "tokens": params.get("tokens") or {},
            "injected_ids": params.get("injected_ids") or [],
            "scope": params.get("scope"),
        }
        scope = self._normalize_scope(params.get("scope"))
        try:
            self.journals[scope].log_injection(
                str(params.get("session_id") or ""),
                str(params.get("turn") or ""),
                params.get("tokens") or {},
                params.get("injected_ids") or [],
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            write_json_atomic(os.path.join(self.paths["workspace"], INJECTION_STATE_FILE), {"last_injection": self.last_injection})
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True}

    # ------------------------------------------------------------------ #
    # 冲突兜底 / 死信 / 令牌 / 调试
    # ------------------------------------------------------------------ #
    async def conflicts_sweep(self, params: dict[str, Any]) -> dict[str, Any]:
        scope = self._normalize_scope(params.get("scope"))
        days = int(params.get("days") or self.config.get("supersede.sweepDays") or 30)
        report = await supersede.sweep_conflicts(self, scope, days)
        self.journals[scope].log_alarm(scope, "conflict_report", report)
        return report

    async def dead_letter_list(self, params: dict[str, Any]) -> dict[str, Any]:
        scope = self._normalize_scope(params.get("scope"))
        return {"items": self.journals[scope].dead_letters(scope)}

    async def dead_letter_replay(self, params: dict[str, Any]) -> dict[str, Any]:
        dead_id = int(params.get("id") or 0)
        for scope in self.scopes:
            journal = self.journals[scope]
            item = journal.get_dead_letter(dead_id)
            if item is None:
                continue
            payload = item.get("payload") or {}
            journal.mark_dead_letter_replayed(dead_id)
            result = await self.ingest(payload)
            return {"replayed": True, "result": result}
        raise GraphMemoryError("dead letter not found", code="dead_letter_not_found")

    async def tokens(self, params: dict[str, Any]) -> dict[str, Any]:
        texts = params.get("texts") or []
        return {"tokens": [estimate_tokens(str(t)) for t in texts]}

    async def debug_set_now(self, params: dict[str, Any]) -> dict[str, Any]:
        if "ts" in params and params.get("ts") is not None:
            self.clock.set_now(float(params["ts"]))
        elif "advance_seconds" in params:
            self.clock.advance(float(params["advance_seconds"]))
        return {"now": self.now(), "today": today_str(self.now())}

    async def debug_seed_entities(self, params: dict[str, Any]) -> dict[str, Any]:
        """测试/校准专用：批量造合成节点（一次落盘，避免 O(N^2)）。"""
        scope = self._normalize_scope(params.get("scope"))
        count = int(params.get("count") or 0)
        prefix = str(params.get("prefix") or "synthetic")
        graph = self.graphs[scope]
        now = self.now()
        created: list[str] = []
        for i in range(count):
            name = f"{prefix}-{i}"
            eid = graph.upsert_entity(
                name,
                "Concept",
                f"{name} synthetic node for calibration and scale testing",
                scope,
                properties={"created_at": now, "updated_at": now, "last_activated_at": now, "w": 1.0},
            )
            created.append(eid)
        graph.save()
        return {"created": len(created), "scope": scope, "count": graph.count(include_archived=True)}

    async def debug_entity_set(self, params: dict[str, Any]) -> dict[str, Any]:
        entity_id = str(params.get("id") or "")
        for scope in self.scopes:
            graph = self.graphs[scope]
            node = graph.get_entity(entity_id)
            if node is None:
                continue
            graph.update_properties(entity_id, dict(params.get("properties") or {}))
            graph.save()
            return {"ok": True, "scope": scope, "node": graph.get_entity(entity_id)}
        raise GraphMemoryError("entity not found", code="entity_not_found")

    async def debug_state(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "now": self.now(),
            "today": today_str(self.now()),
            "paths": self.paths,
            "llm_mode": self.llm.mode,
            "pruners": {scope: self.pruners[scope].state() for scope in self.scopes},
            "daykeepers": {scope: self.daykeepers[scope].state() for scope in self.scopes},
        }

    async def calibrate(self, params: dict[str, Any]) -> dict[str, Any]:
        """应用校准结果（scripts/calibrate.py 实测 P95 后调用）。

        params: {scope?: str|"auto", high?: int, low?: int, hard?: int, dry_run?: bool}
        """
        scope = self._normalize_scope(params.get("scope"))
        pruner = self.pruners[scope]
        result = pruner.calibrate(
            high=params.get("high"),
            low=params.get("low"),
            hard=params.get("hard"),
            dry_run=bool(params.get("dry_run", False)),
        )
        return {"scope": scope, **result, "state": pruner.state()}

    # ------------------------------------------------------------------ #
    # 维护 / 关闭
    # ------------------------------------------------------------------ #
    async def flush(self) -> None:
        if self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)
        for scope in self.scopes:
            try:
                graph = self.graphs[scope]
                graph.save()
                if hasattr(graph, "_save_vectors"):
                    graph._save_vectors()
            except Exception as exc:  # noqa: BLE001
                log("warning", "flush failed", scope=scope, error=str(exc))

    async def close(self) -> None:
        await self.flush()
        for scope in self.scopes:
            try:
                self.journals[scope].close()
            except Exception:  # noqa: BLE001
                pass
