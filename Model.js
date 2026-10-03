// Pure helpers for the Claude sessions widget; unit-tested with node (tests/model.test.js).

var SPINNER = ["⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷"]  // 8-dot: full cell height, stays vertically centred

function parse(text) {
  try {
    var d = JSON.parse(String(text || ""))
    if (d && Array.isArray(d.sessions)) return d
  } catch (e) {}
  return null
}

function waitingCount(sessions) {
  var n = 0
  for (var i = 0; i < sessions.length; i++) if (sessions[i].status === "waiting") n++
  return n
}

function busyCount(sessions) {
  return sessions.length - waitingCount(sessions)
}

// 309421 -> "309k", 1250000 -> "1.3M", 950 -> "950"
function formatTokens(n) {
  n = Math.max(0, Math.floor(n || 0))
  if (n >= 1000000) return (Math.round(n / 100000) / 10) + "M"
  if (n >= 1000) return Math.round(n / 1000) + "k"
  return String(n)
}

// Compact age: "now", "45s", "12m", "3h 5m", "2d"
function formatAge(seconds) {
  var s = Math.max(0, Math.floor(seconds || 0))
  if (s < 10) return "now"
  if (s < 60) return s + "s"
  var m = Math.floor(s / 60)
  if (m < 60) return m + "m"
  var h = Math.floor(m / 60)
  if (h < 24) return h + "h" + (m % 60 ? " " + (m % 60) + "m" : "")
  return Math.floor(h / 24) + "d"
}

// Bar label: spinner while every session is working, 󰛄 once any waits.
function barText(sessions, frame) {
  if (sessions.length === 0) return "󰛄"
  var waiting = waitingCount(sessions)
  var glyph = waiting > 0 ? "󰛄" : SPINNER[frame % SPINNER.length]
  return glyph + " " + sessions.length + (waiting > 0 && waiting < sessions.length ? " (" + waiting + ")" : "")
}

// Count shown next to the bar 󰛄: "2", or "3 (1)" when some (not all) wait.
function countText(sessions) {
  if (sessions.length === 0) return ""
  var waiting = waitingCount(sessions)
  return sessions.length + (waiting > 0 && waiting < sessions.length ? " (" + waiting + ")" : "")
}

function sessionTitle(s) {
  return s.title || s.project || s.name || ("pid " + s.pid)
}

function sessionMeta(s, now) {
  var parts = [s.project]
  parts.push((s.status === "waiting" ? "waiting " : "working ") + formatAge(now - s.since))
  if (s.contextTokens > 0) parts.push(formatTokens(s.contextTokens) + " ctx")
  return parts.join(" · ")
}

function heroMeta(sessions) {
  if (sessions.length === 0) return "No live sessions"
  var waiting = waitingCount(sessions)
  var busy = sessions.length - waiting
  var parts = []
  if (waiting) parts.push(waiting + " waiting for you")
  if (busy) parts.push(busy + " working")
  return parts.join(" · ")
}

function tooltip(sessions, now) {
  var lines = []
  for (var i = 0; i < sessions.length; i++) {
    var s = sessions[i]
    lines.push((s.status === "waiting" ? "● " : "○ ") + sessionTitle(s) + " — " + sessionMeta(s, now))
  }
  if (lines.length === 0) lines.push("No live Claude sessions")
  return lines.join("\n")
}

if (typeof module !== "undefined") {
  module.exports = {
    SPINNER: SPINNER,
    parse: parse,
    waitingCount: waitingCount,
    busyCount: busyCount,
    formatTokens: formatTokens,
    formatAge: formatAge,
    barText: barText,
    countText: countText,
    sessionTitle: sessionTitle,
    sessionMeta: sessionMeta,
    heroMeta: heroMeta,
    tooltip: tooltip
  }
}
