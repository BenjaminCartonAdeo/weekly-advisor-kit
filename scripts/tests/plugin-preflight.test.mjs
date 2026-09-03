import assert from "node:assert/strict"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import test from "node:test"

import { register } from "node:module"
register(new URL("../plugin-smoke-hook.mjs", import.meta.url))

const { default: WeeklyAdvisorPlugin } = await import("../../.opencode/plugins/weekly-advisor.ts")

function makeKit() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "wa-preflight-"))
  const engine = path.join(root, ".opencode", "plugins", "weekly-advisor-engine")
  fs.mkdirSync(path.join(engine, ".venv", "bin"), { recursive: true })
  fs.mkdirSync(path.join(engine, "weekly_telemetry_aggregator"), { recursive: true })
  fs.writeFileSync(path.join(engine, "weekly_telemetry_aggregator", "main.py"), "")
  fs.writeFileSync(path.join(engine, "weekly-telemetry-config.json"), "{}")
  fs.writeFileSync(path.join(engine, ".venv", "bin", "python"), "")
  return root
}

function makeCapturingKit() {
  const root = makeKit()
  const python = path.join(root, "fake-python.sh")
  fs.writeFileSync(
    python,
    '#!/bin/sh\nwhile [ "$#" -gt 0 ]; do\n  if [ "$1" = "--coherence" ]; then cat "$2" > "$CAPTURE"; printf "\\nARG=%s\\n" "$2" >> "$CAPTURE"; fi\n  shift\ndone\nprintf "ok\\n"\n',
  )
  fs.chmodSync(python, 0o755)
  return { root, python }
}

test("preflight resolves kit root from plugin location before cwd", async () => {
  const root = makeKit()
  const plugin = await WeeklyAdvisorPlugin({ worktree: path.dirname(root), directory: "/tmp" })
  const result = JSON.parse(await plugin.tool.weekly_preflight.execute({}))

  assert.equal(result.worktree, path.resolve(new URL("../..", import.meta.url).pathname))
  assert.equal(result.engine_ok, true)
})

test("weekly-review preflight fails with rc 3 before command execution", async () => {
  const previous = process.env.WEEKLY_KIT_ROOT
  process.env.WEEKLY_KIT_ROOT = "/tmp/not-a-weekly-kit"
  const plugin = await WeeklyAdvisorPlugin({ worktree: "/tmp/not-a-weekly-kit", directory: "/tmp/not-a-weekly-kit" })
  const output = { parts: [] }
  try {
    await assert.rejects(
      plugin["command.execute.before"]({ command: "weekly-review", sessionID: "s", arguments: "" }, output),
      /rc=3.*worktree Adeo requis/,
    )
  } finally {
    if (previous === undefined) delete process.env.WEEKLY_KIT_ROOT
    else process.env.WEEKLY_KIT_ROOT = previous
  }
})

test("weekly-advisor agent preflight fails before direct run boot", async () => {
  const previous = process.env.WEEKLY_KIT_ROOT
  process.env.WEEKLY_KIT_ROOT = "/tmp/not-a-weekly-kit"
  const plugin = await WeeklyAdvisorPlugin({ worktree: "/tmp/not-a-weekly-kit", directory: "/tmp/not-a-weekly-kit" })
  const output = { message: {}, parts: [] }
  try {
    await assert.rejects(
      plugin["chat.message"](
        { agent: "weekly-advisor", sessionID: "s", messageID: "m" },
        output,
      ),
      /rc=3.*worktree Adeo requis/,
    )
    await plugin["chat.message"]({ agent: "other-agent", sessionID: "s", messageID: "m" }, output)
  } finally {
    if (previous === undefined) delete process.env.WEEKLY_KIT_ROOT
    else process.env.WEEKLY_KIT_ROOT = previous
  }
})

test("skill-curate transports oversized coherence through a temporary file", async () => {
  const { root, python } = makeCapturingKit()
  const capture = path.join(root, "capture.json")
  const previousRoot = process.env.WEEKLY_KIT_ROOT
  const previousPython = process.env.WEEKLY_PYTHON
  const previousCapture = process.env.CAPTURE
  process.env.WEEKLY_KIT_ROOT = root
  process.env.WEEKLY_PYTHON = python
  process.env.CAPTURE = capture
  try {
    const plugin = await WeeklyAdvisorPlugin({ worktree: root, directory: root })
    const coherence = JSON.stringify({ findings: [{ tag_action: "archive", description: "x".repeat(200_000) }] })
    await plugin.tool.weekly_skill_curate.execute({ anchor: "2026-09-01", coherence })
    const transported = fs.readFileSync(capture, "utf8")
    assert.match(transported, /"findings"/)
    assert.match(transported, /x{200000}/)
    assert.match(transported, /ARG=\/tmp\/weekly-coherence-/)
    const stagedPath = transported.match(/ARG=(\/tmp\/weekly-coherence-[^\n]+)/)?.[1]
    assert.ok(stagedPath, "staged path captured")
    assert.equal(fs.existsSync(stagedPath), false, "staged JSON is cleaned after consumer exits")
  } finally {
    if (previousRoot === undefined) delete process.env.WEEKLY_KIT_ROOT
    else process.env.WEEKLY_KIT_ROOT = previousRoot
    if (previousPython === undefined) delete process.env.WEEKLY_PYTHON
    else process.env.WEEKLY_PYTHON = previousPython
    if (previousCapture === undefined) delete process.env.CAPTURE
    else process.env.CAPTURE = previousCapture
  }
})
