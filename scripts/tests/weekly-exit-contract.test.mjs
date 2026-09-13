import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
// Single-source post-dedup (26339c6) : la commande `weekly-review` est un wrapper
// thin qui renvoie à l'agent ; les contrats JOIN/RC vivent dans l'agent uniquement.
const contractFiles = [
  resolve(root, '.opencode/agents/weekly-advisor/weekly-advisor.md'),
];

const readContracts = () => Promise.all(contractFiles.map((file) => readFile(file, 'utf8')));

test('JOIN keeps valid transcript-truncated audit artifacts nonblocking', async () => {
  const contracts = await readContracts();

  for (const contract of contracts) {
    assert.match(contract, /transcript-truncated[\s\S]{0,240}(?:nonblocking|non-bloquant|informatif)/i);
  }
});

test('JOIN treats recovered watch and harness inputs as nonblocking', async () => {
  const contracts = await readContracts();

  for (const contract of contracts) {
    assert.match(contract, /(?:recovered|récupéré)[\s\S]{0,240}(?:watch|veille)[\s\S]{0,240}(?:harness|recovered|récupéré)/i);
    assert.match(contract, /(?:watch|veille)[\s\S]{0,240}(?:harness)[\s\S]{0,240}(?:nonblocking|non-bloquant|informatif)/i);
  }
});

test('external permission refusal and curation dry-run stay report-only', async () => {
  const contracts = await readContracts();

  for (const contract of contracts) {
    assert.match(contract, /external[\s-]+permission[\s-]+refusal[\s\S]{0,240}(?:report-only|report seul|informatif)/i);
    assert.match(contract, /curation[\s-]+dry-run[\s\S]{0,160}(?:nonblocking|non-bloquant|informatif)/i);
  }
});

test('JOIN blocks required artifact and contract failures plus critical rules', async () => {
  const contracts = await readContracts();

  for (const contract of contracts) {
    assert.match(contract, /missing[\s/,-]+invalid[\s-]+required[\s-]+artifacts[\s\S]{0,240}blocking/i);
    assert.match(contract, /malformed[\s-]+contracts?[\s\S]{0,240}blocking/i);
    assert.match(contract, /fatal[\s-]+rc[\s=:`]*2[\s\S]{0,240}blocking/i);
    for (const rule of ['mcp-tool-poisoning', 'unbounded-delegation', 'memory-write-unscoped']) {
      assert.match(contract, new RegExp(`${rule}[\\s\\S]{0,240}blocking`, 'i'));
    }
  }
});

test('required audit envelopes and downstream inputs use one strict contract', async () => {
  const contracts = await readContracts();

  for (const contract of contracts) {
    assert.match(contract, /schema-valid audit envelope with a nonempty summary/i);
    assert.match(contract, /raw watch findings[\s\S]{0,180}required[\s\S]{0,180}before downstream validation/i);
    assert.match(contract, /harness remediation proposal[\s\S]{0,180}required[\s\S]{0,180}before downstream remediation/i);
    assert.match(contract, /weekly-watch-findings-raw-<date>\.json/i);
    assert.match(contract, /weekly-harness-remediation-proposals-<date>\.json/i);
    assert.match(contract, /recovered input[\s\S]{0,220}exact canonical path[\s\S]{0,220}exact schema/i);
    assert.doesNotMatch(contract, /\bfallback\b/i);
    assert.doesNotMatch(contract, /summary\s*:\s*null/i);
  }
});

test('final exit remains identical across summary, output marker, and wrapper END', async () => {
  const contracts = await readContracts();

  for (const contract of contracts) {
    assert.match(contract, /summary\.exit[\s\S]{0,240}WEEKLY_REVIEW_RC[\s\S]{0,240}END[^\n]*exit=/i);
    assert.match(contract, /(?:summary\.exit|WEEKLY_REVIEW_RC)[\s\S]{0,240}(?:identical|equal|égaux|égalité)/i);
  }
});
