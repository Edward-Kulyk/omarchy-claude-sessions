const test = require("node:test")
const assert = require("node:assert")
const M = require("../Model.js")

const W = { pid: 1, project: "alpha", title: "Fix auth", status: "waiting", since: 1000, contextTokens: 309421 }
const B = { pid: 2, project: "beta", title: "", status: "busy", since: 1200, contextTokens: 0 }

test("parse", () => {
  assert.strictEqual(M.parse("x"), null)
  assert.strictEqual(M.parse('{"sessions":[]}').sessions.length, 0)
})

test("formatTokens and formatAge", () => {
  assert.strictEqual(M.formatTokens(950), "950")
  assert.strictEqual(M.formatTokens(309421), "309k")
  assert.strictEqual(M.formatTokens(1250000), "1.3M")
  assert.strictEqual(M.formatAge(5), "now")
  assert.strictEqual(M.formatAge(45), "45s")
  assert.strictEqual(M.formatAge(720), "12m")
  assert.strictEqual(M.formatAge(3 * 3600 + 300), "3h 5m")
  assert.strictEqual(M.formatAge(3 * 3600), "3h")
  assert.strictEqual(M.formatAge(50 * 3600), "2d")
})

test("barText: spinner only when all busy, count of waiting in parens", () => {
  assert.strictEqual(M.barText([], 0), "󰛄")
  assert.strictEqual(M.barText([B], 1), "⣽ 1")
  assert.strictEqual(M.barText([W, B], 3), "󰛄 2 (1)")
  assert.strictEqual(M.barText([W], 3), "󰛄 1")
})

test("session lines", () => {
  assert.strictEqual(M.sessionTitle(W), "Fix auth")
  assert.strictEqual(M.sessionTitle(B), "beta")
  assert.strictEqual(M.sessionMeta(W, 1720), "alpha · waiting 12m · 309k ctx")
  assert.strictEqual(M.sessionMeta(B, 1205), "beta · working now")
  assert.strictEqual(M.heroMeta([W, B]), "1 waiting for you · 1 working")
  assert.strictEqual(M.heroMeta([]), "No live sessions")
})

test("tooltip", () => {
  assert.strictEqual(M.tooltip([W, B], 1720), "● Fix auth — alpha · waiting 12m · 309k ctx\n○ beta — beta · working 8m")
  assert.strictEqual(M.tooltip([], 0), "No live Claude sessions")
})

test("countText", () => {
  assert.strictEqual(M.countText([]), "")
  assert.strictEqual(M.countText([B]), "1")
  assert.strictEqual(M.countText([W, B]), "2 (1)")
  assert.strictEqual(M.countText([W]), "1")
})
