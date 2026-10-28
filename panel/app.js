/* ============================================================================
 * dsh-graph-memory 本地面板前端（原生 JS，无依赖、无构建、无外部网络）
 *
 * 契约依据：docs/CONTRACT.md §2.1（7 天半衰期惰性衰减）、§2.2（恒定注入预算 3000）
 * HTTP API：sidecar/panel.py（127.0.0.1:9629，仅回环）
 *
 * 设计要点：
 *  - 所有请求经 requestJson()/guard() 封装：失败只返回 {ok:false,error}，绝不抛出未捕获异常；
 *  - 视图模型函数（budgetViewModel/journaRows/...）不依赖 DOM，便于用 mock JSON 自测；
 *  - 每 5 秒轮询；页面隐藏时暂停；过期响应（scope 切换）直接丢弃；
 *  - 面板只做只读展示 + 受控操作（pin/unpin/forget/set_weight/update）。
 * ========================================================================== */
(function () {
  'use strict';

  // ------------------------------------------------------------------ //
  // 常量（与 docs/CONTRACT.md §2.1 / §2.2 / §2.6 对齐）
  // ------------------------------------------------------------------ //
  var HALF_LIFE_SECONDS = 7 * 24 * 3600; // §2.1：7 天半衰期
  var SECONDS_PER_DAY = 24 * 3600;
  var POLL_MS = 5000;

  var TOTAL_HARD = 3000; // §2.2 R1
  var CAPS = {
    system: 128,  // SYSTEM_MAX
    memory: 2048, // MEM_MAX（动态上限 1024~2048）
    day: 512,     // DAY_TEXT_MAX
    detail: 312,  // DETAIL_MAX
    history: 200, // HISTORY_MAX
  };
  var SEGMENTS = [
    { key: 'system', label: 'system', color: '#6b8fbf' },
    { key: 'memory', label: 'memory', color: '#5c8a5a' },
    { key: 'day', label: 'day', color: '#c08a2e' },
    { key: 'detail', label: 'detail', color: '#8a7aa8' },
    { key: 'history', label: 'history', color: '#b06f83' },
  ];

  var COMPRESS_W = 0.6; // §2.6 压缩阈值
  var ARCHIVE_W = 0.3;  // §2.6 归档阈值

  var ROLLBACK_KINDS = ['supersede', 'update', 'weight', 'pin', 'forget', 'prune_archive'];
  var ROLLBACK_DAYS = 7;

  var GRAPH_NODE_LIMIT = 140;
  var GRAPH_WIDTH = 1000;
  var GRAPH_HEIGHT = 600;
  var LIST_LIMIT = 60;

  var GRAPH_NOTE_DEFAULT = '节点大小 ∝ 权重 w；颜色按 status；金色描边 = pinned；hover 看描述，点击查看详情。';

  // ------------------------------------------------------------------ //
  // 状态
  // ------------------------------------------------------------------ //
  var state = {
    scope: 'workspace',
    panel: null,
    panelError: null,
    injectionLast: null,
    injectionError: null,
    graph: null,
    graphError: null,
    graphDegraded: false,
    journal: [],
    journalError: null,
    journalFilter: '',
    prune: [],
    pruneError: null,
    search: { q: '', items: [], top1: null, error: null, loading: false },
    selectedId: null,
    pseudoNode: null,
    renderedDetailId: null,
    weightDirty: false,
    currentDetailVm: null,
    graphModel: null,
    graphSignature: '',
    layouts: { workspace: new Map(), user: new Map() },
    lastRefresh: null,
    lastErrors: [],
    refreshing: false,
    pendingRefresh: false,
    actionBusy: false,
    toastTimer: null,
    pollTimer: null,
  };

  var refreshSeq = 0;
  var els = {};

  // ------------------------------------------------------------------ //
  // 基础工具
  // ------------------------------------------------------------------ //
  function nowSec() {
    return Date.now() / 1000;
  }

  function pad2(value) {
    return value < 10 ? '0' + value : String(value);
  }

  function num(value, fallback) {
    var parsed = typeof value === 'number' ? value : parseFloat(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  function escapeHtml(value) {
    var text = value === undefined || value === null ? '' : String(value);
    return text
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function truncateText(value, max) {
    var text = String(value === undefined || value === null ? '' : value).replace(/\s+/g, ' ').trim();
    if (!text) return '';
    var limit = Math.max(0, num(max, 80));
    return text.length > limit ? text.slice(0, Math.max(0, limit - 1)) + '…' : text;
  }

  function safeJson(value) {
    if (value === undefined || value === null) return '';
    try {
      return JSON.stringify(value);
    } catch (error) {
      return String(value);
    }
  }

  function formatTs(ts) {
    var n = num(ts, null);
    if (n === null || n <= 0) return '—';
    var date = new Date(n * 1000);
    if (Number.isNaN(date.getTime())) return '—';
    return date.getFullYear() + '-' + pad2(date.getMonth() + 1) + '-' + pad2(date.getDate()) +
      ' ' + pad2(date.getHours()) + ':' + pad2(date.getMinutes()) + ':' + pad2(date.getSeconds());
  }

  function formatDate(ts) {
    var n = num(ts, null);
    if (n === null || n <= 0) return '—';
    var date = new Date(n * 1000);
    if (Number.isNaN(date.getTime())) return '—';
    return date.getFullYear() + '-' + pad2(date.getMonth() + 1) + '-' + pad2(date.getDate());
  }

  function fmtNumber(value) {
    var n = num(value, null);
    return n === null ? '—' : String(n);
  }

  function shortId(id) {
    var text = String(id || '');
    if (text.length <= 18) return text;
    return text.slice(0, 16) + '…';
  }

  function safeClass(value) {
    return String(value === undefined || value === null ? '' : value).replace(/[^a-zA-Z0-9_-]/g, '-');
  }

  function msgOf(error) {
    if (!error) return '未知错误';
    if (typeof error === 'string') return truncateText(error, 240);
    var text = error.message ? String(error.message) : String(error);
    return truncateText(text, 240);
  }

  function debounce(fn, wait) {
    var timer = null;
    return function debounced() {
      var args = arguments;
      var context = this;
      if (timer) clearTimeout(timer);
      timer = setTimeout(function () {
        timer = null;
        fn.apply(context, args);
      }, num(wait, 150));
    };
  }

  // ------------------------------------------------------------------ //
  // HTTP：统一封装，失败只返回错误对象（不抛未捕获异常）
  // ------------------------------------------------------------------ //
  function buildQuery(params) {
    if (!params) return '';
    var pairs = [];
    Object.keys(params).forEach(function (key) {
      var value = params[key];
      if (value === undefined || value === null || value === '') return;
      pairs.push(encodeURIComponent(key) + '=' + encodeURIComponent(String(value)));
    });
    return pairs.length ? '?' + pairs.join('&') : '';
  }

  async function requestJson(path, options) {
    if (typeof fetch !== 'function') {
      throw new Error('当前环境不支持 fetch，请通过 http://127.0.0.1:9629/ 打开面板');
    }
    var init = Object.assign({ cache: 'no-store', method: 'GET' }, options || {});
    init.headers = Object.assign({ Accept: 'application/json' }, (options && options.headers) || {});
    var response;
    try {
      response = await fetch(path, init);
    } catch (error) {
      throw new Error('无法连接面板服务（' + truncateText(msgOf(error), 120) + '），请确认 sidecar 正在 127.0.0.1:9629 运行');
    }
    var text = '';
    try {
      text = await response.text();
    } catch (error) {
      throw new Error('读取响应失败：' + msgOf(error));
    }
    var data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (error) {
        throw new Error('响应不是合法 JSON（HTTP ' + response.status + '）：' + truncateText(text, 120));
      }
    }
    if (!response.ok) {
      var httpMessage = (data && data.error) || ('HTTP ' + response.status + ' ' + (response.statusText || ''));
      throw new Error(String(httpMessage));
    }
    if (data && typeof data === 'object' && data.error) {
      throw new Error(String(data.error));
    }
    return data || {};
  }

  function apiGet(path, params) {
    return requestJson(path + buildQuery(params), { method: 'GET' });
  }

  function apiPost(path, payload) {
    return requestJson(path, {
      method: 'POST',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify(payload || {}),
    });
  }

  function guard(promise) {
    return promise.then(
      function (data) { return { ok: true, data: data }; },
      function (error) { return { ok: false, error: msgOf(error) }; }
    );
  }

  // ------------------------------------------------------------------ //
  // 视图模型（纯函数，不碰 DOM；可用 mock JSON 自测）
  // ------------------------------------------------------------------ //

  // §2.1：w(t) = min(10, w_last * 2^(-(t-last_activated_at)/604800))
  function decayValue(rawW, baseTs, atTs) {
    var w = clamp(num(rawW, 1), 0, 10);
    var base = num(baseTs, null);
    var now = num(atTs, nowSec());
    if (base === null || base <= 0) return w;
    var delta = Math.max(0, now - base);
    return clamp(Math.min(10, w * Math.pow(2, -delta / HALF_LIFE_SECONDS)), 0, 10);
  }

  function currentWeight(node, atTs) {
    var props = (node && node.properties) || {};
    var rawW = num(props.w, num(node && node.w, 1));
    var baseTs = num(props.last_activated_at, num(props.updated_at, num(props.created_at, atTs)));
    return decayValue(rawW, baseTs, atTs);
  }

  function decaySeries(rawW, baseTs, atTs, horizonDays, samples) {
    var now = num(atTs, nowSec());
    var horizon = Math.max(1, num(horizonDays, 30));
    var count = Math.max(2, Math.round(num(samples, 60)));
    var series = [];
    for (var i = 0; i <= count; i += 1) {
      var day = (horizon * i) / count;
      series.push({ day: day, w: decayValue(rawW, baseTs, now + day * SECONDS_PER_DAY) });
    }
    return series;
  }

  // 1) 硬预算仪表盘视图模型
  function budgetViewModel(injectionLast) {
    var tokens = (injectionLast && injectionLast.tokens) || {};
    var segments = SEGMENTS.map(function (segment) {
      return {
        key: segment.key,
        label: segment.label,
        color: segment.color,
        value: Math.max(0, Math.round(num(tokens[segment.key], 0))),
        cap: CAPS[segment.key],
      };
    });
    var sum = segments.reduce(function (acc, seg) { return acc + seg.value; }, 0);
    var declared = Math.max(0, Math.round(num(tokens.total, 0)));
    var total = Math.max(declared, sum);
    var injectedIds = injectionLast && Array.isArray(injectionLast.injected_ids) ? injectionLast.injected_ids : [];
    return {
      hasData: Boolean(injectionLast && (num(injectionLast.ts, 0) > 0 || total > 0)),
      ts: num(injectionLast && injectionLast.ts, null),
      turn: injectionLast && injectionLast.turn !== undefined && injectionLast.turn !== null ? injectionLast.turn : null,
      sessionId: injectionLast && injectionLast.session_id ? injectionLast.session_id : null,
      total: total,
      sum: sum,
      declared: declared,
      pct: Math.round((total / TOTAL_HARD) * 1000) / 10,
      overflow: total > TOTAL_HARD,
      injectedCount: injectedIds.length,
      segments: segments,
    };
  }

  // 4) 节点详情 + 权重曲线视图模型
  function detailViewModel(node, atTs) {
    if (!node) return null;
    var now = num(atTs, nowSec());
    var props = node.properties || {};
    var rawW = num(props.w, num(node.w, 1));
    var baseTs = num(props.last_activated_at, num(props.updated_at, num(props.created_at, now)));
    var wNow = decayValue(rawW, baseTs, now);
    var status = props.status || node.status || 'active';
    var pinned = props.pinned !== undefined ? Boolean(props.pinned) : Boolean(node.pinned);
    var meta = [];
    function add(label, value) {
      if (value === undefined || value === null || value === '') return;
      meta.push({ label: label, value: String(value) });
    }
    add('原始 w', rawW.toFixed(3));
    add('当前惰性 w', wNow.toFixed(3));
    add('last_activated_at', formatTs(props.last_activated_at));
    add('updated_at', formatTs(props.updated_at));
    add('created_at', formatTs(props.created_at));
    add('valid_from', props.valid_from || '—');
    add('protect_until', props.protect_until ? formatTs(props.protect_until) : '—');
    if (props.compressed_at) add('compressed_at', formatTs(props.compressed_at));
    if (props.archived_at) add('archived_at', formatTs(props.archived_at));
    if (props.workspace_hit !== undefined) add('workspace_hit', props.workspace_hit ? 'true' : 'false');
    if (Array.isArray(props.source_turn_ids)) add('source_turn_ids', props.source_turn_ids.length + ' 条');
    if (props.embed_version !== undefined && props.embed_version !== null) add('embed_version', String(props.embed_version));
    return {
      id: String(node.id || ''),
      name: node.name || String(node.id || '(未命名)'),
      type: node.type || '—',
      description: node.description || '',
      status: String(status),
      pinned: pinned,
      rawW: rawW,
      wNow: wNow,
      baseTs: baseTs,
      meta: meta,
    };
  }

  // 3) 记忆列表视图模型
  function memoryRowsFromGraph(graph, atTs) {
    var nodes = graph && Array.isArray(graph.nodes) ? graph.nodes : [];
    return nodes.map(function (node) {
      var props = node.properties || {};
      var updated = num(props.updated_at, num(props.created_at, null));
      return {
        id: String(node.id || ''),
        name: node.name || '',
        type: node.type || '',
        w: currentWeight(node, atTs),
        rawW: num(props.w, num(node.w, 1)),
        pinned: Boolean(props.pinned),
        status: String(props.status || 'active'),
        dateText: updated !== null ? formatDate(updated) : '—',
        text: node.description || '（无描述）',
        sim: null,
        source: 'graph',
      };
    }).sort(function (a, b) { return b.w - a.w; });
  }

  function memoryRowsFromSearch(items, atTs) {
    var list = Array.isArray(items) ? items : [];
    return list.map(function (item) {
      var updated = num(item.updated_at, num(item.updated, num(item.created_at, null)));
      return {
        id: String(item.id || ''),
        name: item.name || '',
        type: item.type || '',
        w: num(item.w, currentWeight(item, atTs)),
        rawW: num(item.w, 0),
        pinned: Boolean(item.pinned),
        status: String(item.status || 'active'),
        dateText: updated !== null ? formatDate(updated) : (item.valid_from || '—'),
        text: item.text || item.description || '（无描述）',
        sim: num(item.sim, null),
        source: 'search',
      };
    });
  }

  // 2) 图谱视图模型（按权重截断，控制渲染规模）
  function graphModel(graph, limit) {
    var allNodes = graph && Array.isArray(graph.nodes) ? graph.nodes : [];
    var allRelations = graph && Array.isArray(graph.relations) ? graph.relations : [];
    var cap = Math.max(1, Math.round(num(limit, GRAPH_NODE_LIMIT)));
    var snapshotNow = nowSec();
    var sorted = allNodes.slice().sort(function (a, b) {
      return currentWeight(b, snapshotNow) - currentWeight(a, snapshotNow);
    });
    var kept = sorted.slice(0, cap);
    var keptIds = {};
    kept.forEach(function (node) { if (node && node.id) keptIds[node.id] = true; });
    var edges = [];
    allRelations.forEach(function (relation) {
      if (!relation) return;
      var source = relation.source;
      var target = relation.target;
      if (!source || !target || source === target) return;
      if (!keptIds[source] || !keptIds[target]) return;
      edges.push({
        id: relation.id || (source + '->' + target),
        source: String(source),
        target: String(target),
        description: relation.description || '',
      });
    });
    var nodes = kept.map(function (node) {
      var props = node.properties || {};
      return {
        id: String(node.id || ''),
        name: node.name || String(node.id || ''),
        type: node.type || '',
        description: node.description || '',
        w: currentWeight(node, snapshotNow),
        rawW: num(props.w, 1),
        status: String(props.status || 'active'),
        pinned: Boolean(props.pinned),
      };
    });
    return {
      nodes: nodes,
      edges: edges,
      totalNodes: allNodes.length,
      totalEdges: allRelations.length,
      shownNodes: nodes.length,
      shownEdges: edges.length,
      truncated: allNodes.length > nodes.length,
    };
  }

  function makeRandom(seed) {
    var a = seed >>> 0;
    return function random() {
      a = (a + 0x6D2B79F5) >>> 0;
      var t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  // 简化力导向布局：斥力（全对）+ 边弹簧 + 向心引力；已有位置复用，避免轮询抖动
  function layoutGraph(model, previousPositions) {
    var nodes = model.nodes;
    var random = makeRandom(0x51ed270b);
    var positions = new Map();
    var previous = previousPositions instanceof Map ? previousPositions : null;
    nodes.forEach(function (node, index) {
      var old = previous ? previous.get(node.id) : null;
      if (old && Number.isFinite(old.x) && Number.isFinite(old.y)) {
        positions.set(node.id, { x: old.x, y: old.y, vx: 0, vy: 0 });
      } else {
        var angle = (index / Math.max(1, nodes.length)) * Math.PI * 2;
        var radius = Math.min(GRAPH_WIDTH, GRAPH_HEIGHT) * (0.2 + 0.2 * random());
        positions.set(node.id, {
          x: GRAPH_WIDTH / 2 + Math.cos(angle) * radius,
          y: GRAPH_HEIGHT / 2 + Math.sin(angle) * radius * 0.72,
          vx: 0,
          vy: 0,
        });
      }
    });
    var indexMap = new Map();
    nodes.forEach(function (node, index) { indexMap.set(node.id, index); });
    var edgePairs = model.edges.map(function (edge) {
      return [indexMap.get(edge.source), indexMap.get(edge.target)];
    }).filter(function (pair) {
      return pair[0] !== undefined && pair[1] !== undefined && pair[0] !== pair[1];
    });

    var settle = previous && previous.size ? 70 : 300;
    var repulsion = 3200;
    var springK = 0.012;
    var springLength = 92;
    var gravity = 0.0024;
    var damping = 0.82;
    var list = nodes.map(function (node) { return positions.get(node.id); });

    for (var step = 0; step < settle; step += 1) {
      var i;
      var j;
      for (i = 0; i < list.length; i += 1) {
        var a = list[i];
        for (j = i + 1; j < list.length; j += 1) {
          var b = list[j];
          var dx = a.x - b.x;
          var dy = a.y - b.y;
          var dist2 = dx * dx + dy * dy;
          if (dist2 < 1) {
            dx = random() - 0.5;
            dy = random() - 0.5;
            dist2 = dx * dx + dy * dy + 0.01;
          }
          var dist = Math.sqrt(dist2);
          var force = repulsion / dist2;
          var fx = (dx / dist) * force;
          var fy = (dy / dist) * force;
          a.vx += fx;
          a.vy += fy;
          b.vx -= fx;
          b.vy -= fy;
        }
      }
      for (i = 0; i < edgePairs.length; i += 1) {
        var pair = edgePairs[i];
        var pa = list[pair[0]];
        var pb = list[pair[1]];
        var ex = pb.x - pa.x;
        var ey = pb.y - pa.y;
        var edgeDist = Math.sqrt(ex * ex + ey * ey) || 0.01;
        var spring = (edgeDist - springLength) * springK;
        var sfx = (ex / edgeDist) * spring;
        var sfy = (ey / edgeDist) * spring;
        pa.vx += sfx;
        pa.vy += sfy;
        pb.vx -= sfx;
        pb.vy -= sfy;
      }
      for (i = 0; i < list.length; i += 1) {
        var point = list[i];
        point.vx += (GRAPH_WIDTH / 2 - point.x) * gravity;
        point.vy += (GRAPH_HEIGHT / 2 - point.y) * gravity;
        point.vx = clamp(point.vx * damping, -18, 18);
        point.vy = clamp(point.vy * damping, -18, 18);
        point.x = clamp(point.x + point.vx, 28, GRAPH_WIDTH - 28);
        point.y = clamp(point.y + point.vy, 28, GRAPH_HEIGHT - 28);
      }
    }

    var result = new Map();
    nodes.forEach(function (node) {
      var pos = positions.get(node.id);
      result.set(node.id, { x: pos.x, y: pos.y });
    });
    return result;
  }

  // 5) Journal 变更历史视图模型
  function normalizeState(value) {
    if (value === null || value === undefined) return { description: null, properties: {} };
    if (typeof value === 'string') return { description: value, properties: {} };
    if (typeof value === 'object') {
      if ('description' in value || 'properties' in value) {
        var properties = value.properties && typeof value.properties === 'object' ? value.properties : {};
        return {
          description: value.description === undefined || value.description === null ? null : String(value.description),
          properties: properties,
        };
      }
      return { description: null, properties: value };
    }
    return { description: String(value), properties: {} };
  }

  function sameValue(a, b) {
    if (a === b) return true;
    if (a && b && typeof a === 'object' && typeof b === 'object') {
      try {
        return JSON.stringify(a) === JSON.stringify(b);
      } catch (error) {
        return false;
      }
    }
    return false;
  }

  function formatChangeValue(value) {
    if (value === undefined) return '—';
    if (value === null) return 'null';
    if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(2);
    if (typeof value === 'boolean') return value ? 'true' : 'false';
    if (typeof value === 'object') return truncateText(safeJson(value), 40);
    return truncateText(String(value), 40);
  }

  function summarizeChange(before, after) {
    var b = normalizeState(before);
    var a = normalizeState(after);
    if (a.properties && a.properties.deleted === true) return '<span class="to">→ 删除 (deleted)</span>';
    var parts = [];
    if (a.description && a.description !== b.description) {
      if (b.description) {
        parts.push('<span class="from">' + escapeHtml(truncateText(b.description, 46)) + '</span>' +
          '<span class="arrow">→</span><span class="to">' + escapeHtml(truncateText(a.description, 46)) + '</span>');
      } else {
        parts.push('<span class="to">' + escapeHtml(truncateText(a.description, 52)) + '</span>');
      }
    }
    var keys = [];
    Object.keys(b.properties).forEach(function (key) { if (keys.indexOf(key) < 0) keys.push(key); });
    Object.keys(a.properties).forEach(function (key) { if (keys.indexOf(key) < 0) keys.push(key); });
    var shown = 0;
    var skipped = 0;
    keys.forEach(function (key) {
      if (key === 'deleted') return;
      if (sameValue(b.properties[key], a.properties[key])) return;
      if (shown >= 4) {
        skipped += 1;
        return;
      }
      shown += 1;
      parts.push('<span class="prop">' + escapeHtml(key) + ' ' + escapeHtml(formatChangeValue(b.properties[key])) +
        '→' + escapeHtml(formatChangeValue(a.properties[key])) + '</span>');
    });
    if (skipped > 0) parts.push('<span class="muted">…+' + skipped + '</span>');
    if (!parts.length) return '<span class="muted">无可见变化</span>';
    return parts.join(' ');
  }

  function journalRows(entries, atTs) {
    var now = num(atTs, nowSec());
    return (Array.isArray(entries) ? entries : []).map(function (entry) {
      var ts = num(entry.ts, null);
      var kind = String(entry.kind || '?');
      var rollbackText = '—';
      var rollbackClass = 'rollback-muted';
      if (entry.rolled_back) {
        rollbackText = '已回滚';
        rollbackClass = 'rollback-ok';
      } else if (ROLLBACK_KINDS.indexOf(kind) < 0) {
        rollbackText = '不可回滚';
      } else if (ts !== null && now - ts > ROLLBACK_DAYS * SECONDS_PER_DAY) {
        rollbackText = '已过 7 天';
      } else {
        rollbackText = '可回滚';
        rollbackClass = 'rollback-info';
      }
      return {
        id: entry.id,
        ts: ts,
        timeText: formatTs(ts),
        kind: kind,
        target: entry.target_id ? String(entry.target_id) : '—',
        changeHtml: summarizeChange(entry.before, entry.after),
        confidence: num(entry.confidence, null),
        rollbackText: rollbackText,
        rollbackClass: rollbackClass,
      };
    });
  }

  // 6) 剪枝日志视图模型（兼容 compress / prune / dry-run 三种 result 形态）
  function pruneRows(history) {
    return (Array.isArray(history) ? history : []).map(function (item) {
      var result = item && item.result && typeof item.result === 'object' ? item.result : {};
      var kind = String((item && item.kind) || '?');
      var round = result.round !== undefined && result.round !== null
        ? result.round
        : (result.round_no !== undefined ? result.round_no : '—');
      var summary;
      if (Array.isArray(result.compressed)) {
        var compressed = result.compressed.length;
        var merged = Array.isArray(result.merged) ? result.merged.length : 0;
        var candidates = result.candidates !== undefined ? result.candidates : '—';
        var protectedCount = 0;
        if (result.protected && typeof result.protected === 'object') {
          Object.keys(result.protected).forEach(function (key) {
            protectedCount += num(result.protected[key], 0);
          });
        }
        summary = '压缩 ' + compressed + ' 个 · 合并 ' + merged + ' 组 · 候选 ' + candidates +
          (protectedCount ? ' · 受保护 ' + protectedCount : '');
      } else if (result.dry_run !== undefined || result.archived !== undefined || result.deleted !== undefined) {
        var dryRun = result.dry_run === true ? '[dry-run] ' : '';
        var text = dryRun + '归档 ' + num(result.archived, 0) + ' / 删除 ' + num(result.deleted, 0);
        if (result.would_archive !== undefined) {
          text += ' · 预估归档 ' + num(result.would_archive, 0) + ' / 删除 ' + num(result.would_delete, 0);
        }
        if (result.reason) text += ' · ' + result.reason;
        if (result.count_before !== undefined && result.count_after !== undefined) {
          text += ' · 节点 ' + result.count_before + '→' + result.count_after;
        }
        summary = text;
      } else {
        var serialized = safeJson(result);
        summary = truncateText(serialized === '{}' ? '（无 report 字段）' : serialized, 180);
      }
      return {
        id: item && item.id,
        ts: item ? num(item.ts, null) : null,
        timeText: formatTs(item && item.ts),
        kind: kind,
        round: round,
        summary: summary,
        rawTitle: truncateText(safeJson(result), 400),
      };
    });
  }

  // ------------------------------------------------------------------ //
  // DOM 工具
  // ------------------------------------------------------------------ //
  function cacheElements() {
    var ids = [
      'conn-chip', 'refresh-note',
      'budget-total', 'budget-cap', 'budget-pct', 'budget-meta', 'budget-bar', 'budget-legend',
      'scope-stats', 'budget-error',
      'graph-svg', 'graph-wrap', 'graph-tooltip', 'graph-empty', 'graph-counts', 'graph-note',
      'detail-body', 'detail-clear', 'curve-block', 'curve-current', 'weight-canvas',
      'memory-count', 'search-form', 'search-input', 'search-btn', 'search-clear', 'search-meta',
      'memory-list', 'memory-error',
      'journal-count', 'journal-filter-form', 'journal-filter', 'journal-filter-clear',
      'journal-body', 'journal-error',
      'prune-count', 'prune-body', 'prune-error',
      'dead-count', 'dead-list', 'alarm-list', 'dead-error',
      'status-list', 'status-errors',
      'footer-scope', 'toast',
    ];
    ids.forEach(function (id) { els[id] = document.getElementById(id); });
  }

  function showDegraded(el, text) {
    if (!el) return;
    el.textContent = text;
    el.hidden = false;
  }

  function hideDegraded(el) {
    if (!el) return;
    el.hidden = true;
    el.textContent = '';
  }

  function toast(message, type, duration) {
    var el = els.toast;
    if (!el) return;
    el.textContent = message;
    el.className = 'toast' + (type ? ' is-' + type : '');
    el.hidden = false;
    if (state.toastTimer) clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(function () {
      el.hidden = true;
      state.toastTimer = null;
    }, num(duration, type === 'err' ? 7000 : 4200));
  }

  function getById(id) {
    return document.getElementById(id);
  }

  // ------------------------------------------------------------------ //
  // 刷新管线
  // ------------------------------------------------------------------ //
  async function refreshAll(reason) {
    if (state.refreshing) {
      // 已有刷新在途：记一个待刷新，等本轮结束后补一次（避免操作后 UI 等 5s 轮询）
      state.pendingRefresh = true;
      return;
    }
    state.refreshing = true;
    var seq = ++refreshSeq;
    var scope = state.scope;
    var filter = state.journalFilter;
    var searchQuery = state.search.q;
    try {
      var results = await Promise.all([
        guard(apiGet('/api/panel_state')),
        guard(apiGet('/api/injection')),
        guard(apiGet('/api/graph', { scope: scope })),
        guard(apiGet('/api/journal', { scope: scope, target_id: filter || undefined })),
        guard(apiGet('/api/prune', { scope: scope })),
        searchQuery ? guard(apiGet('/api/search', { q: searchQuery, scope: scope })) : Promise.resolve({ ok: true, data: null }),
      ]);
      // scope 已切换 / 有更新的刷新：丢弃本轮结果
      if (seq !== refreshSeq || state.scope !== scope) return;

      var panelRes = results[0];
      var injectionRes = results[1];
      var graphRes = results[2];
      var journalRes = results[3];
      var pruneRes = results[4];
      var searchRes = results[5];
      var errors = [];

      // ---- panel_state（统计 / 死信 / 告警 / nodes 降级源）----
      if (panelRes.ok) {
        state.panel = panelRes.data;
        state.panelError = null;
      } else {
        state.panelError = panelRes.error;
        errors.push('panel_state：' + panelRes.error);
      }

      // ---- 注入记录（预算仪表盘）----
      if (injectionRes.ok) {
        state.injectionError = null;
        state.injectionLast = injectionRes.data ? injectionRes.data.injection_last : null;
        if (!state.injectionLast && state.panel) state.injectionLast = state.panel.injection_last;
      } else {
        state.injectionError = injectionRes.error;
        errors.push('injection：' + injectionRes.error);
        state.injectionLast = state.panel ? state.panel.injection_last : state.injectionLast;
      }

      // ---- 图谱（失败时降级到 panel_state.nodes，丢弃关系边）----
      if (graphRes.ok) {
        state.graph = graphRes.data;
        state.graphError = null;
        state.graphDegraded = false;
      } else {
        state.graphError = graphRes.error;
        state.graphDegraded = true;
        errors.push('graph：' + graphRes.error);
        if (!state.graph && state.panel && state.panel.nodes && Array.isArray(state.panel.nodes[scope])) {
          state.graph = { scope: scope, nodes: state.panel.nodes[scope], relations: [], counts: null };
        }
      }

      // ---- Journal ----
      if (journalRes.ok) {
        state.journal = Array.isArray(journalRes.data.entries) ? journalRes.data.entries : [];
        state.journalError = null;
      } else {
        state.journalError = journalRes.error;
        errors.push('journal：' + journalRes.error);
        if (!state.journal.length && !filter && state.panel && Array.isArray(state.panel.journal_recent)) {
          state.journal = state.panel.journal_recent;
        }
      }

      // ---- 剪枝日志 ----
      if (pruneRes.ok) {
        state.prune = Array.isArray(pruneRes.data.history) ? pruneRes.data.history : [];
        state.pruneError = null;
      } else {
        state.pruneError = pruneRes.error;
        errors.push('prune：' + pruneRes.error);
        if (!state.prune.length && state.panel && Array.isArray(state.panel.prune_recent)) {
          state.prune = state.panel.prune_recent;
        }
      }

      // ---- 语义搜索（仅在已有查询时轮询）----
      if (searchQuery) {
        if (searchRes && searchRes.ok && searchRes.data) {
          state.search.items = Array.isArray(searchRes.data.items) ? searchRes.data.items : [];
          state.search.top1 = num(searchRes.data.top1_sim, null);
          state.search.error = null;
        } else if (searchRes) {
          state.search.error = searchRes.error;
          errors.push('search：' + searchRes.error);
        }
      }

      state.lastErrors = errors;
      state.lastRefresh = nowSec();

      safeRender('budget', renderDashboard);
      safeRender('scope-stats', function () { renderScopeStats(state.panel); });
      safeRender('graph', renderGraph);
      safeRender('detail', renderDetail);
      safeRender('memory', renderMemoryList);
      safeRender('journal', renderJournal);
      safeRender('prune', renderPrune);
      safeRender('dead-letters', function () { renderDeadLetters(state.panel); });
      safeRender('status', function () { renderStatus(state.panel); });
      renderConn();
    } catch (error) {
      // 理论上不会到这里（requestJson 已捕获），兜底保证轮询不中断
      console.error('[panel] refreshAll 未预期错误', error);
      state.lastErrors = ['刷新异常：' + msgOf(error)];
      renderConn();
    } finally {
      state.refreshing = false;
      if (state.pendingRefresh) {
        state.pendingRefresh = false;
        refreshAll('pending');
      }
    }
  }

  function safeRender(label, fn) {
    try {
      fn();
    } catch (error) {
      console.error('[panel] 渲染 ' + label + ' 失败', error);
      state.lastErrors.push('渲染 ' + label + ' 降级：' + msgOf(error));
      toast('渲染降级：' + label + ' 区块出错（' + msgOf(error) + '）', 'warn');
    }
  }

  function renderConn() {
    var chip = els['conn-chip'];
    var errors = state.lastErrors || [];
    var hardFail = Boolean(state.panelError) && Boolean(state.graphError) &&
      Boolean(state.journalError) && Boolean(state.pruneError);
    if (chip) {
      if (!errors.length) {
        chip.textContent = '已连接';
        chip.className = 'chip chip-ok';
      } else if (hardFail) {
        chip.textContent = '连接失败（降级显示）';
        chip.className = 'chip chip-err';
      } else {
        chip.textContent = '部分失败（' + errors.length + ' 项降级）';
        chip.className = 'chip chip-warn';
      }
      chip.title = errors.length ? errors.join('\n') : '面板 API 正常（仅回环 127.0.0.1）';
    }
    var note = els['refresh-note'];
    if (note) {
      var hidden = typeof document !== 'undefined' && document.hidden;
      note.textContent = '上次刷新 ' + formatTs(state.lastRefresh) + ' · 每 ' + (POLL_MS / 1000) + ' 秒自动刷新' +
        (hidden ? '（页面隐藏，暂停）' : '');
    }
  }

  // ------------------------------------------------------------------ //
  // 渲染：1) 硬预算仪表盘
  // ------------------------------------------------------------------ //
  function renderDashboard() {
    var vm = budgetViewModel(state.injectionLast);
    if (els['budget-total']) els['budget-total'].textContent = String(vm.total);
    if (els['budget-cap']) els['budget-cap'].textContent = String(TOTAL_HARD);
    if (els['budget-pct']) {
      els['budget-pct'].textContent = vm.pct + '%';
      els['budget-pct'].className = 'budget-pct' + (vm.overflow ? ' is-over' : '');
    }
    if (els['budget-meta']) {
      if (vm.hasData) {
        var turnText = vm.turn !== null ? 'turn ' + vm.turn : 'turn —';
        els['budget-meta'].textContent = '最近注入：' + turnText + ' · ' + formatTs(vm.ts) + ' · 注入条目 ' + vm.injectedCount + ' 条';
      } else if (state.injectionError && state.panelError) {
        els['budget-meta'].textContent = '尚无注入记录（获取失败：' + state.injectionError + '）';
      } else {
        els['budget-meta'].textContent = '尚无注入记录（等待首个 turn，仪表盘显示 0）';
      }
    }
    if (els['budget-bar']) {
      els['budget-bar'].innerHTML = vm.segments.map(function (seg) {
        var width = (seg.value / TOTAL_HARD) * 100;
        return '<span class="seg" style="width:' + width.toFixed(3) + '%;background:' + seg.color +
          '" title="' + escapeHtml(seg.label + ' ' + seg.value + ' / 上限 ' + seg.cap + ' tokens') + '"></span>';
      }).join('');
      els['budget-bar'].className = 'stack-bar' + (vm.total === 0 ? ' is-empty' : '');
    }
    if (els['budget-legend']) {
      els['budget-legend'].innerHTML = vm.segments.map(function (seg) {
        var ratio = seg.cap > 0 ? Math.min(100, (seg.value / seg.cap) * 100) : 0;
        return '<li><span class="dot" style="background:' + seg.color + '"></span>' +
          '<span class="legend-label">' + seg.label + '</span>' +
          '<span class="legend-bar"><i style="width:' + ratio.toFixed(1) + '%;background:' + seg.color + '"></i></span>' +
          '<span class="legend-value">' + seg.value + '<span class="cap">/' + seg.cap + '</span></span></li>';
      }).join('');
    }
    if (state.injectionError) {
      showDegraded(els['budget-error'], '注入统计获取失败，仪表盘已降级（' + state.injectionError + '）');
    } else {
      hideDegraded(els['budget-error']);
    }
  }

  function renderScopeStats(panel) {
    var el = els['scope-stats'];
    if (!el) return;
    var scopes = panel && panel.stats && panel.stats.scopes ? panel.stats.scopes : null;
    if (!scopes) {
      el.innerHTML = '<div class="empty-line">暂无统计（/api/panel_state 不可用或降级）</div>';
      return;
    }
    var order = ['workspace', 'user'];
    var html = order.filter(function (scope) { return scopes[scope]; }).map(function (scope) {
      var item = scopes[scope] || {};
      var watermarks = item.watermarks || {};
      var tag = scope === 'workspace' ? '项目知识' : '用户偏好';
      function cell(label, value) {
        return '<span>' + label + ' <b>' + escapeHtml(value) + '</b></span>';
      }
      return '<div class="scope-stat">' +
        '<h3>' + escapeHtml(scope) + ' <span class="scope-tag">' + tag + '</span></h3>' +
        '<div class="stat-grid">' +
        cell('节点', fmtNumber(item.nodes)) +
        cell('活跃', fmtNumber(item.nodes_live)) +
        cell('关系', fmtNumber(item.edges)) +
        cell('chunks', fmtNumber(item.chunks)) +
        cell('水位 low/high/hard', fmtNumber(watermarks.low) + '/' + fmtNumber(watermarks.high) + '/' + fmtNumber(watermarks.hard)) +
        cell('phase', item.phase === undefined || item.phase === null ? '—' : String(item.phase)) +
        cell('round', fmtNumber(item.round)) +
        cell('day_tokens', fmtNumber(item.day_tokens)) +
        cell('history_tokens', fmtNumber(item.history_tokens)) +
        '</div>' +
        '<div class="data-home" title="' + escapeHtml(item.data_home || '') + '">data_home: ' + escapeHtml(item.data_home || '—') + '</div>' +
        '</div>';
    }).join('');
    el.innerHTML = html || '<div class="empty-line">暂无统计</div>';
  }

  // ------------------------------------------------------------------ //
  // 渲染：2) 图谱（SVG）
  // ------------------------------------------------------------------ //
  function renderGraph() {
    var svg = els['graph-svg'];
    if (!svg) return;
    var empty = els['graph-empty'];
    var graph = state.graph;
    if (!graph) {
      if (state.graphError) {
        showGraphEmpty('图谱数据获取失败：' + state.graphError + '（下次轮询重试）', true);
      } else {
        showGraphEmpty('正在加载图谱…', false);
      }
      if (els['graph-counts']) els['graph-counts'].textContent = 'scope=' + state.scope;
      return;
    }
    var model = graphModel(graph, GRAPH_NODE_LIMIT);
    state.graphModel = model;
    var signature = model.nodes.map(function (node) { return node.id; }).sort().join(',') +
      '#' + model.edges.map(function (edge) { return edge.id; }).sort().join(',');
    var previous = state.layouts[state.scope] || new Map();
    var positions;
    if (signature === state.graphSignature && previous.size === model.nodes.length) {
      positions = previous;
    } else {
      positions = layoutGraph(model, previous);
      state.graphSignature = signature;
    }
    state.layouts[state.scope] = positions;
    drawGraph(svg, model, positions, state.selectedId);

    if (els['graph-counts']) {
      var counts = 'scope=' + state.scope + ' · 节点 ' + model.shownNodes + '/' + model.totalNodes +
        ' · 关系 ' + model.shownEdges + '/' + model.totalEdges;
      if (model.truncated) counts += ' · 已按 w 截断';
      if (state.graphDegraded) counts += ' · 降级（仅节点）';
      els['graph-counts'].textContent = counts;
    }
    if (els['graph-note']) {
      els['graph-note'].textContent = state.graphDegraded
        ? '图谱接口降级：仅显示节点（无关系边）— ' + (state.graphError || '')
        : GRAPH_NOTE_DEFAULT;
    }
    if (!model.nodes.length) {
      showGraphEmpty(state.graphError ? ('图谱数据获取失败：' + state.graphError) : '当前 scope 暂无实体（等待注入）', Boolean(state.graphError));
    } else {
      if (empty) empty.hidden = true;
    }
  }

  function showGraphEmpty(text, isError) {
    var empty = els['graph-empty'];
    if (!empty) return;
    empty.textContent = text;
    empty.className = 'empty-overlay' + (isError ? ' is-error' : '');
    empty.hidden = false;
  }

  function drawGraph(svg, model, positions, selectedId) {
    var statusColors = {
      active: '#a6723d',
      compressed: '#8a7aa8',
      archived: '#8a7f6b',
      pending_forget: '#b4574d',
    };
    var edgeHtml = model.edges.map(function (edge) {
      var a = positions.get(edge.source);
      var b = positions.get(edge.target);
      if (!a || !b) return '';
      var active = selectedId && (edge.source === selectedId || edge.target === selectedId);
      return '<line class="edge' + (active ? ' edge-active' : '') + '" x1="' + a.x.toFixed(1) + '" y1="' + a.y.toFixed(1) +
        '" x2="' + b.x.toFixed(1) + '" y2="' + b.y.toFixed(1) + '"></line>';
    }).join('');
    var showLabels = model.nodes.length <= 26;
    var nodeHtml = model.nodes.map(function (node) {
      var pos = positions.get(node.id);
      if (!pos) return '';
      var radius = 5 + Math.min(10, Math.max(0, node.w)) * 1.45 + (node.pinned ? 1.5 : 0);
      var fill = node.pinned ? '#c08a2e' : (statusColors[node.status] || '#8a7f6b');
      var opacity = node.status === 'archived' ? 0.45 : 1;
      var selected = node.id === selectedId;
      var label = showLabels || selected ? truncateText(node.name || node.id, 12) : '';
      return '<g class="gnode' + (selected ? ' is-selected' : '') + '" data-node-id="' + escapeHtml(node.id) + '" style="opacity:' + opacity + '">' +
        '<circle cx="' + pos.x.toFixed(1) + '" cy="' + pos.y.toFixed(1) + '" r="' + radius.toFixed(1) +
        '" fill="' + fill + '" stroke="' + (node.pinned ? 'rgba(192,138,46,0.72)' : 'rgba(58,50,38,0.5)') +
        '" stroke-width="' + (node.pinned ? 2.2 : 1.2) + '">' +
        '<title>' + escapeHtml((node.name || node.id) + ' · w=' + node.w.toFixed(2) + ' · ' + node.status + (node.pinned ? ' · pinned' : '')) + '</title>' +
        '</circle>' +
        (label ? '<text x="' + pos.x.toFixed(1) + '" y="' + (pos.y + radius + 12).toFixed(1) + '" text-anchor="middle">' + escapeHtml(label) + '</text>' : '') +
        '</g>';
    }).join('');
    svg.innerHTML = '<g class="edges">' + edgeHtml + '</g><g class="nodes">' + nodeHtml + '</g>';
  }

  function findGraphNode(id) {
    if (!id) return null;
    var model = state.graphModel;
    if (model) {
      for (var i = 0; i < model.nodes.length; i += 1) {
        if (model.nodes[i].id === id) return model.nodes[i];
      }
    }
    var graph = state.graph;
    if (graph && Array.isArray(graph.nodes)) {
      for (var j = 0; j < graph.nodes.length; j += 1) {
        if (graph.nodes[j].id === id) {
          return {
            id: id,
            name: graph.nodes[j].name || id,
            type: graph.nodes[j].type || '',
            description: graph.nodes[j].description || '',
            w: currentWeight(graph.nodes[j], nowSec()),
            status: String((graph.nodes[j].properties || {}).status || 'active'),
            pinned: Boolean((graph.nodes[j].properties || {}).pinned),
          };
        }
      }
    }
    return null;
  }

  function showGraphTooltip(node, event) {
    var wrap = els['graph-wrap'];
    var tooltip = els['graph-tooltip'];
    if (!wrap || !tooltip) return;
    tooltip.innerHTML = '<div class="tt-title">' + escapeHtml(node.name || node.id) + '</div>' +
      '<div class="tt-meta">' + escapeHtml(node.type || '—') + ' · w=' + node.w.toFixed(2) + ' · ' +
      escapeHtml(node.status || 'active') + (node.pinned ? ' · pinned' : '') + '</div>' +
      '<div class="tt-desc">' + escapeHtml(truncateText(node.description || '（无描述）', 220)) + '</div>';
    tooltip.hidden = false;
    var wrapRect = wrap.getBoundingClientRect();
    var tooltipRect = tooltip.getBoundingClientRect();
    var left = event.clientX - wrapRect.left + 14;
    var top = event.clientY - wrapRect.top + 14;
    if (left + tooltipRect.width > wrapRect.width - 8) {
      left = Math.max(8, event.clientX - wrapRect.left - tooltipRect.width - 14);
    }
    if (top + tooltipRect.height > wrapRect.height - 8) {
      top = Math.max(8, event.clientY - wrapRect.top - tooltipRect.height - 14);
    }
    tooltip.style.left = left + 'px';
    tooltip.style.top = top + 'px';
  }

  function hideGraphTooltip() {
    var tooltip = els['graph-tooltip'];
    if (!tooltip) return;
    tooltip.hidden = true;
    tooltip.innerHTML = '';
  }

  // ------------------------------------------------------------------ //
  // 渲染：4) 节点详情 + 权重曲线
  // ------------------------------------------------------------------ //
  function findNodeById(id) {
    if (!id) return null;
    var graph = state.graph;
    if (graph && Array.isArray(graph.nodes)) {
      for (var i = 0; i < graph.nodes.length; i += 1) {
        if (graph.nodes[i].id === id) return graph.nodes[i];
      }
    }
    var panel = state.panel;
    if (panel && panel.nodes && Array.isArray(panel.nodes[state.scope])) {
      var list = panel.nodes[state.scope];
      for (var j = 0; j < list.length; j += 1) {
        if (list[j].id === id) return list[j];
      }
    }
    return null;
  }

  function resolveSelectedNode() {
    if (!state.selectedId) return null;
    var node = findNodeById(state.selectedId);
    if (node) return node;
    if (state.pseudoNode && state.pseudoNode.id === state.selectedId) return state.pseudoNode;
    return null;
  }

  function selectNode(id) {
    if (!id) return;
    var nodeId = String(id);
    var node = findNodeById(nodeId);
    var searchItem = null;
    if (!node) {
      state.search.items.forEach(function (item) {
        if (!searchItem && item && item.id === nodeId) searchItem = item;
      });
      if (searchItem) {
        state.pseudoNode = {
          id: nodeId,
          name: searchItem.name || nodeId,
          type: searchItem.type || '—',
          description: searchItem.text || '',
          properties: {
            w: num(searchItem.w, 1),
            pinned: Boolean(searchItem.pinned),
            status: searchItem.status || 'active',
            updated_at: num(searchItem.updated_at, num(searchItem.updated, null)),
            last_activated_at: num(searchItem.updated_at, num(searchItem.updated, null)),
          },
        };
      } else {
        // 节点不在当前 scope 图谱缓存中（已删除 / 另一个 scope）：清空选择并提示，不留错配详情
        state.selectedId = null;
        state.pseudoNode = null;
        state.renderedDetailId = null;
        renderDetail();
        renderGraph();
        renderMemoryList();
        if (els['detail-clear']) els['detail-clear'].hidden = true;
        toast('节点 ' + shortId(nodeId) + ' 不在当前 scope 图谱中（可能已删除或属于另一个 scope）', 'warn', 6000);
        return;
      }
    } else {
      state.pseudoNode = null;
    }
    state.selectedId = nodeId;
    state.renderedDetailId = null;
    renderDetail();
    renderGraph();
    renderMemoryList();
    if (els['detail-clear']) els['detail-clear'].hidden = false;
  }

  function clearSelection() {
    state.selectedId = null;
    state.pseudoNode = null;
    state.renderedDetailId = null;
    state.currentDetailVm = null;
    renderDetail();
    renderGraph();
    renderMemoryList();
    if (els['detail-clear']) els['detail-clear'].hidden = true;
    if (els['curve-block']) els['curve-block'].hidden = true;
  }

  function renderDetail() {
    var body = els['detail-body'];
    if (!body) return;
    var node = resolveSelectedNode();
    if (!node) {
      state.renderedDetailId = null;
      state.currentDetailVm = null;
      body.innerHTML = '<p class="placeholder">在左侧图谱或记忆列表中点击一个节点。</p>';
      if (els['curve-block']) els['curve-block'].hidden = true;
      if (els['detail-clear']) els['detail-clear'].hidden = true;
      return;
    }
    var vm = detailViewModel(node, nowSec());
    if (!vm) return;
    state.currentDetailVm = vm;
    if (state.renderedDetailId !== vm.id) {
      body.innerHTML = buildDetailHtml(vm);
      state.renderedDetailId = vm.id;
      state.weightDirty = false;
      bindDetailControls(vm);
    }
    updateDetailDynamic(vm);
    if (els['detail-clear']) els['detail-clear'].hidden = false;
    if (els['curve-block']) els['curve-block'].hidden = false;
    drawWeightCurve(vm);
  }

  function buildDetailHtml(vm) {
    var statusBadge = '<span class="badge badge-' + escapeHtml(safeClass(vm.status)) + '">' + escapeHtml(vm.status) + '</span>';
    var pinBadge = vm.pinned ? '<span class="badge badge-pin">pinned</span>' : '';
    var typeBadge = '<span class="badge">' + escapeHtml(vm.type) + '</span>';
    var metaHtml = vm.meta.map(function (item) {
      return '<dt>' + escapeHtml(item.label) + '</dt><dd data-dv="' + escapeHtml(item.label) + '">' + escapeHtml(item.value) + '</dd>';
    }).join('');
    return '<div data-detail-id="' + escapeHtml(vm.id) + '">' +
      '<div class="detail-title">' + statusBadge + pinBadge + typeBadge + '</div>' +
      '<h3 class="detail-name">' + escapeHtml(vm.name) + '</h3>' +
      '<div class="detail-id">' + escapeHtml(vm.id) + ' <span class="muted">· scope ' + escapeHtml(state.scope) + '</span></div>' +
      '<p class="detail-desc' + (vm.description ? '' : ' is-empty') + '">' + escapeHtml(vm.description || '（无描述）') + '</p>' +
      '<dl class="kv detail-meta">' + metaHtml + '</dl>' +
      '<div class="actions">' +
      '<button type="button" class="btn' + (vm.pinned ? ' is-pinned' : '') + '" data-action="' + (vm.pinned ? 'unpin' : 'pin') + '">' +
      (vm.pinned ? 'unpin 取消 pin' : 'pin 固定') + '</button>' +
      '<button type="button" class="btn danger" data-action="forget">forget 遗忘</button>' +
      '<button type="button" class="btn ghost" data-action="filter-journal">只看该节点变更</button>' +
      '</div>' +
      '<div class="weight-editor">' +
      '<h3>手动调权重 <span class="en">set_weight</span></h3>' +
      '<div class="weight-row">' +
      '<label>w <input type="number" id="weight-input" min="0" max="10" step="0.1" value="' + vm.rawW.toFixed(1) + '"></label>' +
      '<input type="range" id="weight-range" min="0" max="10" step="0.1" value="' + vm.rawW.toFixed(1) + '">' +
      '<button type="button" class="btn primary" id="weight-apply">提交</button>' +
      '</div>' +
      '</div>' +
      '<details class="update-box"><summary>改口 update（语义替换）</summary>' +
      '<div class="update-form">' +
      '<input type="search" id="update-query" placeholder="旧说法 / 查询（如：用 npm 管理依赖）">' +
      '<input type="search" id="update-value" placeholder="新说法（如：改用 pnpm 管理依赖）">' +
      '<button type="button" class="btn primary" id="update-apply">提交 update</button>' +
      '</div></details>' +
      '</div>';
  }

  function updateDetailDynamic(vm) {
    var body = els['detail-body'];
    if (!body) return;
    var nameEl = body.querySelector('.detail-name');
    if (nameEl) nameEl.textContent = vm.name;
    var descEl = body.querySelector('.detail-desc');
    if (descEl) {
      descEl.textContent = vm.description || '（无描述）';
      descEl.className = 'detail-desc' + (vm.description ? '' : ' is-empty');
    }
    var titleEl = body.querySelector('.detail-title');
    if (titleEl) {
      titleEl.innerHTML = '<span class="badge badge-' + escapeHtml(safeClass(vm.status)) + '">' + escapeHtml(vm.status) + '</span>' +
        (vm.pinned ? '<span class="badge badge-pin">pinned</span>' : '') +
        '<span class="badge">' + escapeHtml(vm.type) + '</span>';
    }
    var pinButton = body.querySelector('[data-action="pin"], [data-action="unpin"]');
    if (pinButton) {
      pinButton.setAttribute('data-action', vm.pinned ? 'unpin' : 'pin');
      pinButton.className = 'btn' + (vm.pinned ? ' is-pinned' : '');
      pinButton.textContent = vm.pinned ? 'unpin 取消 pin' : 'pin 固定';
    }
    var metaEl = body.querySelector('.detail-meta');
    if (metaEl) {
      var values = {};
      vm.meta.forEach(function (item) { values[item.label] = item.value; });
      var dds = metaEl.querySelectorAll('dd');
      for (var i = 0; i < dds.length; i += 1) {
        var label = dds[i].getAttribute('data-dv');
        if (label && Object.prototype.hasOwnProperty.call(values, label)) dds[i].textContent = values[label];
      }
    }
    if (!state.weightDirty) {
      var input = body.querySelector('#weight-input');
      var range = body.querySelector('#weight-range');
      var active = document.activeElement;
      if (input && active !== input) input.value = vm.rawW.toFixed(1);
      if (range && active !== range) range.value = vm.rawW.toFixed(1);
    }
    if (els['curve-current']) {
      els['curve-current'].textContent = '当前 w=' + vm.wNow.toFixed(2) + '（原始 ' + vm.rawW.toFixed(2) + '）';
    }
  }

  // 权重衰减 canvas 折线（未来 30 天）
  function drawWeightCurve(vm) {
    var canvas = els['weight-canvas'];
    if (!canvas || !vm) return;
    var ctx = canvas.getContext && canvas.getContext('2d');
    if (!ctx) return;
    var rect = canvas.getBoundingClientRect();
    var cssWidth = Math.max(220, Math.round(rect.width || 340));
    var cssHeight = 190;
    var dpr = Math.min(2.5, Math.max(1, window.devicePixelRatio || 1));
    if (canvas.width !== Math.round(cssWidth * dpr) || canvas.height !== Math.round(cssHeight * dpr)) {
      canvas.width = Math.round(cssWidth * dpr);
      canvas.height = Math.round(cssHeight * dpr);
    }
    canvas.style.height = cssHeight + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssWidth, cssHeight);

    var padding = { left: 40, right: 14, top: 16, bottom: 26 };
    var plotW = cssWidth - padding.left - padding.right;
    var plotH = cssHeight - padding.top - padding.bottom;
    var series = decaySeries(vm.rawW, vm.baseTs, nowSec(), 30, 60);
    if (!series.length) return;
    var peak = series.reduce(function (acc, point) { return Math.max(acc, point.w); }, 0);
    var yMax = Math.max(0.5, Math.min(10, Math.ceil(peak * 10) / 10));

    function xOf(day) {
      return padding.left + (clamp(day, 0, 30) / 30) * plotW;
    }
    function yOf(w) {
      return padding.top + plotH - (clamp(w, 0, yMax) / yMax) * plotH;
    }

    // 网格 + y 轴刻度
    ctx.font = '10px ui-monospace, Consolas, "Cascadia Mono", monospace';
    ctx.lineWidth = 1;
    for (var i = 0; i <= 4; i += 1) {
      var value = (yMax * i) / 4;
      var y = yOf(value);
      ctx.strokeStyle = 'rgba(120, 104, 78, 0.14)';
      ctx.beginPath();
      ctx.moveTo(padding.left, y);
      ctx.lineTo(padding.left + plotW, y);
      ctx.stroke();
      ctx.fillStyle = 'rgba(120, 104, 78, 0.75)';
      ctx.textAlign = 'right';
      ctx.textBaseline = 'middle';
      ctx.fillText(value.toFixed(2), padding.left - 6, y);
    }
    // x 轴刻度
    [0, 7, 14, 21, 30].forEach(function (day) {
      var x = xOf(day);
      ctx.strokeStyle = 'rgba(120, 104, 78, 0.1)';
      ctx.beginPath();
      ctx.moveTo(x, padding.top);
      ctx.lineTo(x, padding.top + plotH);
      ctx.stroke();
      ctx.fillStyle = 'rgba(120, 104, 78, 0.75)';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      ctx.fillText(day + 'd', x, padding.top + plotH + 7);
    });

    function thresholdLine(threshold, color) {
      if (threshold > yMax) return;
      var y = yOf(threshold);
      ctx.save();
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = color;
      ctx.globalAlpha = 0.62;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(padding.left, y);
      ctx.lineTo(padding.left + plotW, y);
      ctx.stroke();
      ctx.restore();
      ctx.fillStyle = color;
      ctx.textAlign = 'right';
      ctx.textBaseline = 'bottom';
      ctx.fillText(threshold.toFixed(1), padding.left + plotW, y - 2);
    }
    thresholdLine(COMPRESS_W, '#c08a2e');
    thresholdLine(ARCHIVE_W, '#b4574d');

    // 曲线下面积
    var gradient = ctx.createLinearGradient(0, padding.top, 0, padding.top + plotH);
    gradient.addColorStop(0, 'rgba(166, 114, 61, 0.22)');
    gradient.addColorStop(1, 'rgba(166, 114, 61, 0.01)');
    ctx.beginPath();
    series.forEach(function (point, index) {
      var x = xOf(point.day);
      var y = yOf(point.w);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.lineTo(xOf(30), padding.top + plotH);
    ctx.lineTo(xOf(0), padding.top + plotH);
    ctx.closePath();
    ctx.fillStyle = gradient;
    ctx.fill();

    // 折线
    ctx.beginPath();
    series.forEach(function (point, index) {
      var x = xOf(point.day);
      var y = yOf(point.w);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = '#a6723d';
    ctx.lineWidth = 2;
    ctx.lineJoin = 'round';
    ctx.stroke();

    function dot(x, y, color, radius) {
      ctx.beginPath();
      ctx.arc(x, y, num(radius, 3), 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();
      ctx.strokeStyle = '#f7f2e7';
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }
    // 当前点 / 7d 半衰点 / 30d 终点
    var first = series[0];
    var seven = series.reduce(function (best, point) {
      return Math.abs(point.day - 7) < Math.abs(best.day - 7) ? point : best;
    }, series[0]);
    var last = series[series.length - 1];
    dot(xOf(first.day), yOf(first.w), '#a6723d', 3.2);
    dot(xOf(seven.day), yOf(seven.w), '#b98a55', 2.6);
    dot(xOf(last.day), yOf(last.w), '#8a7aa8', 2.6);
    ctx.font = '10px ui-monospace, Consolas, "Cascadia Mono", monospace';
    ctx.fillStyle = 'rgba(185, 138, 85, 0.95)';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'bottom';
    ctx.fillText('7d ' + seven.w.toFixed(2), xOf(seven.day), yOf(seven.w) - 7);
    ctx.fillStyle = 'rgba(138, 122, 168, 0.95)';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'bottom';
    ctx.fillText('30d ' + last.w.toFixed(2), xOf(30), yOf(last.w) - 7);
  }

  function bindDetailControls(vm) {
    var body = els['detail-body'];
    if (!body) return;
    var actionButtons = body.querySelectorAll('[data-action]');
    for (var i = 0; i < actionButtons.length; i += 1) {
      (function (button) {
        button.addEventListener('click', function () {
          handleDetailAction(button.getAttribute('data-action'));
        });
      })(actionButtons[i]);
    }
    var input = body.querySelector('#weight-input');
    var range = body.querySelector('#weight-range');
    var apply = body.querySelector('#weight-apply');
    function syncWeight(source, rawValue) {
      var value = clamp(num(rawValue, vm.rawW), 0, 10);
      state.weightDirty = true;
      if (input && source !== input) input.value = value.toFixed(1);
      if (range && source !== range) range.value = value.toFixed(1);
    }
    if (input) {
      input.addEventListener('input', function () { syncWeight(input, input.value); });
    }
    if (range) {
      range.addEventListener('input', function () { syncWeight(range, range.value); });
    }
    if (apply) {
      apply.addEventListener('click', function () { submitSetWeight(vm.id); });
    }
    var updateApply = body.querySelector('#update-apply');
    if (updateApply) updateApply.addEventListener('click', submitUpdate);
    var updateQuery = body.querySelector('#update-query');
    if (updateQuery) {
      updateQuery.addEventListener('keydown', function (event) {
        if (event.key === 'Enter') {
          event.preventDefault();
          submitUpdate();
        }
      });
    }
    var updateValue = body.querySelector('#update-value');
    if (updateValue) {
      updateValue.addEventListener('keydown', function (event) {
        if (event.key === 'Enter') {
          event.preventDefault();
          submitUpdate();
        }
      });
    }
  }

  function handleDetailAction(action) {
    var nodeId = state.selectedId;
    if (!nodeId) return;
    if (action === 'pin' || action === 'unpin') {
      submitAction({ action: action, id: nodeId, scope: state.scope }, action === 'pin' ? '已 pin：' + shortId(nodeId) : '已 unpin：' + shortId(nodeId));
      return;
    }
    if (action === 'forget') {
      var confirmed = window.confirm('确定要 forget 该节点吗？\n\n' + nodeId + '\n\n该操作会写入 Journal，影响后续检索。');
      if (!confirmed) return;
      submitAction({ action: 'forget', id: nodeId, scope: state.scope }, '已提交 forget：' + shortId(nodeId));
      return;
    }
    if (action === 'filter-journal') {
      state.journalFilter = nodeId;
      if (els['journal-filter']) els['journal-filter'].value = nodeId;
      refreshAll('journal-filter');
    }
  }

  async function submitAction(payload, successText) {
    if (state.actionBusy) return false;
    state.actionBusy = true;
    setActionBusy(true);
    var ok = false;
    try {
      await apiPost('/api/action', payload);
      ok = true;
      toast(successText || ('操作成功：' + (payload && payload.action)), 'ok');
      if (payload && payload.action === 'forget') {
        state.selectedId = null;
        state.pseudoNode = null;
        state.renderedDetailId = null;
      }
      state.weightDirty = false;
      await refreshAll('action');
    } catch (error) {
      toast('操作失败：' + msgOf(error), 'err', 7000);
    } finally {
      state.actionBusy = false;
      setActionBusy(false);
    }
    return ok;
  }

  function setActionBusy(flag) {
    var body = els['detail-body'];
    if (!body) return;
    var buttons = body.querySelectorAll('button');
    for (var i = 0; i < buttons.length; i += 1) buttons[i].disabled = Boolean(flag);
  }

  function submitSetWeight(nodeId) {
    var body = els['detail-body'];
    var input = body ? body.querySelector('#weight-input') : null;
    var value = clamp(num(input && input.value, NaN), 0, 10);
    if (!Number.isFinite(value)) {
      toast('请输入 0~10 之间的权重值', 'warn');
      return;
    }
    submitAction(
      { action: 'set_weight', id: nodeId, w: value, scope: state.scope },
      '已提交 set_weight：w=' + value.toFixed(1) + '（' + shortId(nodeId) + '）'
    );
  }

  function submitUpdate() {
    var body = els['detail-body'];
    if (!body) return;
    var queryInput = body.querySelector('#update-query');
    var valueInput = body.querySelector('#update-value');
    var query = queryInput ? queryInput.value.trim() : '';
    var newValue = valueInput ? valueInput.value.trim() : '';
    if (!query || !newValue) {
      toast('update 需要同时填写「旧说法/查询」与「新说法」', 'warn');
      return;
    }
    submitAction(
      { action: 'update', query: query, new_value: newValue, scope: state.scope },
      '已提交 update（改口）'
    ).then(function (ok) {
      if (ok) {
        if (queryInput) queryInput.value = '';
        if (valueInput) valueInput.value = '';
      }
    });
  }

  // ------------------------------------------------------------------ //
  // 渲染：3) 记忆列表 / 语义搜索
  // ------------------------------------------------------------------ //
  function renderMemoryList() {
    var list = els['memory-list'];
    if (!list) return;
    var at = nowSec();
    var searching = Boolean(state.search.q);
    var rows;
    var metaHtml;
    if (searching) {
      rows = memoryRowsFromSearch(state.search.items, at).slice(0, LIST_LIMIT);
      var simText = state.search.top1 !== null ? ' · <span class="sim">top1 sim=' + state.search.top1.toFixed(3) + '</span>' : '';
      metaHtml = '搜索结果 ' + rows.length + ' 条 · q="' + escapeHtml(truncateText(state.search.q, 80)) + '" · scope=' +
        escapeHtml(state.scope) + simText;
      if (state.search.loading) metaHtml += ' · <span class="is-warn">搜索中…</span>';
      if (state.search.error) {
        metaHtml += ' · <span class="is-warn">搜索失败，已降级</span>';
        showDegraded(els['memory-error'], '语义搜索失败：' + state.search.error);
      } else {
        hideDegraded(els['memory-error']);
      }
      if (els['search-clear']) els['search-clear'].hidden = false;
    } else {
      var graphForList = null;
      if (state.graph && Array.isArray(state.graph.nodes)) {
        graphForList = state.graph;
      } else if (state.panel && state.panel.nodes && Array.isArray(state.panel.nodes[state.scope])) {
        graphForList = { nodes: state.panel.nodes[state.scope] };
      }
      rows = memoryRowsFromGraph(graphForList, at).slice(0, LIST_LIMIT);
      var total = graphForList ? graphForList.nodes.length : 0;
      metaHtml = '当前 scope 节点 ' + total + ' 个 · 按当前权重 w 排序，显示前 ' + rows.length + ' 条 · 输入关键词做语义搜索';
      if (state.graphError) {
        metaHtml += ' · <span class="is-warn">图谱数据失败，列表降级</span>';
        showDegraded(els['memory-error'], '图谱/节点数据获取失败：' + state.graphError);
      } else {
        hideDegraded(els['memory-error']);
      }
      if (els['search-clear']) els['search-clear'].hidden = true;
    }
    if (els['memory-count']) els['memory-count'].textContent = rows.length + ' 条';
    if (els['search-meta']) els['search-meta'].innerHTML = metaHtml;
    if (!rows.length) {
      var emptyText;
      if (searching) {
        emptyText = state.search.loading ? '搜索中…' : (state.search.error ? '搜索失败（无结果）' : '没有命中记忆节点');
      } else {
        emptyText = '当前 scope 暂无记忆节点（等待注入）';
      }
      list.innerHTML = '<li class="empty-line">' + emptyText + '</li>';
      return;
    }
    list.innerHTML = rows.map(function (row) {
      var sim = row.sim !== null ? ' | sim=' + row.sim.toFixed(3) : '';
      var bracket = '[#' + shortId(row.id) + ' | w=' + row.w.toFixed(2) + ' | ' + row.dateText + sim + ']';
      var badgeHtml = '';
      if (row.pinned) badgeHtml += '<span class="badge badge-pin">pinned</span>';
      if (row.status && row.status !== 'active') {
        badgeHtml += '<span class="badge badge-' + escapeHtml(safeClass(row.status)) + '">' + escapeHtml(row.status) + '</span>';
      }
      return '<li class="memory-item' + (row.id === state.selectedId ? ' is-selected' : '') +
        '" data-node-id="' + escapeHtml(row.id) + '" title="' + escapeHtml(row.text) + '">' +
        '<span class="memory-bracket">' + escapeHtml(bracket) + '</span>' +
        (badgeHtml ? '<span class="badges">' + badgeHtml + '</span>' : '') +
        '<p class="memory-text">' + escapeHtml(row.text) + '</p>' +
        '</li>';
    }).join('');
  }

  async function runSearch(query) {
    var input = els['search-input'];
    var q = String(query === undefined ? (input ? input.value : '') : query).trim();
    if (!q) {
      clearSearch();
      return;
    }
    state.search.q = q;
    state.search.loading = true;
    state.search.error = null;
    if (els['search-clear']) els['search-clear'].hidden = false;
    if (els['search-btn']) {
      els['search-btn'].disabled = true;
      els['search-btn'].textContent = '搜索中…';
    }
    renderMemoryList();
    try {
      var data = await apiGet('/api/search', { q: q, scope: state.scope });
      if (state.search.q !== q) return;
      state.search.items = Array.isArray(data.items) ? data.items : [];
      state.search.top1 = num(data.top1_sim, null);
      state.search.error = null;
    } catch (error) {
      if (state.search.q !== q) return;
      state.search.items = [];
      state.search.top1 = null;
      state.search.error = msgOf(error);
    } finally {
      if (state.search.q === q) state.search.loading = false;
      if (els['search-btn']) {
        els['search-btn'].disabled = false;
        els['search-btn'].textContent = '搜索';
      }
      renderMemoryList();
    }
  }

  function clearSearch() {
    state.search = { q: '', items: [], top1: null, error: null, loading: false };
    if (els['search-input']) els['search-input'].value = '';
    if (els['search-clear']) els['search-clear'].hidden = true;
    renderMemoryList();
  }

  // ------------------------------------------------------------------ //
  // 渲染：5) Journal / 6) 剪枝 / 7) 死信告警 / 运行状态
  // ------------------------------------------------------------------ //
  function renderJournal() {
    var body = els['journal-body'];
    if (!body) return;
    var rows = journalRows(state.journal, nowSec());
    if (els['journal-count']) {
      els['journal-count'].textContent = (state.journalFilter ? 'filter: ' + state.journalFilter + ' · ' : '') + rows.length + ' 条';
    }
    if (!rows.length) {
      var emptyText = state.journalError
        ? '获取失败（' + state.journalError + '）'
        : (state.journalFilter ? '该 target 暂无变更记录' : '暂无变更记录（等待首次写入 / 改口 / 剪枝）');
      body.innerHTML = '<tr><td colspan="5" class="empty-line">' + escapeHtml(emptyText) + '</td></tr>';
    } else {
      body.innerHTML = rows.map(function (row) {
        var targetCell = row.target && row.target !== '—'
          ? '<button type="button" class="link-btn cell-target" data-target-id="' + escapeHtml(row.target) + '">' + escapeHtml(shortId(row.target)) + '</button>'
          : '<span class="muted">—</span>';
        var confidence = row.confidence !== null ? ' <span class="muted mono">conf ' + row.confidence.toFixed(2) + '</span>' : '';
        return '<tr>' +
          '<td class="cell-time">' + escapeHtml(row.timeText) + '</td>' +
          '<td><span class="badge">' + escapeHtml(row.kind) + '</span></td>' +
          '<td>' + targetCell + '</td>' +
          '<td class="cell-change">' + row.changeHtml + confidence + '</td>' +
          '<td><span class="' + row.rollbackClass + '">' + escapeHtml(row.rollbackText) + '</span></td>' +
          '</tr>';
      }).join('');
    }
    if (state.journalError) {
      showDegraded(els['journal-error'], 'Journal 刷新失败，表格显示上次数据（' + state.journalError + '）');
    } else {
      hideDegraded(els['journal-error']);
    }
  }

  function renderPrune() {
    var body = els['prune-body'];
    if (!body) return;
    var rows = pruneRows(state.prune);
    if (els['prune-count']) els['prune-count'].textContent = rows.length + ' 条';
    if (!rows.length) {
      var emptyText = state.pruneError ? '获取失败（' + state.pruneError + '）' : '暂无剪枝 / 压缩报告';
      body.innerHTML = '<tr><td colspan="4" class="empty-line">' + escapeHtml(emptyText) + '</td></tr>';
    } else {
      body.innerHTML = rows.map(function (row) {
        return '<tr>' +
          '<td class="cell-time">' + escapeHtml(row.timeText) + '</td>' +
          '<td><span class="badge">' + escapeHtml(row.kind) + '</span></td>' +
          '<td class="mono">' + escapeHtml(String(row.round)) + '</td>' +
          '<td class="cell-change" title="' + escapeHtml(row.rawTitle) + '">' + escapeHtml(row.summary) + '</td>' +
          '</tr>';
      }).join('');
    }
    if (state.pruneError) {
      showDegraded(els['prune-error'], '剪枝日志刷新失败，表格显示上次数据（' + state.pruneError + '）');
    } else {
      hideDegraded(els['prune-error']);
    }
  }

  function renderDeadLetters(panel) {
    var deadEl = els['dead-list'];
    var alarmEl = els['alarm-list'];
    if (!deadEl) return;
    if (!panel) {
      deadEl.innerHTML = '<li class="empty-line">panel_state 不可用，无法读取死信</li>';
      if (alarmEl) alarmEl.innerHTML = '<li class="empty-line">panel_state 不可用，无法读取告警</li>';
      if (els['dead-count']) els['dead-count'].textContent = '—';
      showDegraded(els['dead-error'], 'panel_state 获取失败：' + (state.panelError || '未知错误'));
      return;
    }
    hideDegraded(els['dead-error']);
    var dead = Array.isArray(panel.dead_letters) ? panel.dead_letters : [];
    var alarms = Array.isArray(panel.alarms) ? panel.alarms : [];
    if (els['dead-count']) els['dead-count'].textContent = '死信 ' + dead.length + ' · 告警 ' + alarms.length;
    deadEl.innerHTML = dead.length ? dead.map(function (item) {
      var payloadText = safeJson(item.payload);
      return '<li>' +
        '<div class="dead-head">' +
        '<span class="dead-id">#' + escapeHtml(item.id) + '</span>' +
        '<span class="dead-scope">' + escapeHtml(item.scope || '') + '</span>' +
        '<span class="dead-meta">' + escapeHtml(formatTs(item.ts)) + '</span>' +
        '<span class="dead-meta">attempts ' + escapeHtml(String(fmtNumber(item.attempts))) + '</span>' +
        '<span class="dead-meta">replayed ' + (item.replayed ? '是' : '否') + '</span>' +
        '</div>' +
        (payloadText ? '<div class="dead-payload" title="' + escapeHtml(payloadText) + '">payload: ' + escapeHtml(truncateText(payloadText, 140)) + '</div>' : '') +
        (item.last_error ? '<div class="dead-err">' + escapeHtml(truncateText(item.last_error, 200)) + '</div>' : '') +
        '</li>';
    }).join('') : '<li class="empty-line">暂无死信</li>';
    if (alarmEl) {
      alarmEl.innerHTML = alarms.length ? alarms.map(function (item) {
        var detailText = safeJson(item.detail);
        return '<li>' +
          '<div class="dead-head">' +
          '<span class="dead-id">' + escapeHtml(item.kind || 'alarm') + '</span>' +
          '<span class="dead-meta">' + escapeHtml(formatTs(item.ts)) + '</span>' +
          '<span class="dead-meta">' + escapeHtml(item.scope || '') + '</span>' +
          '</div>' +
          (detailText ? '<div class="dead-payload" title="' + escapeHtml(detailText) + '">' + escapeHtml(truncateText(detailText, 160)) + '</div>' : '') +
          '</li>';
      }).join('') : '<li class="empty-line">暂无告警</li>';
    }
  }

  function renderStatus(panel) {
    var el = els['status-list'];
    if (!el) return;
    var sidecar = panel && panel.stats && panel.stats.sidecar ? panel.stats.sidecar : null;
    var rows = [];
    rows.push(['graph_backend', panel && panel.stats ? (panel.stats.graph_backend || '—') : '—']);
    rows.push(['sidecar', sidecar ? ('pid ' + fmtNumber(sidecar.pid) + ' · v' + (sidecar.version || '—')) : '—']);
    rows.push(['llm_mode', sidecar && sidecar.llm_mode ? String(sidecar.llm_mode) : '—']);
    rows.push(['started_at', sidecar ? formatTs(sidecar.started_at) : '—']);
    rows.push(['当前 scope', state.scope]);
    rows.push(['API 地址', typeof location !== 'undefined' ? location.origin : '—']);
    rows.push(['轮询间隔', (POLL_MS / 1000) + ' 秒']);
    rows.push(['上次刷新', formatTs(state.lastRefresh)]);
    rows.push(['图谱缓存', state.graph ? ('节点 ' + state.graph.nodes.length + ' · 关系 ' + (state.graph.relations || []).length) : '—']);
    rows.push(['Journal 条目', state.journal.length + ' 条（含过滤）']);
    rows.push(['剪枝报告', state.prune.length + ' 条']);
    el.innerHTML = rows.map(function (row) {
      return '<dt>' + escapeHtml(row[0]) + '</dt><dd>' + escapeHtml(String(row[1])) + '</dd>';
    }).join('');
    var errorsEl = els['status-errors'];
    if (errorsEl) {
      errorsEl.innerHTML = (state.lastErrors || []).map(function (error) {
        return '<div class="status-error-item">' + escapeHtml(error) + '</div>';
      }).join('');
    }
  }

  // ------------------------------------------------------------------ //
  // 事件：scope / 搜索 / Journal 过滤 / 图交互 / 轮询
  // ------------------------------------------------------------------ //
  function switchScope(scope) {
    if (scope !== 'user' && scope !== 'workspace') return;
    if (scope === state.scope) return;
    state.scope = scope;
    var buttons = document.querySelectorAll('.scope-btn');
    for (var i = 0; i < buttons.length; i += 1) {
      var active = buttons[i].getAttribute('data-scope') === scope;
      buttons[i].classList.toggle('is-active', active);
      buttons[i].setAttribute('aria-pressed', active ? 'true' : 'false');
    }
    if (els['footer-scope']) els['footer-scope'].textContent = 'scope: ' + scope;
    state.selectedId = null;
    state.pseudoNode = null;
    state.renderedDetailId = null;
    state.currentDetailVm = null;
    state.graph = null;
    state.graphModel = null;
    state.graphSignature = '';
    state.graphError = null;
    state.graphDegraded = false;
    state.journal = [];
    state.journalError = null;
    state.prune = [];
    state.pruneError = null;
    state.search.items = [];
    state.search.top1 = null;
    state.search.error = null;
    renderDetail();
    renderMemoryList();
    renderJournal();
    renderPrune();
    if (els['graph-svg']) els['graph-svg'].innerHTML = '';
    renderGraph();
    refreshAll('scope-change');
    if (state.search.q) runSearch(state.search.q);
  }

  function bindEvents() {
    var scopeButtons = document.querySelectorAll('.scope-btn');
    for (var i = 0; i < scopeButtons.length; i += 1) {
      (function (button) {
        button.addEventListener('click', function () {
          switchScope(button.getAttribute('data-scope'));
        });
      })(scopeButtons[i]);
    }

    if (els['search-form']) {
      els['search-form'].addEventListener('submit', function (event) {
        event.preventDefault();
        runSearch();
      });
    }
    if (els['search-clear']) {
      els['search-clear'].addEventListener('click', clearSearch);
    }
    if (els['memory-list']) {
      els['memory-list'].addEventListener('click', function (event) {
        var item = event.target && event.target.closest ? event.target.closest('.memory-item') : null;
        if (!item) return;
        selectNode(item.getAttribute('data-node-id'));
      });
    }
    if (els['journal-filter-form']) {
      els['journal-filter-form'].addEventListener('submit', function (event) {
        event.preventDefault();
        var value = els['journal-filter'] ? els['journal-filter'].value.trim() : '';
        state.journalFilter = value;
        refreshAll('journal-filter');
      });
    }
    if (els['journal-filter-clear']) {
      els['journal-filter-clear'].addEventListener('click', function () {
        state.journalFilter = '';
        if (els['journal-filter']) els['journal-filter'].value = '';
        refreshAll('journal-filter');
      });
    }
    if (els['journal-body']) {
      els['journal-body'].addEventListener('click', function (event) {
        var button = event.target && event.target.closest ? event.target.closest('[data-target-id]') : null;
        if (!button) return;
        var id = button.getAttribute('data-target-id');
        if (!id) return;
        selectNode(id);
        state.journalFilter = id;
        if (els['journal-filter']) els['journal-filter'].value = id;
        refreshAll('journal-target');
      });
    }
    if (els['detail-clear']) {
      els['detail-clear'].addEventListener('click', clearSelection);
    }

    // 图谱：hover 显示 description 气泡；click 选中
    var svg = els['graph-svg'];
    if (svg) {
      svg.addEventListener('pointermove', function (event) {
        var group = event.target && event.target.closest ? event.target.closest('g.gnode') : null;
        if (!group) {
          hideGraphTooltip();
          return;
        }
        var node = findGraphNode(group.getAttribute('data-node-id'));
        if (!node) {
          hideGraphTooltip();
          return;
        }
        showGraphTooltip(node, event);
      });
      svg.addEventListener('pointerleave', hideGraphTooltip);
      svg.addEventListener('click', function (event) {
        var group = event.target && event.target.closest ? event.target.closest('g.gnode') : null;
        if (!group) return;
        selectNode(group.getAttribute('data-node-id'));
      });
    }

    window.addEventListener('resize', debounce(function () {
      var vm = state.currentDetailVm;
      if (vm) drawWeightCurve(vm);
    }, 180));

    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) refreshAll('visible');
    });

    // 兜底：任何未捕获异常只记录/提示，不中断面板（本地只读展示不允许白屏）
    window.addEventListener('error', function (event) {
      console.error('[panel] 未捕获异常', event.error || event.message);
    });
    window.addEventListener('unhandledrejection', function (event) {
      console.error('[panel] 未处理 Promise 拒绝', event.reason);
      event.preventDefault();
    });
  }

  function isLoopbackLocation() {
    if (typeof location === 'undefined') return true;
    if (location.protocol === 'file:') return false;
    var host = location.hostname;
    return host === '127.0.0.1' || host === 'localhost' || host === '::1' || host === '[::1]';
  }

  // ------------------------------------------------------------------ //
  // 启动
  // ------------------------------------------------------------------ //
  function boot() {
    cacheElements();
    bindEvents();
    if (els['footer-scope']) els['footer-scope'].textContent = 'scope: ' + state.scope;
    if (!isLoopbackLocation()) {
      toast('当前页面不是通过 127.0.0.1/localhost 打开；面板服务仅允许回环访问，API 可能不可用', 'warn', 8000);
    }
    renderDetail();
    renderMemoryList();
    refreshAll('boot');
    state.pollTimer = setInterval(function () {
      if (typeof document !== 'undefined' && document.hidden) return;
      refreshAll('poll');
    }, POLL_MS);
  }

  // ------------------------------------------------------------------ //
  // 自测 / 调试钩子（浏览器控制台可用；node 下可注入 DOM stub 集成测试）
  // ------------------------------------------------------------------ //
  var TEST_API = {
    HALF_LIFE_SECONDS: HALF_LIFE_SECONDS,
    SECONDS_PER_DAY: SECONDS_PER_DAY,
    TOTAL_HARD: TOTAL_HARD,
    CAPS: CAPS,
    SEGMENTS: SEGMENTS,
    POLL_MS: POLL_MS,
    escapeHtml: escapeHtml,
    truncateText: truncateText,
    num: num,
    formatTs: formatTs,
    formatDate: formatDate,
    decayValue: decayValue,
    decaySeries: decaySeries,
    currentWeight: currentWeight,
    budgetViewModel: budgetViewModel,
    detailViewModel: detailViewModel,
    memoryRowsFromGraph: memoryRowsFromGraph,
    memoryRowsFromSearch: memoryRowsFromSearch,
    graphModel: graphModel,
    layoutGraph: layoutGraph,
    journalRows: journalRows,
    pruneRows: pruneRows,
    summarizeChange: summarizeChange,
    normalizeState: normalizeState,
    buildQuery: buildQuery,
    shortId: shortId,
    // 运行时（集成自测 / 控制台排查用）
    refreshAll: refreshAll,
    selectNode: selectNode,
    clearSelection: clearSelection,
    switchScope: switchScope,
    runSearch: runSearch,
    submitAction: submitAction,
    state: state,
  };
  if (typeof globalThis !== 'undefined') {
    globalThis.__DSH_PANEL__ = TEST_API;
  }
  if (typeof document !== 'undefined' && typeof window !== 'undefined') {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', boot);
    } else {
      boot();
    }
  }
})();
