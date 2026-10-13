#!/usr/bin/env node
/**
 * verify.mjs — an independent Docket receipt verifier.
 *
 * Written against spec/docket-receipt-1.md, not against the Python code: it shares
 * no source with the reference implementation, uses only Node's standard library,
 * and runs offline. If this file and `docket verify` disagree, one of them is
 * wrong, and the conformance vectors in spec/vectors/ decide which.
 *
 *   node verify-js/verify.mjs receipt.json [--ledger runs/x.jsonl] [--json]
 *
 * Exit codes: 0 verified · 1 not verified · 2 usage/IO error.
 *
 * What it checks
 *   1. format tag
 *   2. ledger chain: every event hash recomputed from canonical JSON, links checked
 *   3. head, event count, Merkle root against the receipt
 *   4. every usage row proven present in the receipt's Merkle tree
 *   5. cost recomputed from token counts using the price table embedded in the receipt
 *   6. the ledger file digest, when the ledger is supplied
 *   7. the receipt core digest, when a digest vector is supplied
 *   8. seal (HMAC-SHA256) and signature (Ed25519), when present and keys are available
 */

import { createHash, createHmac, createPublicKey, verify as cryptoVerify } from "node:crypto";
import { readFileSync } from "node:fs";

// --------------------------------------------------------------------------- //
// Canonical JSON (§3 of the spec)
// --------------------------------------------------------------------------- //

function canonical(value) {
  if (value === null) return "null";
  const type = typeof value;
  if (type === "boolean") return value ? "true" : "false";
  if (type === "number") {
    if (!Number.isFinite(value)) throw new Error("non-finite number in canonical JSON");
    if (!Number.isInteger(value)) {
      // Mirrors the Python implementation exactly: a hashed structure may contain
      // integers only, because shortest-representation rules differ between
      // runtimes (Python prints 5.0 as "5.0", JavaScript prints it as "5").
      throw new Error(`non-integer number ${value} in canonical JSON; encode it as a decimal string (§3.3)`);
    }
    return String(value);
  }
  if (type === "string") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (type === "object") {
    const keys = Object.keys(value).sort();
    return `{${keys.map((k) => `${JSON.stringify(k)}:${canonical(value[k])}`).join(",")}}`;
  }
  throw new Error(`unsupported type in canonical JSON: ${type}`);
}

const sha256Hex = (data) => createHash("sha256").update(data).digest("hex");
const canonicalDigest = (value) => sha256Hex(canonical(value));

// --------------------------------------------------------------------------- //
// Merkle tree, RFC 6962 (§4 of the spec)
// --------------------------------------------------------------------------- //

const LEAF = Buffer.from([0x00]);
const NODE = Buffer.from([0x01]);

function leafHash(hexDigest) {
  const digest = Buffer.from(hexDigest, "hex");
  return createHash("sha256").update(LEAF).update(digest).digest();
}

function nodeHash(left, right) {
  return createHash("sha256").update(NODE).update(left).update(right).digest();
}

function merkleRoot(hexDigests) {
  if (hexDigests.length === 0) return sha256Hex(Buffer.alloc(0));
  let level = hexDigests.map(leafHash);
  while (level.length > 1) {
    const next = [];
    for (let i = 0; i + 1 < level.length; i += 2) next.push(nodeHash(level[i], level[i + 1]));
    if (level.length % 2 === 1) next.push(level[level.length - 1]);
    level = next;
  }
  return level[0].toString("hex");
}

function verifyInclusion(hexDigest, path, root) {
  let node = leafHash(hexDigest);
  for (const step of path) {
    const sibling = Buffer.from(step.hash, "hex");
    node = step.side === "right" ? nodeHash(node, sibling) : nodeHash(sibling, node);
  }
  return node.toString("hex") === String(root).toLowerCase();
}

// --------------------------------------------------------------------------- //
// Money: decimal strings only, verified in integer micro-dollars (§3.3)
// --------------------------------------------------------------------------- //

const USD_RE = /^-?\d+(\.\d{1,6})?$/;

function micros(text) {
  const value = String(text);
  if (!USD_RE.test(value)) throw new Error(`not a canonical USD amount: ${JSON.stringify(text)}`);
  const negative = value.startsWith("-");
  const [whole, fraction = ""] = value.replace("-", "").split(".");
  const padded = (fraction + "000000").slice(0, 6);
  const total = BigInt(whole) * 1_000_000n + BigInt(padded);
  return negative ? -total : total;
}

function formatMicros(value) {
  const negative = value < 0n;
  const magnitude = negative ? -value : value;
  const text = (magnitude / 1_000_000n).toString() + "." + (magnitude % 1_000_000n).toString().padStart(6, "0");
  const [whole, fraction] = text.split(".");
  const cents = magnitude % (1_000_000n / 100n);
  return (negative ? "-" : "") + (cents === 0n ? `${whole}.${fraction.slice(0, 2)}` : `${whole}.${fraction}`);
}

// --------------------------------------------------------------------------- //
// Price table + glob matching (§6 of the spec)
// --------------------------------------------------------------------------- //

function globToRegExp(pattern) {
  const escaped = pattern.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".");
  return new RegExp(`^${escaped}$`, "i");
}

function loadPrices(document) {
  const entries = [];
  for (const family of document.families ?? []) {
    const patterns = String(family.pattern ?? "*")
      .split("/")
      .map((p) => p.trim())
      .filter(Boolean);
    for (const pattern of patterns) {
      entries.push({
        regex: globToRegExp(pattern),
        pattern: pattern.toLowerCase(),
        input: micros(String(family.input ?? "0")),
        cached: micros(String(family.cached_input ?? family.input ?? "0")),
        output: micros(String(family.output ?? "0")),
      });
    }
  }
  if (entries.length === 0) throw new Error("receipt embeds an empty price table");
  return entries;
}

function priceFor(entries, model) {
  const name = String(model ?? "").toLowerCase();
  for (const entry of entries) {
    if (entry.pattern !== "*" && entry.regex.test(name)) return { entry, assumed: false };
  }
  const fallback = entries.find((entry) => entry.pattern === "*");
  if (!fallback) throw new Error("price table has no fallback entry");
  return { entry: fallback, assumed: true };
}

function callCost(entries, model, inputTokens, cachedTokens, outputTokens) {
  const { entry, assumed } = priceFor(entries, model);
  const billableInput = BigInt(Math.max(inputTokens - cachedTokens, 0));
  const total =
    (billableInput * entry.input + BigInt(cachedTokens) * entry.cached + BigInt(outputTokens) * entry.output) /
    1_000_000n;
  return { micros: total, assumed };
}

// --------------------------------------------------------------------------- //
// Verification
// --------------------------------------------------------------------------- //

function verifyLedgerChain(events) {
  const genesis = sha256Hex(Buffer.from("docket/genesis/v1", "utf8"));
  let previous = genesis;
  for (let index = 0; index < events.length; index += 1) {
    const event = events[index];
    if (event.seq !== index) return { ok: false, detail: `sequence gap at #${index} (found seq ${event.seq})` };
    if (event.prev !== previous) return { ok: false, detail: `broken link at #${index}` };
    const recomputed = canonicalDigest({
      seq: event.seq,
      ts: event.ts,
      type: event.type,
      data: event.data,
      prev: event.prev,
    });
    if (recomputed !== event.hash) return { ok: false, detail: `content hash mismatch at #${index}` };
    previous = event.hash;
  }
  return { ok: true, detail: `${events.length} events verified`, head: previous, genesis };
}

function verifyReceipt(receipt, options = {}) {
  const checks = [];
  const add = (name, status, detail) => checks.push({ name, status, detail });

  if (receipt.format !== "docket/receipt/v1") {
    add("format", "fail", `unrecognised format ${JSON.stringify(receipt.format)}`);
  } else {
    add("format", "pass", "docket/receipt/v1");
  }

  const ledgerMeta = receipt.ledger ?? {};
  const events = options.ledgerEvents ?? null;

  if (!events) {
    add("ledger-chain", "skip", "no ledger supplied; the receipt alone cannot rebuild the chain");
  } else {
    const chain = verifyLedgerChain(events);
    add("ledger-chain", chain.ok ? "pass" : "fail", chain.detail);
    const head = events.length ? events[events.length - 1].hash : chain.genesis;
    add(
      "head",
      head === ledgerMeta.head ? "pass" : "fail",
      head === ledgerMeta.head ? `head matches (${String(head).slice(0, 16)}…)` : "ledger head differs from receipt",
    );
    const root = merkleRoot(events.map((event) => event.hash));
    add(
      "merkle-root",
      root === ledgerMeta.merkle_root ? "pass" : "fail",
      root === ledgerMeta.merkle_root ? `${root.slice(0, 16)}…` : "recomputed root differs from receipt",
    );
    add(
      "event-count",
      events.length === ledgerMeta.events ? "pass" : "fail",
      `${events.length} events in ledger, receipt claims ${ledgerMeta.events}`,
    );
  }

  const rows = receipt.appendix?.usage ?? [];
  const rootValue = String(ledgerMeta.merkle_root ?? "");
  let proven = 0;
  let unproven = 0;
  for (const row of rows) {
    if (!row.merkle_path) {
      unproven += 1;
      continue;
    }
    if (verifyInclusion(row.event_hash, row.merkle_path, rootValue)) proven += 1;
    else unproven += 1;
  }
  if (rows.length === 0) {
    add("usage-inclusion-proofs", "pass", "no metered calls in this session");
  } else if (unproven > 0) {
    add("usage-inclusion-proofs", "fail", `${unproven} of ${rows.length} usage rows failed their inclusion proof`);
  } else {
    add("usage-inclusion-proofs", "pass", `${proven} usage rows proven against the Merkle root`);
  }

  // Cost recomputation — the property that lets a third party check the arithmetic.
  const priceDocument = receipt.price_table?.document;
  if (!priceDocument) {
    add("cost-recomputation", "fail", "receipt does not embed the price table it used");
  } else {
    const entries = loadPrices(priceDocument);
    let total = 0n;
    let inputTokens = 0;
    let cachedTokens = 0;
    let outputTokens = 0;
    let mismatches = 0;
    const assumed = new Set();
    for (const row of rows) {
      const input = Number(row.input_tokens ?? 0);
      const cached = Number(row.cached_input_tokens ?? 0);
      const output = Number(row.output_tokens ?? 0);
      const { micros: computed, assumed: isAssumed } = callCost(entries, row.model, input, cached, output);
      if (isAssumed) assumed.add(row.model ?? "unknown");
      const recorded = row.cost_usd ? micros(row.cost_usd) : null;
      if (recorded !== null && recorded !== computed) {
        mismatches += 1;
        add(
          "cost-recomputation",
          "fail",
          `call #${row.seq}: recorded ${row.cost_usd} but token counts imply ${formatMicros(computed)}`,
        );
        break;
      }
      total += computed;
      inputTokens += input;
      cachedTokens += cached;
      outputTokens += output;
    }
    if (mismatches === 0) {
      add("cost-recomputation", "pass", `${rows.length} calls recomputed from token counts`);
      const declared = micros(String(receipt.totals?.cost_usd ?? "0"));
      add(
        "totals",
        declared === total ? "pass" : "fail",
        declared === total
          ? `total ${formatMicros(total)} agrees with recomputation`
          : `receipt total ${formatMicros(declared)} != recomputed ${formatMicros(total)}`,
      );
      const declaredTokens = Number(receipt.totals?.total_tokens ?? -1);
      const recomputedTokens = inputTokens + outputTokens;
      add(
        "token-totals",
        declaredTokens === recomputedTokens ? "pass" : "fail",
        `${recomputedTokens.toLocaleString("en-US")} tokens recomputed`,
      );
      add(
        "cached-totals",
        Number(receipt.totals?.cached_input_tokens ?? 0) === cachedTokens ? "pass" : "fail",
        `${cachedTokens.toLocaleString("en-US")} cached input tokens`,
      );
      if (assumed.size > 0) {
        add("assumed-prices", "pass", `flagged as assumed: ${[...assumed].join(", ")}`);
      }
    }
  }

  if (options.ledgerFileBytes) {
    const digest = sha256Hex(options.ledgerFileBytes);
    add(
      "ledger-file-digest",
      digest === ledgerMeta.file_sha256 ? "pass" : "fail",
      digest === ledgerMeta.file_sha256 ? `sha256 ${digest.slice(0, 16)}…` : "ledger file digest differs",
    );
  }

  if (options.receiptCoreDigest) {
    const core = { ...receipt };
    delete core.seal;
    delete core.signature;
    const digest = canonicalDigest(core);
    add(
      "receipt-core-digest",
      digest === options.receiptCoreDigest ? "pass" : "fail",
      digest === options.receiptCoreDigest ? `${digest.slice(0, 16)}…` : "receipt core digest differs from the vector",
    );
  }

  if (receipt.seal) {
    if (!options.sealKeyHex) {
      add("seal", "skip", "receipt is sealed; supply --seal-key to check the HMAC");
    } else {
      const core = { ...receipt };
      delete core.seal;
      delete core.signature;
      const expected = createHmac("sha256", Buffer.from(options.sealKeyHex, "hex"))
        .update(canonical(core), "utf8")
        .digest("hex");
      add(
        "seal",
        expected === receipt.seal.value ? "pass" : "fail",
        expected === receipt.seal.value ? `HMAC-SHA256 valid (key ${receipt.seal.key_id})` : "HMAC does not verify",
      );
    }
  } else {
    add("seal", "skip", "receipt is not sealed");
  }

  if (receipt.signature) {
    try {
      const signable = { ...receipt };
      delete signable.signature;
      const spki = Buffer.concat([
        Buffer.from("302a300506032b6570032100", "hex"), // Ed25519 SPKI prefix
        Buffer.from(receipt.signature.public_key, "hex"),
      ]);
      const key = createPublicKey({ key: spki, format: "der", type: "spki" });
      const ok = cryptoVerify(null, Buffer.from(canonical(signable), "utf8"), key, Buffer.from(receipt.signature.value, "hex"));
      add("signature", ok ? "pass" : "fail", ok ? "Ed25519 signature valid" : "Ed25519 signature does not verify");
    } catch (error) {
      add("signature", "fail", `could not check signature: ${error.message}`);
    }
  } else {
    add("signature", "skip", "receipt is not signed");
  }

  add(
    "limits-disclosure",
    receipt.limits_disclosure && Object.keys(receipt.limits_disclosure).length > 0 ? "pass" : "fail",
    receipt.limits_disclosure && Object.keys(receipt.limits_disclosure).length > 0
      ? "receipt states what Docket could and could not observe"
      : "receipt does not disclose the scope of its evidence",
  );

  return {
    ok: checks.every((check) => check.status !== "fail"),
    docket_number: receipt.docket_number,
    checks,
    failures: checks.filter((check) => check.status === "fail").map((check) => `${check.name}: ${check.detail}`),
  };
}

function render(result, color) {
  const paint = (text, code) => (color ? `\u001b[${code}m${text}\u001b[0m` : text);
  const lines = [];
  const verdict = result.ok ? paint("VERIFIED", "1;32") : paint("NOT VERIFIED", "1;31");
  lines.push(`${verdict}  ${result.docket_number ?? ""}`.trim());
  for (const check of result.checks) {
    const mark = { pass: paint("PASS", "32"), fail: paint("FAIL", "31"), skip: paint("SKIP", "33") }[check.status];
    lines.push(`  ${mark}  ${check.name}: ${check.detail}`);
  }
  return lines.join("\n");
}

// --------------------------------------------------------------------------- //
// CLI
// --------------------------------------------------------------------------- //

function parseArgs(argv) {
  const options = { file: null, ledger: null, json: false, sealKey: null, receiptDigest: null, color: process.stdout.isTTY && !process.env.NO_COLOR };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--ledger") options.ledger = argv[++i];
    else if (arg === "--json") options.json = true;
    else if (arg === "--seal-key") options.sealKey = argv[++i];
    else if (arg === "--receipt-digest") options.receiptDigest = argv[++i];
    else if (arg === "--no-color") options.color = false;
    else if (!arg.startsWith("--") && !options.file) options.file = arg;
  }
  return options;
}

function main(argv) {
  const options = parseArgs(argv);
  if (!options.file) {
    process.stderr.write("usage: node verify-js/verify.mjs <receipt.json> [--ledger <ledger.jsonl>] [--json]\n");
    return 2;
  }
  let receipt;
  try {
    receipt = JSON.parse(readFileSync(options.file, "utf8"));
  } catch (error) {
    process.stderr.write(`verify-js: cannot read receipt: ${error.message}\n`);
    return 2;
  }
  const settings = { receiptCoreDigest: null };
  if (options.ledger) {
    const bytes = readFileSync(options.ledger);
    settings.ledgerFileBytes = bytes;
    settings.ledgerEvents = String(bytes)
      .split("\n")
      .filter((line) => line.trim().length > 0)
      .map((line) => JSON.parse(line));
  }
  if (options.sealKey) {
    try {
      settings.sealKeyHex = readFileSync(options.sealKey, "utf8").trim();
    } catch (error) {
      process.stderr.write(`verify-js: cannot read seal key: ${error.message}\n`);
      return 2;
    }
  }
  if (options.receiptDigest) {
    try {
      settings.receiptCoreDigest = JSON.parse(readFileSync(options.receiptDigest, "utf8")).receipt_core_sha256;
    } catch (error) {
      process.stderr.write(`verify-js: cannot read receipt digest vector: ${error.message}\n`);
      return 2;
    }
  }
  const result = verifyReceipt(receipt, settings);
  if (options.json) {
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
  } else {
    process.stdout.write(`${render(result, options.color)}\n`);
  }
  return result.ok ? 0 : 1;
}

export { canonical, canonicalDigest, formatMicros, merkleRoot, micros, verifyInclusion, verifyLedgerChain, verifyReceipt };

const isMain = process.argv[1] && import.meta.url.endsWith(process.argv[1].split("/").pop());
if (isMain) process.exit(main(process.argv.slice(2)));
