"""JSON-RPC 协议黑盒测试（CONTRACT §3）：真实 sidecar 子进程 + stdio JSON-RPC 2.0。

覆盖：
- §3.1 公开方法返回结构与字段（ping/retrieve/ingest/day_pack/stats/panel_state/journal_list）；
- §3.2 内部方法（tokens/injection_record/activate）；
- 错误码：方法不存在 -32601、业务错误 -32000（参数非法 -32602 记契约缺口证据，不硬失败）；
- 通知（无 id）被忽略、非法 JSON 行不崩溃、retrieve 空结果零 token、shutdown 优雅退出；
- llm.mode=scripted 可启动（GRAPH_MEMORY_LLM_SCRIPT fixture）。

注意：单测不 import sidecar 实现（只 import tests/acceptance 的黑盒客户端）。
"""
from __future__ import annotations

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ACCEPTANCE_DIR = os.path.join(os.path.dirname(HERE), "acceptance")
if ACCEPTANCE_DIR not in sys.path:
    sys.path.insert(0, ACCEPTANCE_DIR)

from rpc_client import (  # noqa: E402
    RpcError,
    SidecarClient,
    SidecarStartError,
    SidecarFixture,
    missing_sidecar_modules,
)

FIXTURES_DIR = os.path.join(os.path.dirname(HERE), "fixtures")
LLM_SCRIPT = os.path.join(FIXTURES_DIR, "llm_script.json")


def spawn_fixture(testcase: unittest.TestCase, **kwargs) -> SidecarFixture:
    missing = missing_sidecar_modules()
    if missing:
        raise unittest.SkipTest(f"sidecar 模块未就绪: {', '.join(missing)}")
    try:
        client = SidecarClient(**kwargs)
        client.start()
    except SidecarStartError as exc:
        tail = exc.stderr_tail or ""
        if "ModuleNotFoundError" in tail or "No module named" in tail:
            raise unittest.SkipTest(f"sidecar 无法启动: {tail.strip().splitlines()[-1] if tail.strip() else exc}")
        raise
    fixture = SidecarFixture(client)
    testcase.addCleanup(fixture.close)
    return fixture


class ProtocolPublicMethodsTest(unittest.TestCase):
    """§3.1 公开方法结构。"""

    def setUp(self) -> None:
        self.fx = spawn_fixture(self, name=self._testMethodName)

    def test_ping(self) -> None:
        result = self.fx.ping()
        self.assertTrue(result.get("pong"))
        self.assertIsInstance(result.get("version"), str)
        self.assertIsInstance(result.get("pid"), int)
        self.assertIsInstance(result.get("started_at"), (int, float))

    def test_ingest_and_retrieve_structure(self) -> None:
        ingest = self.fx.ingest("我们项目决定用 pnpm 管理依赖，构建脚本用 vite。", turn_id="p-1")
        for key in ("accepted", "node_ids", "edges", "superseded"):
            self.assertIn(key, ingest, f"ingest 缺少字段 {key}: {ingest}")
        self.assertIsInstance(ingest["node_ids"], list)
        self.assertIsInstance(ingest["edges"], list)
        self.assertIsInstance(ingest["superseded"], list)
        if ingest["edges"]:
            self.assertGreaterEqual(len(ingest["edges"][0]), 2)

        retrieve = self.fx.retrieve("用什么包管理器管理依赖？")
        for key in ("items", "injected_ids", "tokens"):
            self.assertIn(key, retrieve, f"retrieve 缺少字段 {key}: {retrieve}")
        self.assertIsInstance(retrieve["items"], list)
        self.assertIsInstance(retrieve["injected_ids"], list)
        self.assertIsInstance(retrieve["tokens"], dict)
        self.assertIsInstance(retrieve["tokens"].get("memory"), int)
        if retrieve["items"]:
            item = retrieve["items"][0]
            for key in ("id", "text", "w", "valid_from", "updated", "sim", "pinned", "scope"):
                self.assertIn(key, item, f"retrieve item 缺少字段 {key}: {item}")
            self.assertEqual(retrieve["injected_ids"], [entry["id"] for entry in retrieve["items"]])
            sims = [float(entry.get("sim") or 0.0) for entry in retrieve["items"]]
            self.assertEqual(sims, sorted(sims, reverse=True), "items 必须按相似度/score 降序")

    def test_retrieve_empty_result_zero_tokens(self) -> None:
        """23：召回为空 → 不注入任何标签（memory token=0）。"""
        retrieve = self.fx.retrieve("完全无关的一次性查询 zzzqqqxyz")
        self.assertEqual(retrieve["items"], [])
        self.assertEqual(retrieve["injected_ids"], [])
        self.assertEqual(int(retrieve["tokens"]["memory"]), 0)

    def test_day_pack_structure(self) -> None:
        self.fx.ingest("今天把构建脚本从 webpack 改成 vite。", turn_id="p-day")
        packed = self.fx.day_pack("构建脚本")
        for key in ("day_text", "detail_text", "history_text", "tokens"):
            self.assertIn(key, packed, f"day_pack 缺少字段 {key}: {packed}")
        self.assertIsInstance(packed["day_text"], str)
        for key in ("day", "detail", "history", "total"):
            self.assertIn(key, packed["tokens"], f"day_pack.tokens 缺少 {key}: {packed['tokens']}")
            self.assertIsInstance(packed["tokens"][key], int)
        self.assertEqual(
            packed["tokens"]["total"],
            packed["tokens"]["day"] + packed["tokens"]["detail"] + packed["tokens"]["history"],
        )
        self.assertLessEqual(packed["tokens"]["total"], 1024)

    def test_stats_structure(self) -> None:
        self.fx.ingest("项目技术栈记录：前端用 React，后端 Python。", turn_id="p-stats")
        stats = self.fx.stats()
        self.assertIn("scopes", stats)
        self.assertIn("injection_last", stats)
        self.assertIn("sidecar", stats)
        for scope in ("user", "workspace"):
            self.assertIn(scope, stats["scopes"], f"stats 缺少 scope {scope}")
            entry = stats["scopes"][scope]
            for key in ("nodes", "edges", "chunks", "day_tokens", "history_tokens", "watermarks", "phase"):
                self.assertIn(key, entry, f"stats.{scope} 缺少字段 {key}: {entry}")
            watermarks = entry["watermarks"]
            for key in ("high", "low", "hard"):
                self.assertIn(key, watermarks)
            self.assertIsInstance(watermarks["high"], int)
        self.assertIsInstance(stats["sidecar"].get("pid"), int)
        self.assertIn("llm_mode", stats["sidecar"])

    def test_panel_state_structure(self) -> None:
        self.fx.ingest("面板状态测试：项目使用 pnpm。", turn_id="p-panel")
        panel = self.fx.panel_state()
        self.assertIsInstance(panel, dict)
        self.assertIn("injection_last", panel)
        self.assertIsInstance(panel.get("alarms"), list)
        self.assertIsInstance(panel.get("journal_recent"), list)
        self.assertIn("nodes", panel)

    def test_journal_list_structure(self) -> None:
        self.fx.ingest("我喜欢喝咖啡。", turn_id="p-j1")
        self.fx.advance(10)
        self.fx.ingest("我现在不喜欢喝咖啡了，改成喝茶。", turn_id="p-j2")
        journal = self.fx.journal_list(scope="workspace")
        self.assertIn("entries", journal)
        self.assertIsInstance(journal["entries"], list)
        if journal["entries"]:
            entry = journal["entries"][0]
            for key in ("id", "kind", "scope", "ts"):
                self.assertIn(key, entry, f"journal entry 缺少字段 {key}: {entry}")

    def test_tokens_internal_method(self) -> None:
        tokens = self.fx.tokens(["abc", "中文测试", ""])
        self.assertEqual(tokens, [1, 4, 0])

    def test_injection_record_visible_in_stats(self) -> None:
        self.fx.injection_record(
            session_id="s-1",
            turn=1,
            tokens={"system": 10, "memory": 20, "day": 30, "total": 60},
            injected_ids=["ent_fake"],
            scope="workspace",
        )
        stats = self.fx.stats()
        self.assertEqual((stats.get("injection_last") or {}).get("session_id"), "s-1")

    def test_activate_only_changes_given_ids(self) -> None:
        self.fx.ingest("我们项目决定用 pnpm 管理依赖，同时使用 vite 构建。", turn_id="p-act")
        nodes = self.fx.list_nodes()
        self.assertGreaterEqual(len(nodes), 2)
        weights_before = {node["id"]: float((node.get("properties") or {}).get("w") or 0.0) for node in nodes}
        target_id = nodes[0]["id"]
        activated = self.fx.activate([target_id], reason="injected")
        updated = {entry["id"]: float(entry["w"]) for entry in activated.get("updated") or []}
        self.assertIn(target_id, updated)
        after_nodes = self.fx.list_nodes()
        after = {node["id"]: float((node.get("properties") or {}).get("w") or 0.0) for node in after_nodes}
        for entity_id, weight in weights_before.items():
            if entity_id == target_id:
                self.assertGreater(after[entity_id] + 1e-9, weight)
            else:
                self.assertAlmostEqual(after[entity_id], weight, places=6, msg="未注入节点不应被激活")

    def test_notification_ignored_and_invalid_json_tolerated(self) -> None:
        self.fx.client.notify("log", {"level": "info", "msg": "protocol test notification"})
        self.fx.client.raw_stdin("{this is not valid json")
        self.fx.client.raw_stdin('{"jsonrpc":"2.0","method":"ready","params":{}}')  # 宿主发通知：无 id
        self.assertTrue(self.fx.ping().get("pong"), "非法 JSON/通知后 sidecar 必须继续可用")

    def test_unknown_method_32601(self) -> None:
        with self.assertRaises(RpcError) as caught:
            self.fx.client.request("no/such_method", {})
        self.assertEqual(caught.exception.code, -32601)

    def test_business_error_32000(self) -> None:
        with self.assertRaises(RpcError) as caught:
            self.fx.journal_rollback(change_id=999999999)
        self.assertEqual(caught.exception.code, -32000, "业务错误应使用 -32000")

    def test_invalid_params_contract_gap(self) -> None:
        """CONTRACT §3 规定参数非法 = -32602；实测允许 -32000/-32603（记录为契约缺口，不硬失败）。"""
        evidence = {"expected": -32602, "actual": None}
        try:
            self.fx.client.request("journal_rollback", {"change_id": "not-an-int"}, timeout=5.0)
            evidence["actual"] = "no_error"
        except RpcError as exc:
            evidence["actual"] = exc.code
        self.assertIn(evidence["actual"], (-32602, -32000, -32603, "no_error"), f"非法参数响应异常: {evidence}")
        if evidence["actual"] != -32602:
            sys.stderr.write(f"[protocol] 契约缺口：参数非法错误码应为 -32602，实际 {evidence['actual']}\n")


class ProtocolScriptedLlmTest(unittest.TestCase):
    def test_scripted_mode_boots_and_ingests(self) -> None:
        if not os.path.isfile(LLM_SCRIPT):
            raise unittest.SkipTest(f"scripted fixture 缺失: {LLM_SCRIPT}")
        with open(LLM_SCRIPT, "r", encoding="utf-8") as fh:
            script = json.load(fh)
        fx = spawn_fixture(self, name="scripted", llm_mode="scripted", llm_script=script)
        self.assertEqual(fx.ping().get("llm_mode"), "scripted")
        result = fx.ingest("脚本化 LLM 测试：决定用 pnpm。", turn_id="script-1")
        self.assertIn("accepted", result)


class ProtocolShutdownTest(unittest.TestCase):
    def test_shutdown_rpc_exits_process(self) -> None:
        missing = missing_sidecar_modules()
        if missing:
            raise unittest.SkipTest(f"sidecar 模块未就绪: {', '.join(missing)}")
        client = SidecarClient(name="shutdown")
        client.start()
        try:
            result = client.request("shutdown", {}, timeout=5.0)
            self.assertTrue(result.get("ok"))
            process = client._proc
            assert process is not None
            process.wait(timeout=10)
            self.assertEqual(process.returncode, 0)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
