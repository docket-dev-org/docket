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
 *   7. the latency summary, recomputed from the usage rows it claims to summarise
 *   8. the receipt core digest, when a digest vector is supplied
 *   9. seal (HMAC-SHA256) and signature (Ed25519), when present and keys are available
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

// --------------------------------------------------------------------------- //
// Latency summary (§6.3)
//
// The one claim in a receipt that is a measurement rather than arithmetic. A verifier
// cannot re-time a call, so it recomputes the summary from the `latency_ms` values in
// the rows it is already checking, using the method the summary names. "Nearest-rank"
// is spelled out in the spec because p99 over fifteen samples is a different number
// under a different rule, and a reader comparing two receipts must not be comparing
// two definitions.
// --------------------------------------------------------------------------- //

const LATENCY_METHOD = "nearest-rank";
const LATENCY_PERCENTILES = [50, 95, 99];

function isLatency(value) {
  return Number.isInteger(value) && value >= 0;
}

function percentileMs(values, percent) {
  const ordered = [...values].sort((a, b) => a - b);
  const rank = Math.min(Math.max(Math.ceil((percent / 100) * ordered.length), 1), ordered.length);
  return ordered[rank - 1];
}

function latencySummary(values, totalRows) {
  if (values.length === 0) return null;
  const summary = {
    method: LATENCY_METHOD,
    measured_calls: values.length,
    total_rows: totalRows === undefined ? values.length : totalRows,
    total_ms: values.reduce((a, b) => a + b, 0),
    max_ms: Math.max(...values),
  };
  for (const percent of LATENCY_PERCENTILES) summary[`p${percent}_ms`] = percentileMs(values, percent);
  return summary;
}

function latencyProblems(stated, values, totalRows) {
  if (stated === null || typeof stated !== "object" || Array.isArray(stated)) {
    return [`the receipt's latency is not an object: ${Array.isArray(stated) ? "array" : typeof stated}`];
  }
  const problems = [];
  if (stated.method !== LATENCY_METHOD) {
    problems.push(
      `latency.method is ${JSON.stringify(stated.method)}; this verifier recomputes ${JSON.stringify(LATENCY_METHOD)} and will not compare a summary computed some other way`,
    );
  }
  const expected = latencySummary(values, totalRows);
  if (expected === null) {
    problems.push("the receipt states a latency summary but no usage row carries latency_ms");
    return problems;
  }
  const keys = ["measured_calls", "total_rows", "total_ms", "max_ms", ...LATENCY_PERCENTILES.map((p) => `p${p}_ms`)];
  for (const key of keys) {
    if (!(key in stated)) problems.push(`latency.${key} is missing`);
    else if (stated[key] !== expected[key]) problems.push(`latency.${key} is ${JSON.stringify(stated[key])} but the rows give ${JSON.stringify(expected[key])}`);
  }
  for (const key of Object.keys(stated)) {
    if (!(key in expected)) problems.push(`latency.${key} is not a member this format defines`);
  }
  return problems;
}

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
  const recomputed = [];
  let brokenAt = null;
  for (let index = 0; index < events.length; index += 1) {
    const event = events[index];
    const hash = canonicalDigest({
      seq: event.seq,
      ts: event.ts,
      type: event.type,
      data: event.data,
      prev: event.prev,
    });
    recomputed.push(hash);
    if (brokenAt !== null) continue;
    if (event.seq !== index) {
      brokenAt = { index, detail: `sequence gap at #${index} (found seq ${event.seq})` };
    } else if (event.prev !== previous) {
      brokenAt = { index, detail: `broken link at #${index}` };
    } else if (hash !== event.hash) {
      brokenAt = { index, detail: `content hash mismatch at #${index}` };
    }
    // The chain of *recomputed* hashes is what the next link must match: an edit that
    // leaves the stored tail hash intact must not be able to hide downstream.
    previous = hash;
  }
  return {
    ok: brokenAt === null,
    detail: brokenAt ? brokenAt.detail : `${events.length} events verified`,
    brokenAt: brokenAt ? brokenAt.index : null,
    head: previous,
    recomputed,
    genesis,
  };
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
  let chain = null;

  if (!events) {
    add("ledger-chain", "skip", "no ledger supplied; the receipt alone cannot rebuild the chain");
  } else {
    chain = verifyLedgerChain(events);
    add("ledger-chain", chain.ok ? "pass" : "fail", chain.detail);
    const head = chain.head;
    if (!chain.ok) {
      add("head", "skip", `not checked: the chain is broken at event #${chain.brokenAt}`);
    } else {
      add(
        "head",
        head === ledgerMeta.head ? "pass" : "fail",
        head === ledgerMeta.head ? `head matches (${String(head).slice(0, 16)}…)` : "ledger head differs from receipt",
      );
    }
    // Recomputed leaf hashes, not the stored ones: an edit then shows up as a root
    // mismatch even when the stored hashes were left alone.
    const root = merkleRoot(chain.recomputed);
    const rootMatches = root === ledgerMeta.merkle_root && chain.ok;
    add(
      "merkle-root",
      rootMatches ? "pass" : "fail",
      rootMatches
        ? `${root.slice(0, 16)}…`
        : `recomputed ${root.slice(0, 16)}… != receipt ${String(ledgerMeta.merkle_root ?? "").slice(0, 16)}…`,
    );
    add(
      "event-count",
      events.length === ledgerMeta.events ? "pass" : "fail",
      `${events.length} events in ledger, receipt claims ${ledgerMeta.events}`,
    );
  }

  // Two facts, reported together here and separately by the Python verifier: does the row
  // prove membership in the claimed root (the inclusion proof), and is it the event the
  // *recomputed* ledger holds at that sequence (the re-binding)? Both are counted so the
  // detail line can say which one failed — "usage rows do not match the ledger" when the
  // proofs were fine and the ledger was edited sends a reader looking in the wrong place.
  const rows = receipt.appendix?.usage ?? [];
  const rootValue = chain ? merkleRoot(chain.recomputed) : String(ledgerMeta.merkle_root ?? "");
  let proven = 0;
  let unproven = 0;
  let rebound = 0;
  let proofBad = 0;
  let rebindBad = 0;
  let firstBad = null;
  for (const row of rows) {
    let ok = Boolean(row.merkle_path) && verifyInclusion(row.event_hash, row.merkle_path, rootValue);
    if (!ok) proofBad += 1;
    if (ok && events) {
      const bound = chain.recomputed[row.seq];
      if (bound !== row.event_hash) {
        ok = false;
        rebindBad += 1;
      } else rebound += 1;
    }
    if (ok) proven += 1;
    else {
      unproven += 1;
      if (firstBad === null) firstBad = row.seq;
    }
  }
  if (rows.length === 0) {
    add("usage-inclusion-proofs", "pass", "no metered calls in this session");
  } else if (unproven > 0) {
    add(
      "usage-inclusion-proofs",
      "fail",
      events
        ? `${unproven} of ${rows.length} usage rows do not match the recomputed ledger ` +
          `(first: seq ${firstBad}; ${proofBad} failed their inclusion proof, ` +
          `${rebindBad} are not the event the recomputed ledger holds at that sequence)`
        : `${unproven} of ${rows.length} usage rows failed their inclusion proof`,
    );
  } else {
    add(
      "usage-inclusion-proofs",
      "pass",
      rebound
        ? `${proven} usage rows proven against the Merkle root and re-bound to the recomputed ledger`
        : `${proven} usage rows proven against the Merkle root`,
    );
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

  // The latency summary: recomputed from the same rows whose costs were just checked,
  // so a summary that disagrees with its own appendix is caught here rather than
  // believed by the person reading the receipt.
  if (receipt.latency === undefined || receipt.latency === null) {
    add("latency-summary", "skip", "the receipt carries no per-call latency");
  } else if (rows.some((row) => row && "latency_ms" in row && !isLatency(row.latency_ms))) {
    const bad = rows.find((row) => row && "latency_ms" in row && !isLatency(row.latency_ms));
    add(
      "latency-summary",
      "fail",
      `a usage row carries a latency that is not a whole, non-negative millisecond count: ${JSON.stringify(bad.latency_ms)}`,
    );
  } else {
    const measured = rows.filter((row) => row && isLatency(row.latency_ms)).map((row) => row.latency_ms);
    const problems = latencyProblems(receipt.latency, measured, rows.length);
    if (problems.length > 0) {
      add("latency-summary", "fail", problems.slice(0, 3).join("; "));
    } else {
      add(
        "latency-summary",
        "pass",
        `p50 ${receipt.latency.p50_ms} ms · p99 ${receipt.latency.p99_ms} ms over ${measured.length} measured call(s), nearest-rank`,
      );
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

  // ------------------------------------------------------------------------ //
  // The response cache (§6.4)
  //
  // A saving is the easiest number in a receipt to overstate, because nothing about
  // the spend contradicts it. So it is recomputed the same way a metered call's cost
  // is: from the origin call's token counts and the embedded price table. How many
  // times each origin was replayed is a *measurement* — the number of events the
  // boundary wrote — so it is bound to the ledger by the inclusion proof on each
  // group and compared against the ledger's own events, never derived.
  // ------------------------------------------------------------------------ //
  if (receipt.cache === undefined) {
    add("cache-savings", "skip", "this run had no response cache: every call was forwarded");
  } else if (receipt.cache === null || typeof receipt.cache !== "object" || Array.isArray(receipt.cache)) {
    add("cache-savings", "fail", `the receipt's cache member is not an object: ${typeof receipt.cache}`);
  } else {
    const block = receipt.cache;
    const groups = Array.isArray(block.by_origin) ? block.by_origin : null;
    // The same price table the metered calls were checked against, loaded the same way,
    // so a saving and a cost can never be recomputed under two different rules.
    const cachePriceDocument = receipt.price_table?.document;
    const cacheEntries = cachePriceDocument ? loadPrices(cachePriceDocument) : null;
    const savingsProblems = [];
    const originProblems = [];
    let savedMicros = 0n;
    let avoidedTokens = 0;
    let hits = 0;
    let proven = 0;
    let withoutProof = 0;
    let proofFailures = 0;
    const rowsBySeq = new Map();
    for (const row of rows) {
      if (row && typeof row === "object" && Number.isInteger(row.seq)) rowsBySeq.set(row.seq, row);
    }
    if (!groups) {
      savingsProblems.push(`the receipt's cache.by_origin is not a list: ${typeof block.by_origin}`);
    }
    for (const [index, group] of (groups ?? []).entries()) {
      if (!group || typeof group !== "object") {
        savingsProblems.push(`cache group ${index} is not an object: ${typeof group}`);
        continue;
      }
      const count = group.hits;
      if (!Number.isInteger(count) || count < 1) {
        savingsProblems.push(`cache group ${index} does not say how many hits it counts: ${JSON.stringify(count)}`);
        continue;
      }
      const usage = group.usage;
      if (!usage || typeof usage !== "object") {
        savingsProblems.push(`cache group ${index} carries no usage object`);
        continue;
      }
      let perHit;
      try {
        if (!cacheEntries) {
          throw new Error("the receipt does not embed the price table it used");
        }
        perHit = callCost(
          cacheEntries,
          String(group.model ?? ""),
          Number(usage.input_tokens ?? 0),
          Number(usage.cached_input_tokens ?? 0),
          Number(usage.output_tokens ?? 0),
        ).micros;
      } catch (error) {
        savingsProblems.push(`cache group ${index} cannot be priced: ${error.message}`);
        continue;
      }
      const recomputed = formatMicros(perHit);
      if (String(group.avoided_usd_per_hit ?? "") !== recomputed) {
        savingsProblems.push(
          `cache group ${index} claims ${JSON.stringify(group.avoided_usd_per_hit)} saved per hit; the price table says ${recomputed}`,
        );
      }
      savedMicros += perHit * BigInt(count);
      avoidedTokens += (Number(usage.input_tokens ?? 0) + Number(usage.output_tokens ?? 0)) * count;
      hits += count;
      // The origin must be a metered call this receipt already carries, stored under
      // the same key: a hit that names a call the appendix does not have, or one that
      // was never recorded as kept, is a hit with no response behind it.
      const originSeq = group.origin_seq;
      if (!Number.isInteger(originSeq)) {
        originProblems.push(`cache group ${index} does not cite the call it replays: origin_seq ${JSON.stringify(originSeq)}`);
      } else {
        const row = rowsBySeq.get(originSeq);
        if (!row) {
          originProblems.push(
            `cache group ${index} cites origin seq ${originSeq}, which is not a metered call in this receipt's appendix`,
          );
        } else {
          if (row.cache_key !== group.key) {
            originProblems.push(
              `cache group ${index}: the call at seq ${originSeq} is not recorded as a cached response with this key`,
            );
          }
          if (String(row.model ?? "") !== String(group.model ?? "")) {
            originProblems.push(
              `cache group ${index}: replayed model ${JSON.stringify(group.model)} but seq ${originSeq} returned ${JSON.stringify(row.model)}`,
            );
          }
          for (const field of ["input_tokens", "cached_input_tokens", "output_tokens"]) {
            if (Number(usage[field] ?? 0) !== Number(row[field] ?? 0)) {
              originProblems.push(
                `cache group ${index}: ${field} ${JSON.stringify(usage[field])} does not match the call at seq ${originSeq} (${JSON.stringify(row[field])})`,
              );
            }
          }
        }
      }
      // One proof per group, against the same root the usage appendix proves against.
      if (!Array.isArray(group.merkle_path) || group.merkle_path.length === 0) {
        withoutProof += 1;
      } else {
        try {
          if (verifyInclusion(String(group.event_hash), group.merkle_path, String(ledgerMeta.merkle_root ?? ""))) {
            proven += 1;
          } else {
            proofFailures += 1;
          }
        } catch (error) {
          proofFailures += 1;
          savingsProblems.push(`cache group ${index} carries a malformed inclusion proof: ${error.message}`);
        }
      }
    }
    if (groups) {
      if (block.hits !== hits) {
        savingsProblems.push(`cache.hits is ${JSON.stringify(block.hits)}, but the groups below it add up to ${hits}`);
      }
      let declaredSaved = null;
      try {
        declaredSaved = micros(String(block.saved_usd));
      } catch (error) {
        savingsProblems.push(`the receipt's cache.saved_usd is not a decimal amount: ${JSON.stringify(block.saved_usd)}`);
      }
      if (declaredSaved !== null && declaredSaved !== savedMicros) {
        savingsProblems.push(
          `cache.saved_usd is ${JSON.stringify(block.saved_usd)}, but the groups recompute to ${formatMicros(savedMicros)}`,
        );
      }
      if (Number(block.avoided_tokens ?? -1) !== avoidedTokens) {
        savingsProblems.push(
          `cache.avoided_tokens is ${JSON.stringify(block.avoided_tokens)}, but the groups add up to ${avoidedTokens}`,
        );
      }
    }
    if (savingsProblems.length > 0) {
      add("cache-savings", "fail", savingsProblems.slice(0, 3).join("; "));
    } else {
      add("cache-savings", "pass", `${hits} hit(s) recomputed at the origin's price: ${formatMicros(savedMicros)} not spent`);
    }
    if (originProblems.length > 0) {
      add("cache-hit-origins", "fail", originProblems.slice(0, 3).join("; "));
    } else if (hits > 0) {
      add("cache-hit-origins", "pass", "every hit resolves to the stored response it replays");
    }
    if (groups && groups.length > 0) {
      if (groups.length === withoutProof) {
        add("cache-hit-proofs", "skip", "receipt carries a cache summary without inclusion proofs (large session)");
      } else if (proofFailures > 0) {
        add(
          "cache-hit-proofs",
          "fail",
          `${proofFailures} cache group(s) do not prove against the receipt's Merkle root ${String(ledgerMeta.merkle_root ?? "").slice(0, 16)}…`,
        );
      } else {
        add("cache-hit-proofs", "pass", `${proven} cache group(s) proven against the receipt's claimed Merkle root`);
      }
    }
    // With the ledger in hand, the events themselves are the last word on the count.
    if (events) {
      const hitEvents = events.filter((event) => event && event.type === "model.cache_hit");
      if (hitEvents.length !== hits) {
        add(
          "cache-savings",
          "fail",
          `the receipt claims ${hits} cache hit(s); the ledger records ${hitEvents.length}`,
        );
      }
      const tally = new Map();
      for (const event of hitEvents) {
        const data = event.data ?? {};
        const origin = data.origin ?? {};
        const usage = data.usage ?? {};
        const identity = [
          origin.seq,
          origin.hash,
          data.key,
          data.model,
          usage.input_tokens,
          usage.cached_input_tokens,
          usage.output_tokens,
          String(data.avoided_cost_usd ?? ""),
        ].join("\u0000");
        tally.set(identity, (tally.get(identity) ?? 0) + 1);
        const seq = origin.seq;
        if (!Number.isInteger(seq) || seq < 0 || seq >= events.length) {
          add("cache-hit-origins", "fail", `a hit cites origin seq ${JSON.stringify(seq)}, outside the ledger`);
          continue;
        }
        const originEvent = events[seq];
        if (originEvent?.type !== "model.response") {
          add("cache-hit-origins", "fail", `a hit replays seq ${seq}, which is a ${originEvent?.type} event, not a response`);
          continue;
        }
        const meta = originEvent.data?.cache ?? {};
        if (meta.stored !== true) {
          add("cache-hit-origins", "fail", `a hit replays seq ${seq}, which was not recorded as a stored response`);
        } else if (meta.key !== data.key) {
          add(
            "cache-hit-origins",
            "fail",
            `a hit carries a key that does not match the key seq ${seq} was stored under`,
          );
        }
      }
      for (const [index, group] of (groups ?? []).entries()) {
        if (!group || typeof group !== "object") continue;
        const usage = group.usage ?? {};
        const identity = [
          group.origin_seq,
          group.origin_hash,
          group.key,
          group.model,
          usage.input_tokens,
          usage.cached_input_tokens,
          usage.output_tokens,
          String(group.avoided_usd_per_hit ?? ""),
        ].join("\u0000");
        const counted = tally.get(identity) ?? 0;
        if (counted !== group.hits) {
          add(
            "cache-hit-origins",
            "fail",
            `cache group ${index} claims ${group.hits} hit(s) replaying seq ${group.origin_seq}; the ledger records ${counted}`,
          );
        }
      }
      const notePresent = events.some(
        (event) => event && event.type === "note" && event.data && event.data.event === "cache.hits_ceiling_reached",
      );
      if (Boolean(block.hits_ceiling_reached) !== notePresent) {
        add(
          "cache-savings",
          "fail",
          "the receipt and the ledger disagree about whether the cache reached its hit ceiling",
        );
      }
    }
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

export {
  canonical,
  canonicalDigest,
  formatMicros,
  latencyProblems,
  latencySummary,
  merkleRoot,
  micros,
  percentileMs,
  verifyInclusion,
  verifyLedgerChain,
  verifyReceipt,
};

const isMain = process.argv[1] && import.meta.url.endsWith(process.argv[1].split("/").pop());
if (isMain) process.exit(main(process.argv.slice(2)));
