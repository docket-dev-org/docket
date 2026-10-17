#!/usr/bin/env node
/**
 * Conformance tests for the independent verifier.
 *
 * Same vectors as tests/test_vectors.py, different language, different code. This
 * is what "portable format" has to mean in practice: two implementations agreeing
 * on committed bytes rather than on documentation.
 *
 *   node verify-js/test.mjs
 */

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  canonical,
  canonicalDigest,
  findDuplicateName,
  formatMicros,
  latencySummary,
  merkleRoot,
  micros,
  percentileMs,
  verifyInclusion,
  verifyLedgerChain,
  verifyReceipt,
} from "./verify.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const VECTORS = join(HERE, "..", "spec", "vectors");
const load = (name) => JSON.parse(readFileSync(join(VECTORS, name), "utf8"));
const sha256Hex = (text) => createHash("sha256").update(text).digest("hex");

let failures = 0;
let checks = 0;

function check(name, condition, detail = "") {
  checks += 1;
  if (condition) {
    console.log(`  PASS  ${name}${detail ? `: ${detail}` : ""}`);
  } else {
    failures += 1;
    console.log(`  FAIL  ${name}${detail ? `: ${detail}` : ""}`);
  }
}

console.log("canonical JSON vectors");
for (const testCase of load("canonical.json").cases) {
  check(
    `canonical ${JSON.stringify(testCase.input).slice(0, 42)}`,
    canonical(testCase.input) === testCase.canonical && sha256Hex(testCase.canonical) === testCase.sha256,
    testCase.canonical,
  );
}

console.log("merkle vectors");
const merkle = load("merkle.json");
check("root of 7 leaves", merkleRoot(merkle.leaves) === merkle.root, merkle.root.slice(0, 16));
check("empty tree root", merkleRoot([]) === merkle.empty_tree_root);
for (const proof of merkle.proofs) {
  check(
    `inclusion proof for leaf ${proof.index}`,
    verifyInclusion(merkle.leaves[proof.index], proof.path, merkle.root),
  );
}
check(
  "forged leaf is rejected",
  !verifyInclusion(sha256Hex("forged"), merkle.proofs[2].path, merkle.root),
);

console.log("ledger vectors");
const ledger = load("ledger.json");
const chain = verifyLedgerChain(ledger.events);
check("chain verifies", chain.ok, chain.detail);
check("head matches vector", chain.head === ledger.head);
check("merkle root matches vector", merkleRoot(ledger.events.map((event) => event.hash)) === ledger.merkle_root);

console.log("receipt vectors");
const receipt = load("receipt.json");
const digestVector = load("receipt-digest.json");
const result = verifyReceipt(receipt, {
  ledgerEvents: ledger.events,
  ledgerFileBytes: Buffer.from(ledger.events.map((event) => canonical(event)).join("\n") + "\n", "utf8"),
  receiptCoreDigest: digestVector.receipt_core_sha256,
});
for (const checkItem of result.checks) {
  if (checkItem.status === "skip") continue;
  check(`receipt ${checkItem.name}`, checkItem.status === "pass", checkItem.detail);
}
check("receipt verifies overall", result.ok, result.failures.join("; "));

console.log("cache vectors");
const cacheReceipt = load("cache.json");
const cacheDigest = load("cache-digest.json");
const cacheResult = verifyReceipt(cacheReceipt);
for (const item of cacheResult.checks) {
  if (item.status === "skip") continue;
  check(`cache ${item.name}`, item.status === "pass", item.detail);
}
check("cached receipt verifies overall", cacheResult.ok, cacheResult.failures.join("; "));
check(
  "the saving is what the vector says, and the spend is untouched by it",
  cacheReceipt.cache.hits === cacheDigest.expected.hits &&
    cacheReceipt.cache.saved_usd === cacheDigest.expected.saved_usd &&
    cacheReceipt.totals.cost_usd === cacheDigest.expected.spend_usd &&
    cacheReceipt.totals.calls === cacheDigest.expected.metered_calls,
  JSON.stringify(cacheDigest.expected),
);
const editedCache = JSON.parse(JSON.stringify(cacheReceipt));
editedCache.cache.saved_usd = "9.99";
const editedCacheResult = verifyReceipt(editedCache);
check(
  "an overstated saving is caught",
  !editedCacheResult.ok && editedCacheResult.failures.some((f) => f.includes("cache.saved_usd is")),
  editedCacheResult.failures.join("; "),
);
const editedGroup = JSON.parse(JSON.stringify(cacheReceipt));
editedGroup.cache.by_origin[0].origin_seq = 99;
check("a hit citing a call the receipt does not carry is caught", !verifyReceipt(editedGroup).ok);
const flippedProof = JSON.parse(JSON.stringify(cacheReceipt));
flippedProof.cache.by_origin[0].merkle_path[0].hash = "0".repeat(64);
check(
  "a flipped digit in a group proof is caught",
  verifyReceipt(flippedProof).failures.some((f) => f.startsWith("cache-hit-proofs")),
);

console.log("tamper detection");
const tampered = JSON.parse(JSON.stringify(receipt));
tampered.appendix.usage[0].output_tokens += 1;
const tamperedResult = verifyReceipt(tampered, { receiptCoreDigest: digestVector.receipt_core_sha256 });
check("a modified usage row is caught", !tamperedResult.ok, tamperedResult.failures.join("; "));

const tamperedLedger = JSON.parse(JSON.stringify(ledger.events));
tamperedLedger[3].data.model = "forged-model";
const forgedLedgerResult = verifyReceipt(receipt, { ledgerEvents: tamperedLedger });
check("an edited ledger is caught", !forgedLedgerResult.ok, forgedLedgerResult.failures.join("; "));

const missingTable = JSON.parse(JSON.stringify(receipt));
delete missingTable.price_table;
check("a missing price table is caught", !verifyReceipt(missingTable).ok);

// Parity with the Python verifier on the *shape* of a failure, not just the verdict:
// a mid-file edit must not leave head or the Merkle root reporting a pass.
const midFileEdit = JSON.parse(JSON.stringify(ledger.events));
midFileEdit[3].data = { ...midFileEdit[3].data, injected: "by an attacker" };
const edited = verifyReceipt(receipt, { ledgerEvents: midFileEdit });
const status = (name) => (edited.checks.find((c) => c.name === name) ?? {}).status;
check("a mid-file edit breaks the chain", status("ledger-chain") === "fail");
check("a mid-file edit skips the head check", status("head") === "skip", "stored tail hash is intact; claiming a pass would mislead");
check("a mid-file edit fails the Merkle root", status("merkle-root") === "fail");
check("a mid-file edit invalidates usage rows", status("usage-inclusion-proofs") === "fail");

console.log("latency (the one measured claim)");
check("nearest-rank always returns a measured value", percentileMs([40, 10, 30, 20], 50) === 20);
check("percentile of one sample is that sample", percentileMs([7], 99) === 7);
const latencyVector = latencySummary([10, 20, 30, 40, 5000], 5);
check(
  "summary carries its method and both counts",
  latencyVector.method === "nearest-rank" &&
    latencyVector.measured_calls === 5 &&
    latencyVector.total_rows === 5 &&
    latencyVector.p50_ms === 30 &&
    latencyVector.p99_ms === 5000 &&
    latencyVector.max_ms === 5000 &&
    latencyVector.total_ms === 5100,
  JSON.stringify(latencyVector),
);

const withLatency = JSON.parse(JSON.stringify(receipt));
withLatency.appendix.usage = withLatency.appendix.usage.map((row) => ({ ...row, latency_ms: 100 + row.seq * 10 }));
withLatency.latency = latencySummary(withLatency.appendix.usage.map((row) => row.latency_ms), withLatency.appendix.usage.length);
const latencyOk = verifyReceipt(withLatency, { ledgerEvents: ledger.events });
check("an honest latency summary verifies", latencyOk.ok, latencyOk.failures.join("; "));
check(
  "the summary is recomputed, not trusted",
  (latencyOk.checks.find((check) => check.name === "latency-summary") ?? {}).status === "pass",
);

const fakedLatency = JSON.parse(JSON.stringify(withLatency));
fakedLatency.latency.p99_ms = 1;
const fakedResult = verifyReceipt(fakedLatency, { ledgerEvents: ledger.events });
check(
  "a faked p99 fails verification",
  !fakedResult.ok && fakedResult.failures.some((failure) => failure.includes("p99_ms")),
  fakedResult.failures.join("; "),
);

const wrongRule = JSON.parse(JSON.stringify(withLatency));
wrongRule.latency.method = "linear-interpolation";
check(
  "a summary computed by another rule is refused, not reinterpreted",
  !verifyReceipt(wrongRule, { ledgerEvents: ledger.events }).ok,
);

const inventedMember = JSON.parse(JSON.stringify(withLatency));
inventedMember.latency.stddev_ms = 3;
check("an undefined latency member is refused", !verifyReceipt(inventedMember, { ledgerEvents: ledger.events }).ok);

const fractional = JSON.parse(JSON.stringify(withLatency));
fractional.appendix.usage[0].latency_ms = 12.5;
check(
  "a fractional latency in a row is a failure, not a silent drop",
  !verifyReceipt(fractional, { ledgerEvents: ledger.events }).ok,
);

// Built explicitly rather than read off the committed vector: when this test was written it
// asserted that `receipt.json` had no latency member, which *was* true — the vector had been
// generated before the feature landed and never regenerated, so the test was holding a drift
// in place instead of checking a rule. The rule is what matters: a receipt with no measurement
// must skip, not pass.
const noLatencyReceipt = JSON.parse(JSON.stringify(withLatency));
delete noLatencyReceipt.latency;
noLatencyReceipt.appendix.usage = noLatencyReceipt.appendix.usage.map((row) => {
  const copy = { ...row };
  delete copy.latency_ms;
  return copy;
});
const noLatency = verifyReceipt(noLatencyReceipt, { ledgerEvents: ledger.events });
check(
  "a receipt with no latency skips rather than passing",
  (noLatency.checks.find((check) => check.name === "latency-summary") ?? {}).status === "skip",
  JSON.stringify((noLatency.checks.find((check) => check.name === "latency-summary") ?? {}).detail),
);

console.log("the negative corpus found three defects; these hold the fixes");
// 1. `JSON.parse` keeps the last of two identical names. A receipt that names `ledger` twice means
// two different things to two readers, and the one that computed it is usually not the one reading it.
check("duplicate name is found", findDuplicateName('{"a": 1, "a": 2}') === "a");
check("nested objects are separate namespaces", findDuplicateName('{"a": {"b": 1}, "c": {"b": 2}}') === null);
check("a brace inside a string is not a brace", findDuplicateName('{"a": "{\"b\": 1}", "b": 2}') === null);
check("an escaped quote does not end a string", findDuplicateName('{"a": "x\\"y", "b": 2}') === null);
check("arrays of objects are scanned", findDuplicateName('[{"a": 1, "a": 2}]') === "a");
check("valid receipts have none", findDuplicateName(JSON.stringify(load("receipt.json"))) === null);

// 2. `usage_source` is an enum (spec §6.1), and a consumer is required not to present estimated
// usage as metered. A word outside the vocabulary used to verify, because only the arithmetic of the
// row was examined.
const guessedSource = JSON.parse(JSON.stringify(load("receipt.json")));
guessedSource.appendix.usage[0].usage_source = "guessed";
const guessed = verifyReceipt(guessedSource);
check(
  "an out-of-vocabulary usage_source is a shape failure",
  guessed.ok === false && guessed.checks.some((item) => item.name === "receipt-shape" && item.status === "fail" && item.detail.includes("guessed")),
);

// 3. The inclusion proof and the re-binding are two facts, and they are now two checks: a receipt
// whose proofs are intact but whose rows are not the ledger's events has to say so, rather than
// reporting a broken proof.
const base = load("receipt.json");
const reboundReceipt = JSON.parse(JSON.stringify(base));
const reboundLedger = JSON.parse(JSON.stringify(load("ledger.json")));
const withLedger = verifyReceipt(reboundReceipt, { ledgerEvents: reboundLedger.events });
check(
  "a clean receipt proves and re-binds",
  withLedger.checks.find((item) => item.name === "usage-inclusion-proofs").status === "pass" &&
    withLedger.checks.find((item) => item.name === "usage-rows-rebound").status === "pass",
);
check(
  "re-binding is a skip without a ledger, never a pass",
  verifyReceipt(reboundReceipt).checks.find((item) => item.name === "usage-rows-rebound").status === "skip",
);

console.log("the totals are three statements, not one number");
{
  const miscounted = JSON.parse(JSON.stringify(load("receipt.json")));
  miscounted.totals.calls = 0;
  const result = verifyReceipt(miscounted);
  const row = result.checks.filter((item) => item.name === "totals");
  check("a call count that disagrees with the appendix fails totals", row.length === 1 && row[0].status === "fail", JSON.stringify(row[0]?.detail));

  const moved = JSON.parse(JSON.stringify(load("receipt.json")));
  moved.totals.input_tokens -= 100;
  moved.totals.output_tokens += 100;
  const movedResult = verifyReceipt(moved);
  const tokenRow = movedResult.checks.filter((item) => item.name === "token-totals");
  check(
    "tokens moved between the halves fail token-totals",
    tokenRow.length === 1 && tokenRow[0].status === "fail" && tokenRow[0].detail.includes("input_tokens"),
    JSON.stringify(tokenRow[0]?.detail),
  );
}

console.log("money");
check("2-decimal canonical form", formatMicros(micros("0.018000")) === "0.018000");
check("whole cents collapse to 2 decimals", formatMicros(micros("1.50")) === "1.50");
check("micro-dollars survive", micros("0.000001") === 1n);
check("rejects junk", (() => { try { micros("one"); return false; } catch { return true; } })());

console.log(`\n${checks - failures}/${checks} checks passed`);
process.exit(failures === 0 ? 0 : 1);
