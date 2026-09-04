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
    '#!/bin/sh\nset -eu\n: > "$CAPTURE"\nwhile [ "$#" -gt 0 ]; do\n  case "$1" in\n    --coherence|--catalog|--usage)\n      printf "%s=%s\\n" "$1" "$2" >> "$CAPTURE"\n      cat "$2" >> "$CAPTURE"\n      printf "\\n" >> "$CAPTURE"\n      shift 2\n      ;;\n    --anchor|--runs-seen|--stale-days)\n      printf "%s=%s\\n" "$1" "$2" >> "$CAPTURE"\n      shift 2\n      ;;\n    --apply)\n      printf "%s\\n" "$1" >> "$CAPTURE"\n      shift\n      ;;\n    *)\n      case "$1" in\n        \\{*|\\[* ) printf "RAW=%s\\n" "$1" >> "$CAPTURE" ;;\n      esac\n      shift\n      ;;\n  esac\ndone\nif [ "${FAIL:-0}" = "1" ]; then\n  printf "forced failure\\n" >&2\n  exit 17\nfi\nprintf "ok\\n"\n',
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

test("skill-curate transports oversized JSON inputs through absolute temporary files", async () => {
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
    const catalog = JSON.stringify([{ skill_id: "weekly-old", metadata: { origin: "weekly", ttl_policy: "archive" }, blob: "y".repeat(120_000) }])
    const usage = JSON.stringify([{ skill_id: "weekly-old", usage: { last_loaded: "2026-01-01", load_count: 0 }, blob: "z".repeat(120_000) }])
    await plugin.tool.weekly_skill_curate.execute({
      anchor: "2026-09-01",
      coherence,
      catalog,
      usage,
      runs_seen: "7",
      stale_days: "45",
      apply: "true",
    })
    const transported = fs.readFileSync(capture, "utf8")
    assert.match(transported, /"findings"/)
    assert.match(transported, /x{200000}/)
    assert.match(transported, /"skill_id":"weekly-old"/)
    assert.match(transported, /z{120000}/)
    assert.doesNotMatch(transported, /^RAW=/m, "JSON never travels as an argv value")

    for (const flag of ["--coherence", "--catalog", "--usage"]) {
      const stagedPath = transported.match(new RegExp(`^${flag}=([^\\n]+)$`, "m"))?.[1]
      assert.ok(stagedPath, `${flag} staged path captured`)
      assert.equal(path.isAbsolute(stagedPath), true, `${flag} path is absolute`)
      assert.equal(stagedPath.startsWith(path.resolve(os.tmpdir())), true, `${flag} path uses temp dir`)
      assert.equal(fs.existsSync(stagedPath), false, `${flag} staged JSON is cleaned after consumer exits`)
    }
    assert.match(transported, /^--anchor=2026-09-01$/m)
    assert.match(transported, /^--runs-seen=7$/m)
    assert.match(transported, /^--stale-days=45$/m)
    assert.match(transported, /^--apply$/m)
  } finally {
    if (previousRoot === undefined) delete process.env.WEEKLY_KIT_ROOT
    else process.env.WEEKLY_KIT_ROOT = previousRoot
    if (previousPython === undefined) delete process.env.WEEKLY_PYTHON
    else process.env.WEEKLY_PYTHON = previousPython
    if (previousCapture === undefined) delete process.env.CAPTURE
    else process.env.CAPTURE = previousCapture
  }
})

for (const field of ["coherence", "catalog", "usage"]) {
  test(`skill-curate rejects malformed ${field} JSON with field context`, async () => {
    const { root, python } = makeCapturingKit()
    const capture = path.join(root, "capture.json")
    const previousRoot = process.env.WEEKLY_KIT_ROOT
    const previousPython = process.env.WEEKLY_PYTHON
    const previousCapture = process.env.CAPTURE
    const prefix = `weekly-${field}-`
    const before = new Set(fs.readdirSync(os.tmpdir()).filter((entry) => entry.startsWith(prefix)))
    process.env.WEEKLY_KIT_ROOT = root
    process.env.WEEKLY_PYTHON = python
    process.env.CAPTURE = capture
    try {
      const plugin = await WeeklyAdvisorPlugin({ worktree: root, directory: root })
      await assert.rejects(
        plugin.tool.weekly_skill_curate.execute({ anchor: "2026-09-01", [field]: '{"unterminated":' }),
        new RegExp(`weekly_skill_curate ${field} JSON invalide`),
      )
      assert.equal(fs.existsSync(capture), false, "malformed JSON fails before CLI execution")
      const after = new Set(fs.readdirSync(os.tmpdir()).filter((entry) => entry.startsWith(prefix)))
      assert.deepEqual(after, before, "malformed JSON does not leave a temporary directory")
    } finally {
      if (previousRoot === undefined) delete process.env.WEEKLY_KIT_ROOT
      else process.env.WEEKLY_KIT_ROOT = previousRoot
      if (previousPython === undefined) delete process.env.WEEKLY_PYTHON
      else process.env.WEEKLY_PYTHON = previousPython
      if (previousCapture === undefined) delete process.env.CAPTURE
      else process.env.CAPTURE = previousCapture
    }
  })
}

test("skill-curate cleans every staged JSON file when the CLI fails", async () => {
  const { root, python } = makeCapturingKit()
  const capture = path.join(root, "capture.json")
  const previousRoot = process.env.WEEKLY_KIT_ROOT
  const previousPython = process.env.WEEKLY_PYTHON
  const previousCapture = process.env.CAPTURE
  const previousFailure = process.env.FAIL
  process.env.WEEKLY_KIT_ROOT = root
  process.env.WEEKLY_PYTHON = python
  process.env.CAPTURE = capture
  process.env.FAIL = "1"
  try {
    const plugin = await WeeklyAdvisorPlugin({ worktree: root, directory: root })
    await assert.rejects(
      plugin.tool.weekly_skill_curate.execute({
        anchor: "2026-09-01",
        coherence: JSON.stringify({ findings: [{ tag_action: "archive" }] }),
        catalog: JSON.stringify([{ skill_id: "weekly-old" }]),
        usage: JSON.stringify([{ skill_id: "weekly-old", usage: {} }]),
      }),
      /weekly_telemetry_aggregator .*exit 17/,
    )
    const transported = fs.readFileSync(capture, "utf8")
    for (const flag of ["--coherence", "--catalog", "--usage"]) {
      const stagedPath = transported.match(new RegExp(`^${flag}=([^\\n]+)$`, "m"))?.[1]
      assert.ok(stagedPath, `${flag} staged path captured before failure`)
      assert.equal(fs.existsSync(stagedPath), false, `${flag} staged JSON is cleaned after CLI failure`)
    }
  } finally {
    if (previousRoot === undefined) delete process.env.WEEKLY_KIT_ROOT
    else process.env.WEEKLY_KIT_ROOT = previousRoot
    if (previousPython === undefined) delete process.env.WEEKLY_PYTHON
    else process.env.WEEKLY_PYTHON = previousPython
    if (previousCapture === undefined) delete process.env.CAPTURE
    else process.env.CAPTURE = previousCapture
    if (previousFailure === undefined) delete process.env.FAIL
    else process.env.FAIL = previousFailure
  }
})
