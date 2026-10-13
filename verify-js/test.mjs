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

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { canonical, canonicalDigest, formatMicros, merkleRoot, micros, verifyInclusion, verifyLedgerChain, verifyReceipt } from "./verify.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const VECTORS = join(HERE, "..", "spec", "vectors");
const load = (name) => JSON.parse(readFileSync(join(VECTORS, name), "utf8"));
import { createHash } from "node:crypto";
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

console.log("money");
check("2-decimal canonical form", formatMicros(micros("0.018000")) === "0.018000");
check("whole cents collapse to 2 decimals", formatMicros(micros("1.50")) === "1.50");
check("micro-dollars survive", micros("0.000001") === 1n);
check("rejects junk", (() => { try { micros("one"); return false; } catch { return true; } })());

console.log(`\n${checks - failures}/${checks} checks passed`);
process.exit(failures === 0 ? 0 : 1);
