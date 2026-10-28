"""Sidecar → 宿主的 LLM 桥。

模式（config.llm.mode）：
- host（默认）：反向 JSON-RPC `host/llm` 调用 DSH 当前会话模型（零额外配置）；
- offline：确定性规则引擎（无网络/无模型环境、验收测试用）；
- scripted：从 GRAPH_MEMORY_LLM_SCRIPT 指向的 JSON 文件按 purpose 顺序返回脚本化响应（测试用）；
- openai：可选的 OpenAI-compatible HTTP 端点（config.llm.baseUrl + apiKeyEnv）。

任何失败都不允许向上抛（红线 R7 精神：记忆系统绝不阻断对话）：
host 失败 → 记 warning → 降级 offline 规则；offline 规则也失败 → 返回空串。
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.request
from typing import Any, Optional

from base import Config, LlmUnavailable, log


class LlmBridge:
    def __init__(self, config: Config, peer: Optional[Any] = None) -> None:
        self.config = config
        self.peer = peer
        self.mode = (config.get("llm.mode") or "host").lower()
        self.timeout_ms = int(config.get("llm.timeoutMs") or 60000)
        self._script: dict[str, list[Any]] = {}
        self._script_cursor: dict[str, int] = {}
        self._call_count = 0
        if self.mode == "scripted":
            self._load_script()

    # ------------------------------------------------------------------ #
    # 公开接口
    # ------------------------------------------------------------------ #
    async def complete(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        max_tokens: int = 1024,
        purpose: str = "",
    ) -> str:
        self._call_count += 1
        try:
            if self.mode == "scripted":
                return self._scripted(purpose)
            if self.mode == "offline":
                return self._offline(purpose, prompt)
            if self.mode == "openai":
                return await self._openai(prompt, system, max_tokens)
            return await self._host(prompt, system, max_tokens)
        except Exception as exc:  # noqa: BLE001 — 记忆系统绝不能因 LLM 故障崩溃
            log("warning", "llm call failed; fallback to offline rules", purpose=purpose, error=str(exc))
            try:
                return self._offline(purpose, prompt)
            except Exception:  # noqa: BLE001
                return ""

    # ------------------------------------------------------------------ #
    # host（DSH 当前会话模型）
    # ------------------------------------------------------------------ #
    async def _host(self, prompt: str, system: Optional[str], max_tokens: int) -> str:
        if self.peer is None:
            raise LlmUnavailable("no host peer attached", code="llm_no_peer")
        result = await self.peer.send_request(
            "host/llm",
            {
                "system": system or "",
                "prompt": prompt,
                "max_tokens": max_tokens,
                "purpose": "memory",
            },
            timeout=self.timeout_ms / 1000.0,
        )
        if not isinstance(result, dict) or "text" not in result:
            raise LlmUnavailable("host/llm returned invalid result", code="llm_bad_result")
        return str(result.get("text") or "")

    # ------------------------------------------------------------------ #
    # openai-compatible HTTP（可选）
    # ------------------------------------------------------------------ #
    async def _openai(self, prompt: str, system: Optional[str], max_tokens: int) -> str:
        base_url = (self.config.get("llm.baseUrl") or "").rstrip("/")
        if not base_url:
            raise LlmUnavailable("llm.baseUrl not configured", code="llm_not_configured")
        api_key = os.environ.get(self.config.get("llm.apiKeyEnv") or "GRAPH_MEMORY_LLM_API_KEY", "")
        model = self.config.get("llm.model") or "deepseek-chat"
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body = json.dumps(
            {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.1},
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            base_url + "/chat/completions",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            method="POST",
        )

        def _do() -> str:
            with urllib.request.urlopen(request, timeout=self.timeout_ms / 1000.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return payload["choices"][0]["message"]["content"]

        return await asyncio.get_running_loop().run_in_executor(None, _do)

    # ------------------------------------------------------------------ #
    # scripted（测试）
    # ------------------------------------------------------------------ #
    def _load_script(self) -> None:
        path = os.environ.get("GRAPH_MEMORY_LLM_SCRIPT", "")
        if not path or not os.path.isfile(path):
            log("warning", "scripted llm mode but script file missing", path=path)
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            responses = data.get("responses", data)
            if isinstance(responses, dict):
                for key, value in responses.items():
                    self._script[str(key)] = list(value) if isinstance(value, list) else [value]
        except (OSError, json.JSONDecodeError) as exc:
            log("warning", "failed to load llm script", error=str(exc))

    def _scripted(self, purpose: str) -> str:
        queue = self._script.get(purpose) or self._script.get("default") or []
        cursor = self._script_cursor.get(purpose, 0)
        if queue:
            value = queue[min(cursor, len(queue) - 1)]
            self._script_cursor[purpose] = cursor + 1
            return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return self._offline(purpose, "")

    # ------------------------------------------------------------------ #
    # offline 确定性规则（extract.offline_rules 实现；缺失时本模块兜底）
    # ------------------------------------------------------------------ #
    def _offline(self, purpose: str, prompt: str) -> str:
        try:
            from base import import_offline_rules  # noqa: PLC0415

            rules = import_offline_rules()
        except Exception:  # noqa: BLE001
            rules = _FallbackRules()
        if purpose.startswith("extract"):
            facts = rules.extract(prompt, "")
            return json.dumps(facts, ensure_ascii=False)
        if purpose.startswith("judge"):
            try:
                verdict = rules.judge("", "", prompt)
            except Exception:  # noqa: BLE001
                verdict = _FallbackRules().judge("", "", prompt)
            return json.dumps(verdict, ensure_ascii=False)
        if purpose.startswith("rewrite"):
            # 描述改写属于生成任务，没有确定性规则可仿写：返回空串，交给 service 的
            # 规则兜底（昵称/否定/替换）。绝不能落到 summarize —— 那会把提示词原文
            # 当成新描述写进图谱（提示词泄漏）。
            return ""
        return rules.summarize(prompt, 360)


class _FallbackRules:
    """extract.py 尚未可用时的最小确定性兜底（生产禁用）。"""

    def extract(self, text: str, scope: str) -> list[dict[str, Any]]:  # noqa: ARG002
        compact = " ".join((text or "").split())
        if len(compact) < 8:
            return []
        return [{"name": compact[:24], "type": "Concept", "description": compact[:200]}]

    def summarize(self, text: str, max_tokens: int) -> str:
        compact = " ".join((text or "").split())
        limit = max(32, int(max_tokens) * 2)
        return compact[:limit]

    def judge(self, before: str, after: str, context: str = "") -> dict[str, Any]:  # noqa: ARG002
        from base import has_negation_marker  # noqa: PLC0415

        return {
            "is_same_attribute": True,
            "is_contradiction": has_negation_marker(after),
            "confidence": 0.75,
        }
