/// <reference types="node" />
/**
 * 用宿主的 TS 预算算法对真实 sidecar 数据做注入模拟（供 tests/python/test_tokens.py 与
 * 验收脚本交叉校验 D1/D2/D3 的“实测注入 token 恒定”）。
 *
 * 用法：
 *   node scripts/emit-injection.mjs --input data.json
 * 输入 JSON 结构（由 Python 验收脚本生成）：
 *   {"cases":[{"name":"d1-turn-200","memory_items":[...],"day":{"day_text","detail_text","history_text"},
 *              "explicit":false,"top1_sim":0.4,"high_weight_count":0}]}
 * 输出：
 *   {"cases":[{"name","text","tokens":{system,memory,day,detail,history,total},"injected_ids":[...]}]}
 */
import { readFileSync } from 'node:fs'
import { assembleInjection } from '../lib/budgeter.js'

const args = process.argv.slice(2)
const inputIndex = args.indexOf('--input')
const inputPath = inputIndex >= 0 ? args[inputIndex + 1] : ''
if (!inputPath) {
  process.stderr.write('usage: node scripts/emit-injection.mjs --input <json>\n')
  process.exit(2)
}
const DATA = JSON.parse(readFileSync(inputPath, 'utf8'))
const CONFIG = {
  totalHardTokens: 3000,
  systemMaxTokens: 128,
  memoryMaxTokens: 2048,
  memoryDefaultTokens: 1024,
  memoryHighSimTokens: 1536,
  memoryBoostTokens: 1792,
  memoryExplicitTokens: 2048,
  highSimThreshold: 0.8,
  highWeightThreshold: 5,
  dayMaxTokens: 1024,
  dayTextMaxTokens: 512,
  detailMaxTokens: 312,
  historyMaxTokens: 200,
}
const results = (DATA.cases || []).map((item) => {
  const result = assembleInjection({
    config: CONFIG,
    memoryItems: item.memory_items || [],
    day: item.day || null,
    explicit: Boolean(item.explicit),
    top1Sim: Number(item.top1_sim || 0),
    highWeightCount: Number(item.high_weight_count || 0),
  })
  return {
    name: item.name,
    text: result.text,
    tokens: result.tokens,
    injected_ids: result.injectedIds,
    measured: result.tokens.total,
  }
})
process.stdout.write(JSON.stringify({ cases: results }, null, 2) + '\n')
