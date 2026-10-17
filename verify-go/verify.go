package main

// The verification procedure (spec §8). Each item is reported under the id the registry
// fixes, in the order §8 lists them, so that the first failing check is a stable answer.

import (
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"sort"
	"strings"
)

type Check struct {
	Name   string `json:"name"`
	Status string `json:"status"`
	Detail string `json:"detail"`
}

type Result struct {
	OK           bool     `json:"ok"`
	DocketNumber string   `json:"docket_number,omitempty"`
	Format       string   `json:"format,omitempty"`
	Checks       []Check  `json:"checks"`
	Failures     []string `json:"failures"`
	// Refusal is set when a file could not be read at all — a malformed receipt or a
	// malformed ledger. It is not a verdict: the caller prints the reason on stderr and
	// exits 1 with nothing on stdout (conformance.md §3).
	Refusal string `json:"-"`
}

const receiptFormat = "docket/receipt/v1"

type verifier struct {
	receipt map[string]any
	result  Result
}

func (v *verifier) add(name, status, detail string) {
	if v.result.Checks == nil {
		v.result.Checks = []Check{}
	}
	v.result.Checks = append(v.result.Checks, Check{Name: name, Status: status, Detail: detail})
	if status == "fail" {
		v.result.Failures = append(v.result.Failures, name+": "+detail)
	}
}

func (v *verifier) passed(name string) bool {
	for _, check := range v.result.Checks {
		if check.Name == name {
			return check.Status == "pass"
		}
	}
	return false
}

// Verify runs every item and returns the verdict. A document that is not an object never
// reaches an item: it is refused under `receipt-format` and nothing else is claimed about it.
func Verify(document any, ledgerPath string) Result {
	v := &verifier{result: Result{Checks: []Check{}, Failures: []string{}}}

	receipt, ok := asObject(document)
	if !ok {
		v.add("receipt-format", "fail", fmt.Sprintf("the document is a JSON %s, so there is no receipt to read", typeName(document)))
		return v.finish()
	}
	v.receipt = receipt
	v.add("receipt-format", "pass", "the document is a JSON object")

	tag, _ := asString(receipt["format"])
	if tag != receiptFormat {
		v.add("format", "fail", fmt.Sprintf("format is %q, this reader implements %q", tag, receiptFormat))
		return v.finish()
	}
	v.add("format", "pass", tag)

	if problems := shapeProblems(receipt); len(problems) > 0 {
		v.add("receipt-shape", "fail", strings.Join(prefixN(problems, 3), "; "))
	} else {
		v.add("receipt-shape", "pass", "every member a check reads has the type the format gives it")
	}

	ledgerMeta, _ := asObject(receipt["ledger"])
	if ledgerMeta == nil {
		ledgerMeta = map[string]any{}
	}
	events, chainProblem, fileDigest, ledgerBytes, ledgerRefusal := checkChain(ledgerPath)
	if ledgerRefusal != "" {
		v.result.Refusal = ledgerRefusal
		return v.result
	}
	if ledgerPath == "" {
		v.add("ledger-chain", "skip", "no ledger was supplied; pass --ledger to check the chain")
		v.add("head", "skip", "no ledger was supplied")
		v.add("event-count", "skip", "no ledger was supplied")
		v.add("merkle-root", "skip", "no ledger was supplied")
		v.add("ledger-file-digest", "skip", "no ledger was supplied")
	} else {
		if chainProblem != "" {
			v.add("ledger-chain", "fail", chainProblem)
			v.add("head", "skip", "the chain is broken, so its head is not a fact to compare")
			v.add("event-count", "fail", "the chain is broken; its length is not trustworthy")
			v.add("merkle-root", "fail", "the chain is broken; the root is not trustworthy")
			v.add("ledger-file-digest", "pass", "the file bytes are what they are, whatever they say")
			_ = ledgerBytes
		} else {
			v.add("ledger-chain", "pass", fmt.Sprintf("%d events, every hash and link recomputed", len(events)))
			hashes := make([]string, 0, len(events))
			for _, event := range events {
				hash, _ := asString(event["hash"])
				hashes = append(hashes, hash)
			}
			head := ""
			if len(hashes) > 0 {
				head = hashes[len(hashes)-1]
			}
			if stated, _ := asString(ledgerMeta["head"]); stated == head {
				v.add("head", "pass", "the receipt's head is the ledger's last event hash")
			} else {
				v.add("head", "fail", fmt.Sprintf("receipt head %s… is not the recomputed head %s…", short(stated), short(head)))
			}
			if stated, ok := asInt(ledgerMeta["events"]); ok && stated == int64(len(events)) {
				v.add("event-count", "pass", fmt.Sprintf("%d events", len(events)))
			} else {
				v.add("event-count", "fail", fmt.Sprintf("receipt says %v event(s); the file holds %d", ledgerMeta["events"], len(events)))
			}
			root, err := merkleRoot(hashes)
			if err != nil {
				v.add("merkle-root", "fail", err.Error())
			} else if stated, _ := asString(ledgerMeta["merkle_root"]); stated == root {
				v.add("merkle-root", "pass", "the root recomputed over the event hashes matches")
			} else {
				v.add("merkle-root", "fail", fmt.Sprintf("recomputed %s… != receipt %s…", short(root), short(stated)))
			}
			if stated, _ := asString(ledgerMeta["file_sha256"]); stated == fileDigest {
				v.add("ledger-file-digest", "pass", "the file's SHA-256 matches")
			} else {
				v.add("ledger-file-digest", "fail", fmt.Sprintf("the file hashes to %s…, the receipt says %s…", short(fileDigest), short(stated)))
			}
		}
	}

	appendix, _ := asObject(receipt["appendix"])
	rows := []map[string]any{}
	appendixOK := appendix != nil
	if appendix != nil {
		if raw, ok := asArray(appendix["usage"]); ok {
			for _, item := range raw {
				if row, ok := asObject(item); ok {
					rows = append(rows, row)
				} else {
					appendixOK = false
				}
			}
		} else if appendix["usage"] != nil {
			appendixOK = false
		}
	}
	proofsOmitted := appendix != nil && appendix["proof_included"] == false
	rootHex, _ := asString(ledgerMeta["merkle_root"])

	// Item 4a: a row must be the event the *recomputed* ledger has at that sequence.
	if ledgerPath == "" || chainProblem != "" {
		v.add("usage-rows-rebound", "skip", "no intact ledger was supplied, so the rows cannot be re-bound")
	} else {
		rebind := 0
		failures := 0
		for index, row := range rows {
			seq, ok := asInt(row["seq"])
			if !ok || seq < 0 || seq >= int64(len(events)) {
				failures++
				if failures <= 3 {
					v.add("usage-rows-rebound", "fail", fmt.Sprintf("usage row %d cites seq %v, outside the ledger", index, row["seq"]))
				}
				continue
			}
			stated, _ := asString(row["event_hash"])
			actual, _ := asString(events[seq]["hash"])
			if stated == actual {
				rebind++
			} else {
				failures++
				if failures <= 3 {
					v.add("usage-rows-rebound", "fail", fmt.Sprintf("usage row %d (seq %d) is not the event the ledger holds there", index, seq))
				}
			}
		}
		if failures == 0 {
			v.add("usage-rows-rebound", "pass", fmt.Sprintf("%d usage rows re-bound to the recomputed ledger", rebind))
		} else if failures > 3 {
			v.add("usage-rows-rebound", "fail", fmt.Sprintf("%d of %d usage rows are not the events the ledger holds", failures, len(rows)))
		}
	}

	// Item 4: each row's Merkle path proves it against the receipt's root.
	if proofsOmitted {
		v.add("usage-inclusion-proofs", "pass", "the session states plainly that a large run shipped no proofs")
	} else if !appendixOK {
		v.add("usage-inclusion-proofs", "fail", "the receipt's usage appendix is not readable")
	} else {
		proven, failures, firstDetail := 0, 0, ""
		for index, row := range rows {
			hash, _ := asString(row["event_hash"])
			path, _ := asArray(row["merkle_path"])
			if len(path) == 0 {
				failures++
				if firstDetail == "" {
					firstDetail = fmt.Sprintf("usage row %d carries no inclusion proof", index)
				}
				continue
			}
			good, err := verifyInclusion(hash, path, rootHex)
			if err != nil || !good {
				failures++
				if firstDetail == "" {
					firstDetail = fmt.Sprintf("usage row %d's Merkle path does not reach the receipt's root", index)
				}
				continue
			}
			proven++
		}
		if failures == 0 {
			v.add("usage-inclusion-proofs", "pass", fmt.Sprintf("%d usage rows proven against the Merkle root", proven))
		} else {
			v.add("usage-inclusion-proofs", "fail", firstDetail)
		}
	}

	// Item 5: the arithmetic.
	priceDoc, priceOK := priceDocument(receipt)
	var recomputedCalls int64
	var recomputedCost, recomputedInput, recomputedCached, recomputedOutput int64
	perRowCosts := make([]int64, len(rows))
	assumedModels := map[string]bool{}
	modelSums := map[string]*modelSum{}
	sourceCounts := map[string]int64{"reported": 0, "estimated": 0, "asserted": 0}

	if !appendixOK {
		v.add("cost-recomputation", "fail", "cannot recompute the cost: the receipt's usage appendix is not readable")
	} else if !priceOK {
		v.add("cost-recomputation", "fail", "the receipt does not embed a price table this implementation can read")
	} else {
		table := buildTable(priceDoc)
		mismatch := ""
		for index, row := range rows {
			model, _ := asString(row["model"])
			input, _ := asInt(row["input_tokens"])
			cached, _ := asInt(row["cached_input_tokens"])
			output, _ := asInt(row["output_tokens"])
			cost, assumed, ok := table.cost(model, input, cached, output)
			if !ok {
				mismatch = fmt.Sprintf("usage row %d cites a model the price table cannot price: %q", index, model)
				break
			}
			if assumed {
				assumedModels[model] = true
			}
			perRowCosts[index] = cost
			recomputedCost += cost
			recomputedInput += input
			recomputedCached += cached
			recomputedOutput += output
			recomputedCalls++
			recorded, ok := asDecimalMicros(row["cost_usd"])
			if !ok {
				mismatch = fmt.Sprintf("usage row %d's cost_usd is not a decimal amount", index)
				break
			}
			if recorded != cost {
				seq, _ := asInt(row["seq"])
				mismatch = fmt.Sprintf("per-call cost mismatch at seq %d: recorded %s recomputed %s",
					seq, formatMicros(recorded), formatMicros(cost))
				break
			}
			source, _ := asString(row["usage_source"])
			if source == "reported" || source == "estimated" {
				sourceCounts[source]++
			}
			if row["asserted_by"] == "agent" {
				sourceCounts["asserted"]++
			}
			slot := modelSums[model]
			if slot == nil {
				slot = &modelSum{}
				modelSums[model] = slot
			}
			slot.calls++
			slot.input += input
			slot.cached += cached
			slot.output += output
			slot.cost += cost
		}
		if mismatch != "" {
			v.add("cost-recomputation", "fail", mismatch)
		} else {
			v.add("cost-recomputation", "pass", fmt.Sprintf("%d calls recomputed from token counts", recomputedCalls))
		}
	}

	totals, _ := asObject(receipt["totals"])
	if totals == nil {
		totals = map[string]any{}
	}
	if !appendixOK || !priceOK {
		v.add("totals", "fail", "cannot compare the totals: the arithmetic above could not be recomputed")
		v.add("token-totals", "fail", "cannot compare the totals: the arithmetic above could not be recomputed")
		v.add("cached-totals", "fail", "cannot compare the totals: the arithmetic above could not be recomputed")
		v.add("by-model-totals", "fail", "cannot compare the breakdown: the arithmetic above could not be recomputed")
		v.add("usage-source-counts", "fail", "cannot recount the usage sources")
		v.add("cost-basis", "skip", "cannot compare the basis: the rows could not be read")
	} else {
		problems := []string{}
		if declared, ok := asDecimalMicros(totals["cost_usd"]); !ok {
			problems = append(problems, "totals.cost_usd is not a decimal amount")
		} else if declared != recomputedCost {
			problems = append(problems, fmt.Sprintf("receipt total %s != recomputed %s", formatMicros(declared), formatMicros(recomputedCost)))
		}
		if declared, ok := asInt(totals["calls"]); !ok {
			problems = append(problems, "totals.calls is not a whole number")
		} else if declared != recomputedCalls {
			problems = append(problems, fmt.Sprintf("receipt says %d call(s); the usage appendix holds %d", declared, recomputedCalls))
		}
		v.addStatus("totals", problems, fmt.Sprintf("%d call(s), total %s agrees with recomputation", recomputedCalls, formatMicros(recomputedCost)))

		tokenProblems := []string{}
		for _, pair := range []struct {
			member string
			want   int64
		}{
			{"input_tokens", recomputedInput},
			{"output_tokens", recomputedOutput},
			{"total_tokens", recomputedInput + recomputedOutput},
		} {
			declared, ok := asInt(totals[pair.member])
			if !ok {
				tokenProblems = append(tokenProblems, fmt.Sprintf("totals.%s is not a whole number", pair.member))
			} else if declared != pair.want {
				tokenProblems = append(tokenProblems, fmt.Sprintf("totals.%s says %d; the rows sum to %d", pair.member, declared, pair.want))
			}
		}
		v.addStatus("token-totals", tokenProblems,
			fmt.Sprintf("%d in + %d out = %d tokens", recomputedInput, recomputedOutput, recomputedInput+recomputedOutput))

		if raw, present := totals["cached_input_tokens"]; present {
			declared, ok := asInt(raw)
			if !ok {
				v.add("cached-totals", "fail", "totals.cached_input_tokens is not a whole number")
			} else if declared != recomputedCached {
				v.add("cached-totals", "fail", fmt.Sprintf("totals.cached_input_tokens says %d; the rows sum to %d", declared, recomputedCached))
			} else {
				v.add("cached-totals", "pass", fmt.Sprintf("%d cached input tokens", recomputedCached))
			}
		}

		// Item 5a: the breakdown partitions the rows.
		v.checkBreakdown(totals, modelSums)
		v.checkSourceCounts(totals, sourceCounts)
		v.checkCostBasis(totals, sourceCounts)
	}

	// `assumed-prices` is advisory: a guess is allowed, an undisclosed one is not.
	disclosed := map[string]bool{}
	if list, ok := asArray(totals["assumed_price_models"]); ok {
		for _, item := range list {
			if name, ok := asString(item); ok {
				disclosed[name] = true
			}
		}
	}
	if len(assumedModels) == 0 {
		v.add("assumed-prices", "pass", "every model matched a price entry")
	} else {
		missing := []string{}
		for name := range assumedModels {
			if !disclosed[name] {
				missing = append(missing, name)
			}
		}
		sort.Strings(missing)
		if len(missing) == 0 {
			names := make([]string, 0, len(assumedModels))
			for name := range assumedModels {
				names = append(names, name)
			}
			sort.Strings(names)
			v.add("assumed-prices", "pass", "priced by assumption and disclosed: "+strings.Join(names, ", "))
		} else {
			v.add("assumed-prices", "fail", "priced by assumption but not disclosed in totals.assumed_price_models: "+strings.Join(missing, ", "))
		}
	}

	// Item 6: latency, when present, is the summary of the rows beside it.
	v.checkLatency(rows)

	// Item 8: the seal we cannot check without a key; the signature we can.
	if receipt["seal"] == nil {
		v.add("seal", "skip", "receipt is not sealed")
	} else {
		v.add("seal", "skip", "a seal is an HMAC this implementation was given no key for")
	}
	v.checkSignature()

	// Item 10: the response cache.
	v.checkCache(rows, perRowCosts, ledgerPath, events, chainProblem)

	// Item 9.
	// §9: the statement is a *copy of the policy's own text*, which is an object in the
	// reference tool rather than one sentence — so either shape states it, and an empty one
	// does not.
	disclosure := receipt["limits_disclosure"]
	statesLimits := false
	if text, ok := asString(disclosure); ok {
		statesLimits = strings.TrimSpace(text) != ""
	} else if object, ok := asObject(disclosure); ok {
		statesLimits = len(object) > 0
	} else if list, ok := asArray(disclosure); ok {
		statesLimits = len(list) > 0
	}
	if statesLimits {
		v.add("limits-disclosure", "pass", "receipt states what Docket could and could not observe")
	} else {
		v.add("limits-disclosure", "fail", "the receipt does not state its own limits (§9)")
	}

	return v.finish()
}

func (v *verifier) addStatus(name string, problems []string, passDetail string) {
	if len(problems) > 0 {
		v.add(name, "fail", strings.Join(problems, "; "))
	} else {
		v.add(name, "pass", passDetail)
	}
}

func (v *verifier) finish() Result {
	v.result.OK = len(v.result.Failures) == 0
	if text, ok := asString(v.receipt["docket_number"]); ok {
		v.result.DocketNumber = text
	}
	v.result.Format = receiptFormat
	return v.result
}

type modelSum struct {
	calls, input, cached, output, cost int64
}

func (v *verifier) checkBreakdown(totals map[string]any, sums map[string]*modelSum) {
	raw, present := totals["by_model"]
	if !present {
		v.add("by-model-totals", "fail", "the receipt states no per-model breakdown")
		return
	}
	entries, ok := asArray(raw)
	if !ok {
		v.add("by-model-totals", "fail", fmt.Sprintf("totals.by_model is a %s, not a list", typeName(raw)))
		return
	}
	stated := map[string]map[string]any{}
	problems := []string{}
	for index, item := range entries {
		entry, ok := asObject(item)
		if !ok {
			problems = append(problems, fmt.Sprintf("by_model[%d] is not an object", index))
			continue
		}
		name, _ := asString(entry["model"])
		if _, seen := stated[name]; seen {
			problems = append(problems, fmt.Sprintf("by_model names %q twice", name))
		}
		stated[name] = entry
	}
	names := make([]string, 0, len(sums))
	for name := range sums {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		entry, ok := stated[name]
		if !ok {
			problems = append(problems, fmt.Sprintf("by_model omits %q, which %d usage row(s) used", name, sums[name].calls))
			continue
		}
		for _, pair := range []struct {
			member string
			want   int64
		}{
			{"calls", sums[name].calls},
			{"input_tokens", sums[name].input},
			{"cached_input_tokens", sums[name].cached},
			{"output_tokens", sums[name].output},
		} {
			got, ok := asInt(entry[pair.member])
			if !ok {
				problems = append(problems, fmt.Sprintf("by_model[%q].%s is not a whole number", name, pair.member))
			} else if got != pair.want {
				problems = append(problems, fmt.Sprintf("by_model[%q].%s says %d, the rows sum to %d", name, pair.member, got, pair.want))
			}
		}
		got, ok := asDecimalMicros(entry["cost_usd"])
		if !ok {
			problems = append(problems, fmt.Sprintf("by_model[%q].cost_usd is not a decimal amount", name))
		} else if got != sums[name].cost {
			problems = append(problems, fmt.Sprintf("by_model[%q].cost_usd says %s, the rows sum to %s", name, formatMicros(got), formatMicros(sums[name].cost)))
		}
	}
	extra := []string{}
	for name := range stated {
		if _, ok := sums[name]; !ok {
			extra = append(extra, name)
		}
	}
	sort.Strings(extra)
	for _, name := range extra {
		problems = append(problems, fmt.Sprintf("by_model names %q, which no usage row used", name))
	}
	if len(problems) > 0 {
		v.add("by-model-totals", "fail", strings.Join(prefixN(problems, 4), "; "))
	} else {
		v.add("by-model-totals", "pass", fmt.Sprintf("%d model(s), every count and amount summing to the usage rows", len(sums)))
	}
}

func (v *verifier) checkSourceCounts(totals map[string]any, counts map[string]int64) {
	problems := []string{}
	for _, pair := range []struct {
		member string
		want   int64
	}{
		{"usage_reported_calls", counts["reported"]},
		{"usage_estimated_calls", counts["estimated"]},
		{"asserted_calls", counts["asserted"]},
	} {
		got, ok := asInt(totals[pair.member])
		if !ok {
			problems = append(problems, fmt.Sprintf("totals.%s is not a whole number", pair.member))
		} else if got != pair.want {
			problems = append(problems, fmt.Sprintf("totals.%s says %d; the rows hold %d", pair.member, got, pair.want))
		}
	}
	v.addStatus("usage-source-counts", problems,
		fmt.Sprintf("%d reported, %d estimated, %d asserted by the agent", counts["reported"], counts["estimated"], counts["asserted"]))
}

func (v *verifier) checkCostBasis(totals map[string]any, counts map[string]int64) {
	text, ok := asString(totals["cost_basis"])
	if !ok || strings.TrimSpace(text) == "" {
		v.add("cost-basis", "fail", "totals.cost_basis is not a sentence")
		return
	}
	head := strings.ToLower(strings.TrimSpace(strings.SplitN(strings.TrimSpace(text), ":", 2)[0]))
	reported, estimated := counts["reported"], counts["estimated"]
	switch {
	case head == "quota mode":
		v.add("cost-basis", "skip", "the run claims subscription (quota) pricing, which the receipt states nowhere else")
	case head == "mixed" && !(reported > 0 && estimated > 0):
		v.add("cost-basis", "fail", fmt.Sprintf("cost_basis says mixed, but the rows hold %d reported and %d estimated call(s)", reported, estimated))
	case head == "reported" && estimated > 0:
		v.add("cost-basis", "fail", fmt.Sprintf("cost_basis says reported, but %d row(s) are estimated — usage the provider never reported", estimated))
	case head == "estimated" && reported > 0:
		v.add("cost-basis", "fail", fmt.Sprintf("cost_basis says estimated, but %d row(s) were reported by the provider", reported))
	case head != "mixed" && head != "reported" && head != "estimated":
		v.add("cost-basis", "fail", "cost_basis names no usage class this format defines")
	default:
		v.add("cost-basis", "pass", head+" basis agrees with the usage rows")
	}
}

func (v *verifier) checkLatency(rows []map[string]any) {
	statedRaw, present := v.receipt["latency"]
	if !present {
		v.add("latency-summary", "skip", "the receipt carries no per-call latency")
		return
	}
	stated, ok := asObject(statedRaw)
	if !ok {
		v.add("latency-summary", "fail", fmt.Sprintf("latency is a %s, not an object", typeName(statedRaw)))
		return
	}
	method, _ := asString(stated["method"])
	if method != "nearest-rank" {
		v.add("latency-summary", "fail", fmt.Sprintf("latency.method %q is not a method this implementation knows", method))
		return
	}
	measured := []int64{}
	for index, row := range rows {
		raw, present := row["latency_ms"]
		if !present {
			continue
		}
		value, ok := asInt(raw)
		if !ok || value < 0 {
			v.add("latency-summary", "fail", fmt.Sprintf("usage row %d carries a latency that is not a whole, non-negative millisecond count", index))
			return
		}
		measured = append(measured, value)
	}
	want := map[string]int64{
		"measured_calls": int64(len(measured)),
		"total_rows":     int64(len(rows)),
		"total_ms":       sum(measured),
		"max_ms":         maxOf(measured),
		"p50_ms":         percentile(measured, 50),
		"p95_ms":         percentile(measured, 95),
		"p99_ms":         percentile(measured, 99),
	}
	problems := []string{}
	for _, member := range []string{"measured_calls", "total_rows", "total_ms", "max_ms", "p50_ms", "p95_ms", "p99_ms"} {
		got, ok := asInt(stated[member])
		if !ok {
			problems = append(problems, "latency."+member+" is not a whole number")
		} else if got != want[member] {
			problems = append(problems, fmt.Sprintf("latency.%s says %d, the rows say %d", member, got, want[member]))
		}
	}
	if len(problems) > 0 {
		v.add("latency-summary", "fail", strings.Join(prefixN(problems, 3), "; "))
		return
	}
	v.add("latency-summary", "pass", fmt.Sprintf("p50 %d ms · p99 %d ms over %d measured call(s), nearest-rank",
		want["p50_ms"], want["p99_ms"], len(measured)))
}

func (v *verifier) checkSignature() {
	raw, present := v.receipt["signature"]
	if !present || raw == nil {
		v.add("signature", "skip", "receipt is not signed")
		return
	}
	signature, ok := asObject(raw)
	if !ok {
		v.add("signature", "fail", "signature is not an object")
		return
	}
	algorithm, _ := asString(signature["alg"])
	if algorithm != "ed25519" {
		v.add("signature", "fail", fmt.Sprintf("signature.alg %q is not ed25519", algorithm))
		return
	}
	keyText, _ := asString(signature["public_key"])
	valueText, _ := asString(signature["value"])
	key, err := decodeBase64(keyText)
	if err != nil || len(key) != ed25519.PublicKeySize {
		v.add("signature", "fail", "signature.public_key is not a base64 Ed25519 public key")
		return
	}
	value, err := decodeBase64(valueText)
	if err != nil || len(value) != ed25519.SignatureSize {
		v.add("signature", "fail", "signature.value is not a base64 Ed25519 signature")
		return
	}
	core := map[string]any{}
	for key, item := range v.receipt {
		if key == "signature" {
			continue
		}
		core[key] = item
	}
	encoded, err := canonical(core)
	if err != nil {
		v.add("signature", "fail", "cannot canonicalise the signed core: "+err.Error())
		return
	}
	if ed25519.Verify(ed25519.PublicKey(key), encoded, value) {
		v.add("signature", "pass", "Ed25519 signature verifies against the embedded key")
	} else {
		v.add("signature", "fail", "the Ed25519 signature does not verify against the embedded key")
	}
}

func (v *verifier) checkCache(rows []map[string]any, perRowCosts []int64, ledgerPath string, events []map[string]any, chainProblem string) {
	raw, present := v.receipt["cache"]
	if !present || raw == nil {
		v.add("cache-savings", "skip", "this run had no response cache")
		v.add("cache-hit-origins", "skip", "this run had no response cache")
		v.add("cache-hit-proofs", "skip", "this run had no response cache")
		return
	}
	cache, ok := asObject(raw)
	if !ok {
		v.add("cache-savings", "fail", "cache is not an object")
		v.add("cache-hit-origins", "skip", "cache is not an object")
		v.add("cache-hit-proofs", "skip", "cache is not an object")
		return
	}
	groups, ok := asArray(cache["by_origin"])
	if !ok {
		v.add("cache-savings", "fail", "cache.by_origin is not a list")
		v.add("cache-hit-origins", "skip", "cache.by_origin is not a list")
		v.add("cache-hit-proofs", "skip", "cache.by_origin is not a list")
		return
	}

	priceDoc, priceOK := priceDocument(v.receipt)
	table := buildTable(priceDoc)

	savingProblems := []string{}
	originProblems := []string{}
	proofProblems := []string{}
	var savedTotal, avoidedTokens, hitsTotal int64

	rowsByKey := map[string]map[string]any{}
	for _, row := range rows {
		if key, ok := asString(row["cache_key"]); ok {
			rowsByKey[key] = row
		}
	}

	for index, item := range groups {
		group, ok := asObject(item)
		if !ok {
			savingProblems = append(savingProblems, fmt.Sprintf("by_origin[%d] is not an object", index))
			continue
		}
		key, _ := asString(group["key"])
		model, _ := asString(group["model"])
		usage, _ := asObject(group["usage"])
		input, _ := asInt(usage["input_tokens"])
		cached, _ := asInt(usage["cached_input_tokens"])
		output, _ := asInt(usage["output_tokens"])
		hits, okHits := asInt(group["hits"])
		if !okHits || hits <= 0 {
			savingProblems = append(savingProblems, fmt.Sprintf("by_origin[%d].hits is not a positive count", index))
			continue
		}
		perHit, _, priced := table.cost(model, input, cached, output)
		if !priced {
			savingProblems = append(savingProblems, fmt.Sprintf("by_origin[%d] names a model the price table cannot price: %q", index, model))
			continue
		}
		statedPerHit, ok := asDecimalMicros(group["avoided_usd_per_hit"])
		if !ok || statedPerHit != perHit {
			savingProblems = append(savingProblems, fmt.Sprintf("by_origin[%d].avoided_usd_per_hit is not %s", index, formatMicros(perHit)))
		}
		statedTotal, ok := asDecimalMicros(group["avoided_usd_total"])
		if !ok || statedTotal != perHit*hits {
			savingProblems = append(savingProblems, fmt.Sprintf("by_origin[%d].avoided_usd_total is not %s × %d", index, formatMicros(perHit), hits))
		}
		savedTotal += perHit * hits
		avoidedTokens += (input + output) * hits
		hitsTotal += hits

		// The origin has to be a call this receipt actually records as stored.
		if row, ok := rowsByKey[key]; ok {
			rowModel, _ := asString(row["model"])
			rowInput, _ := asInt(row["input_tokens"])
			rowCached, _ := asInt(row["cached_input_tokens"])
			rowOutput, _ := asInt(row["output_tokens"])
			rowSeq, _ := asInt(row["seq"])
			originSeq, _ := asInt(group["origin_seq"])
			if rowModel != model || rowInput != input || rowCached != cached || rowOutput != output {
				originProblems = append(originProblems, fmt.Sprintf("by_origin[%d] does not match the usage row stored under key %s…", index, short(key)))
			} else if rowSeq != originSeq {
				originProblems = append(originProblems, fmt.Sprintf("by_origin[%d] cites seq %d, but the call it replays is at seq %d", index, originSeq, rowSeq))
			}
		} else if len(rows) > 0 {
			originProblems = append(originProblems, fmt.Sprintf("by_origin[%d].key names no usage row in this receipt", index))
		}
		if ledgerPath != "" && chainProblem == "" {
			seq, ok := asInt(group["origin_seq"])
			if !ok || seq < 0 || seq >= int64(len(events)) {
				originProblems = append(originProblems, fmt.Sprintf("by_origin[%d].origin_seq is outside the ledger", index))
			} else {
				event := events[seq]
				eventType, _ := asString(event["type"])
				data, _ := asObject(event["data"])
				cacheMember, _ := asObject(data["cache"])
				storedKey, _ := asString(cacheMember["key"])
				stored := cacheMember != nil && cacheMember["stored"] == true
				statedHash, _ := asString(group["origin_hash"])
				actualHash, _ := asString(event["hash"])
				if eventType != "model.response" || !stored || storedKey != key || statedHash != actualHash {
					originProblems = append(originProblems, fmt.Sprintf("by_origin[%d] does not cite a model.response recorded as stored under that key", index))
				}
			}
		}

		path, _ := asArray(group["merkle_path"])
		eventHash, _ := asString(group["event_hash"])
		if len(path) == 0 {
			proofProblems = append(proofProblems, fmt.Sprintf("by_origin[%d] carries no inclusion proof", index))
		} else if good, err := verifyInclusion(eventHash, path, rootHexOf(v.receipt)); err != nil || !good {
			proofProblems = append(proofProblems, fmt.Sprintf("by_origin[%d]'s Merkle path does not reach the receipt's root", index))
		}
	}

	if stated, ok := asDecimalMicros(cache["saved_usd"]); !ok || stated != savedTotal {
		savingProblems = append(savingProblems, fmt.Sprintf("cache.saved_usd is not the sum over the groups (%s)", formatMicros(savedTotal)))
	}
	if stated, ok := asInt(cache["avoided_tokens"]); !ok || stated != avoidedTokens {
		savingProblems = append(savingProblems, fmt.Sprintf("cache.avoided_tokens is not %d", avoidedTokens))
	}
	if stated, ok := asInt(cache["hits"]); !ok || stated != hitsTotal {
		savingProblems = append(savingProblems, fmt.Sprintf("cache.hits is not the sum over the groups (%d)", hitsTotal))
	}
	if ledgerPath != "" && chainProblem == "" {
		observed := int64(0)
		ceiling := false
		for _, event := range events {
			if eventType, _ := asString(event["type"]); eventType == "model.cache_hit" {
				observed++
			}
			if eventType, _ := asString(event["type"]); eventType == "note" {
				if data, ok := asObject(event["data"]); ok {
					if flag, ok := asString(data["note"]); ok && flag == "cache_hits_ceiling" {
						ceiling = true
					}
					if flag, ok := asString(data["code"]); ok && flag == "cache_hits_ceiling" {
						ceiling = true
					}
				}
			}
		}
		if stated, ok := asInt(cache["hits"]); !ok || stated != observed {
			savingProblems = append(savingProblems, fmt.Sprintf("cache.hits says %v; the ledger holds %d model.cache_hit events", cache["hits"], observed))
		}
		if reached, ok := cache["hits_ceiling_reached"].(bool); !ok || reached != ceiling {
			savingProblems = append(savingProblems, fmt.Sprintf("cache.hits_ceiling_reached is %v; the ledger says %v", cache["hits_ceiling_reached"], ceiling))
		}
	}
	_ = priceOK
	_ = perRowCosts

	v.addStatus("cache-savings", savingProblems, fmt.Sprintf("%d hit(s) avoided %s", hitsTotal, formatMicros(savedTotal)))
	v.addStatus("cache-hit-origins", originProblems, fmt.Sprintf("%d cache group(s) tied to the calls they replay", len(groups)))
	v.addStatus("cache-hit-proofs", proofProblems, fmt.Sprintf("%d cache group(s) proven against the Merkle root", len(groups)))
}

// --------------------------------------------------------------------------- //
// Shape: the types every other check relies on (§6). Values are not judged here.
// --------------------------------------------------------------------------- //

func shapeProblems(receipt map[string]any) []string {
	problems := []string{}
	for _, member := range []string{"session", "policy", "totals", "appendix", "ledger", "price_table"} {
		if _, ok := asObject(receipt[member]); !ok {
			problems = append(problems, fmt.Sprintf("%s is a %s, not an object", member, typeName(receipt[member])))
		}
	}
	if totals, ok := asObject(receipt["totals"]); ok {
		if _, ok := asDecimalMicros(totals["cost_usd"]); !ok {
			problems = append(problems, "totals.cost_usd is not a decimal amount")
		}
		for _, member := range []string{"calls", "input_tokens", "output_tokens", "total_tokens", "cached_input_tokens"} {
			if raw, present := totals[member]; present {
				if _, ok := asInt(raw); !ok {
					problems = append(problems, fmt.Sprintf("totals.%s is not a whole number", member))
				}
			}
		}
	}
	if appendix, ok := asObject(receipt["appendix"]); ok {
		if raw, present := appendix["usage"]; present {
			if list, ok := asArray(raw); !ok {
				problems = append(problems, fmt.Sprintf("appendix.usage is a %s, not a list", typeName(raw)))
			} else {
				for index, item := range list {
					row, ok := asObject(item)
					if !ok {
						problems = append(problems, fmt.Sprintf("appendix.usage[%d] is not an object", index))
						continue
					}
					if source, ok := asString(row["usage_source"]); !ok || (source != "reported" && source != "estimated") {
						problems = append(problems, fmt.Sprintf("appendix.usage[%d].usage_source is not \"reported\" or \"estimated\"", index))
					}
					for _, member := range []string{"input_tokens", "cached_input_tokens", "output_tokens", "seq"} {
						if _, ok := asInt(row[member]); !ok {
							problems = append(problems, fmt.Sprintf("appendix.usage[%d].%s is not a whole number", index, member))
						}
					}
				}
			}
		}
	}
	if table, ok := asObject(receipt["price_table"]); ok {
		document, ok := asObject(table["document"])
		if !ok || document == nil {
			problems = append(problems, "price_table.document is not an object")
		}
	}
	return problems
}

// --------------------------------------------------------------------------- //
// The ledger (§4): JSONL, one event per line, hash recomputed from five members.
// --------------------------------------------------------------------------- //

func checkChain(ledgerPath string) (events []map[string]any, problem string, fileDigest string, raw []byte, refusal string) {
	if ledgerPath == "" {
		return nil, "", "", nil, ""
	}
	data, err := readFile(ledgerPath)
	if err != nil {
		return nil, "", "", nil, "cannot read the ledger: " + err.Error()
	}
	sum := sha256.Sum256(data)
	fileDigest = hex.EncodeToString(sum[:])
	lines := strings.Split(strings.ReplaceAll(string(data), "\r\n", "\n"), "\n")
	previous := genesis()
	events = []map[string]any{}
	for index, line := range lines {
		trimmed := strings.TrimSpace(line)
		if trimmed == "" {
			continue
		}
		value, err := parseJSON([]byte(trimmed))
		if err != nil {
			// A ledger that cannot be *read* is refused before any check runs: repairing it
			// would mean reporting on a file nobody wrote (conformance.md §3).
			return events, "", fileDigest, data, fmt.Sprintf("the ledger is not JSON this reader accepts (line %d): %v", index+1, err)
		}
		event, ok := asObject(value)
		if !ok {
			return events, "", fileDigest, data, fmt.Sprintf("ledger line %d is not an object", index+1)
		}
		seq, ok := asInt(event["seq"])
		if !ok || seq != int64(len(events)) {
			return events, fmt.Sprintf("broken at event #%d: seq is %v", len(events), event["seq"]), fileDigest, data, ""
		}
		if stated, _ := asString(event["prev"]); stated != previous {
			return events, fmt.Sprintf("broken at event #%d: prev does not link to the event before it", seq), fileDigest, data, ""
		}
		preimage := map[string]any{}
		for _, member := range []string{"seq", "ts", "type", "data", "prev"} {
			item, present := event[member]
			if !present {
				return events, fmt.Sprintf("broken at event #%d: no %s member", seq, member), fileDigest, data, ""
			}
			preimage[member] = item
		}
		computed, err := digestOf(preimage)
		if err != nil {
			return events, fmt.Sprintf("broken at event #%d: %v", seq, err), fileDigest, data, ""
		}
		stated, _ := asString(event["hash"])
		if computed != stated {
			return events, fmt.Sprintf("broken at event #%d: content hash mismatch (event was modified after it was written)", seq), fileDigest, data, ""
		}
		previous = stated
		events = append(events, event)
	}
	return events, "", fileDigest, data, ""
}

// --------------------------------------------------------------------------- //
// Price table: first match in file order, fnmatch-style, case-insensitive.
// --------------------------------------------------------------------------- //

type priceEntry struct {
	patterns []string
	input    int64 // micro-dollars per million tokens
	cached   int64
	output   int64
	fallback bool
}

type priceTable struct{ entries []priceEntry }

func priceDocument(receipt map[string]any) (map[string]any, bool) {
	table, ok := asObject(receipt["price_table"])
	if !ok {
		return nil, false
	}
	document, ok := asObject(table["document"])
	if !ok {
		return nil, false
	}
	if _, ok := asArray(document["families"]); !ok {
		return nil, false
	}
	return document, true
}

func buildTable(document map[string]any) priceTable {
	table := priceTable{}
	entries, _ := asArray(document["families"])
	for _, item := range entries {
		entry, ok := asObject(item)
		if !ok {
			continue
		}
		patterns := []string{}
		for _, part := range strings.Split(toString(entry["pattern"]), "/") {
			part = strings.ToLower(strings.TrimSpace(part))
			if part != "" {
				patterns = append(patterns, part)
			}
		}
		input, ok1 := asDecimalMicros(entry["input"])
		cached, ok2 := asDecimalMicros(entry["cached_input"])
		if !ok2 {
			cached = input
		}
		output, ok3 := asDecimalMicros(entry["output"])
		if !ok1 || !ok3 {
			continue
		}
		fallback := false
		for _, pattern := range patterns {
			if pattern == "*" {
				fallback = true
			}
		}
		table.entries = append(table.entries, priceEntry{patterns: patterns, input: input, cached: cached, output: output, fallback: fallback})
	}
	return table
}

// cost returns the per-call amount in micro-dollars. Prices are per million tokens.
func (t priceTable) cost(model string, input, cached, output int64) (int64, bool, bool) {
	lowered := strings.ToLower(model)
	for _, entry := range t.entries {
		for _, pattern := range entry.patterns {
			if !globMatch(pattern, lowered) {
				continue
			}
			billable := input - cached
			if billable < 0 {
				billable = 0
			}
			numerator := billable*entry.input + cached*entry.cached + output*entry.output
			total, err := divideHalfEven(numerator, 1_000_000)
			if err != nil {
				return 0, false, false
			}
			return total, entry.fallback, true
		}
	}
	return 0, false, false
}

// globMatch is fnmatch without character classes: '*' any run, '?' one rune, else literal.
func globMatch(pattern, text string) bool {
	p := []rune(pattern)
	s := []rune(text)
	pi, si := 0, 0
	star, mark := -1, 0
	for si < len(s) {
		switch {
		case pi < len(p) && (p[pi] == '?' || p[pi] == s[si]):
			pi++
			si++
		case pi < len(p) && p[pi] == '*':
			star = pi
			mark = si
			pi++
		case star >= 0:
			pi = star + 1
			mark++
			si = mark
		default:
			return false
		}
	}
	for pi < len(p) && p[pi] == '*' {
		pi++
	}
	return pi == len(p)
}

// --------------------------------------------------------------------------- //
// Small helpers
// --------------------------------------------------------------------------- //

func typeName(value any) string {
	switch value.(type) {
	case nil:
		return "null"
	case bool:
		return "boolean"
	case string:
		return "string"
	case Num:
		return "number"
	case []any:
		return "list"
	case map[string]any:
		return "object"
	default:
		return fmt.Sprintf("%T", value)
	}
}

func toString(value any) string {
	if text, ok := value.(string); ok {
		return text
	}
	return ""
}

func short(text string) string {
	if len(text) <= 16 {
		return text
	}
	return text[:16] + "…"
}

func prefixN(items []string, limit int) []string {
	if len(items) <= limit {
		return items
	}
	return append(append([]string{}, items[:limit]...), fmt.Sprintf("and %d more", len(items)-limit))
}

func sum(values []int64) int64 {
	total := int64(0)
	for _, value := range values {
		total += value
	}
	return total
}

func maxOf(values []int64) int64 {
	best := int64(0)
	for index, value := range values {
		if index == 0 || value > best {
			best = value
		}
	}
	return best
}

// percentile is the nearest-rank rule of §6.2: rank = ceil(p/100 × n), clamped to [1, n].
func percentile(values []int64, p int) int64 {
	if len(values) == 0 {
		return 0
	}
	sorted := append([]int64{}, values...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i] < sorted[j] })
	n := len(sorted)
	rank := (p*n + 99) / 100
	if rank < 1 {
		rank = 1
	}
	if rank > n {
		rank = n
	}
	return sorted[rank-1]
}

func rootHexOf(receipt map[string]any) string {
	if ledger, ok := asObject(receipt["ledger"]); ok {
		if root, ok := asString(ledger["merkle_root"]); ok {
			return root
		}
	}
	return ""
}

func decodeBase64(text string) ([]byte, error) {
	if decoded, err := base64.StdEncoding.DecodeString(text); err == nil {
		return decoded, nil
	}
	return base64.RawStdEncoding.DecodeString(text)
}
