/// <reference types="node" />
/**
 * 跨语言 token 计量一致性工具：
 *   node scripts/emit-tokens.mjs --samples tests/fixtures/token_samples.json
 *   node scripts/emit-tokens.mjs --text "你好 world"
 * 输出 {"tokens":[...]}，供 tests/python/test_tokens.py 与 Python base.estimate_tokens 对比。
 */
import { readFileSync } from 'node:fs'
import { estimateTokens, truncateToTokens } from '../lib/budgeter.js'

const args = process.argv.slice(2)
let texts = []
for (let i = 0; i < args.length; i += 1) {
  const arg = args[i]
  if (arg === '--samples' && args[i + 1]) {
    const raw = JSON.parse(readFileSync(args[i + 1], 'utf8'))
    texts = Array.isArray(raw) ? raw : raw.texts
    i += 1
  } else if (arg === '--text' && args[i + 1]) {
    texts.push(args[i + 1])
    i += 1
  }
}
if (texts.length === 0) {
  texts = ['你好 world', 'abcdefgh', '价格改成 80', 'A'.repeat(400), '']
}
const result = texts.map((text) => ({
  text,
  tokens: estimateTokens(text),
  truncated64: truncateToTokens(text, 64),
}))
process.stdout.write(JSON.stringify({ tokens: result.map((item) => item.tokens), details: result }, null, 2) + '\n')
