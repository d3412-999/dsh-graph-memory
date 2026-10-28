"""配置加载与合并（默认 config/default.json + 覆盖 + 环境变量）。"""
from __future__ import annotations

import copy
import json
import os
from typing import Any, Optional

from base import Config

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_PATH = os.path.join(REPO_ROOT, "config", "default.json")


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_defaults() -> dict[str, Any]:
    try:
        # utf-8-sig 同时兼容无 BOM 与带 BOM 的 UTF-8（Windows 工具链容易写出 BOM）
        with open(DEFAULT_CONFIG_PATH, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def parse_override(raw: Optional[str]) -> dict[str, Any]:
    """--config 支持：内联 JSON 字符串 / JSON 文件路径。"""
    if not raw:
        return {}
    raw = raw.strip()
    if not raw:
        return {}
    if os.path.isfile(raw):
        try:
            with open(raw, "r", encoding="utf-8-sig") as fh:
                parsed = json.load(fh)
            return parsed if isinstance(parsed, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def build_config(
    *,
    user_home: Optional[str],
    workspace_home: Optional[str],
    workspace_root: str,
    raw_override: Optional[str] = None,
    panel_port: Optional[int] = None,
) -> Config:
    data = _deep_merge(load_defaults(), parse_override(raw_override))
    # 环境变量覆盖（便于 E2E/测试）
    env_paths = {
        "paths.userDataHome": os.environ.get("GRAPH_MEMORY_USER_HOME"),
        "paths.workspaceDataHome": os.environ.get("GRAPH_MEMORY_WORKSPACE_HOME"),
        "llm.mode": os.environ.get("GRAPH_MEMORY_LLM_MODE"),
        "graph.lightragUrl": os.environ.get("GRAPH_MEMORY_LIGHTRAG_URL"),
        "sidecar.pythonPath": os.environ.get("GRAPH_MEMORY_PYTHON"),
    }
    # 显式参数优先于环境变量
    if user_home:
        env_paths["paths.userDataHome"] = user_home
    if workspace_home:
        env_paths["paths.workspaceDataHome"] = workspace_home
    for path, value in env_paths.items():
        if value:
            parts = path.split(".")
            cursor = data
            for part in parts[:-1]:
                cursor = cursor.setdefault(part, {})
            cursor[parts[-1]] = value
    if panel_port:
        data.setdefault("panel", {})["port"] = int(panel_port)
    home = os.path.expanduser("~")
    paths = data.setdefault("paths", {})
    if not paths.get("userDataHome"):
        paths["userDataHome"] = os.path.join(home, ".dsh", "graph-memory")
    if not paths.get("workspaceDataHome"):
        paths["workspaceDataHome"] = os.path.join(workspace_root, ".dsh", "graph-memory")
    config = Config(data, base_dir=REPO_ROOT)
    return config


def resolve_noise_words_path(config: Config) -> str:
    override = config.get("noise.wordsPath")
    if override:
        return os.path.abspath(os.path.join(config.base_dir, override))
    return os.path.join(config.base_dir, "config", "noiseWords.yaml")
