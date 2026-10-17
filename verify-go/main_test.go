package main

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
)

// The vectors are the format's normative examples; these tests hold the primitives to them
// and then exercise the corner cases the corpus only implies.

func loadVector(t *testing.T, name string) any {
	t.Helper()
	data, err := os.ReadFile("../spec/vectors/" + name)
	if err != nil {
		t.Fatalf("reading %s: %v", name, err)
	}
	value, err := parseJSON(data)
	if err != nil {
		t.Fatalf("parsing %s: %v", name, err)
	}
	return value
}

func TestCanonicalVectors(t *testing.T) {
	vector, ok := asObject(loadVector(t, "canonical.json"))
	if !ok {
		t.Fatal("canonical.json is not an object")
	}
	cases, _ := asArray(vector["cases"])
	if len(cases) == 0 {
		t.Fatal("no canonical cases")
	}
	for index, item := range cases {
		entry, _ := asObject(item)
		input, _ := asObject(entry["input"])
		want, _ := asString(entry["canonical"])
		got, err := canonical(input)
		if err != nil {
			t.Fatalf("case %d: %v", index, err)
		}
		if string(got) != want {
			t.Errorf("case %d: canonical form is %s, the vector says %s", index, got, want)
		}
	}
}

func TestMerkleVectors(t *testing.T) {
	vector, _ := asObject(loadVector(t, "merkle.json"))
	leavesRaw, _ := asArray(vector["leaves"])
	leaves := make([]string, 0, len(leavesRaw))
	for _, item := range leavesRaw {
		text, _ := asString(item)
		leaves = append(leaves, text)
	}
	want, _ := asString(vector["root"])
	got, err := merkleRoot(leaves)
	if err != nil {
		t.Fatalf("root: %v", err)
	}
	if got != want {
		t.Fatalf("root is %s, the vector says %s", got, want)
	}
	empty, _ := asString(vector["empty_tree_root"])
	if computed, _ := merkleRoot(nil); computed != empty {
		t.Fatalf("the empty tree hashes to %s, the vector says %s", computed, empty)
	}
	proofs, _ := asArray(vector["proofs"])
	for _, item := range proofs {
		proof, _ := asObject(item)
		index, _ := asInt(proof["index"])
		path, _ := asArray(proof["path"])
		good, err := verifyInclusion(leaves[index], path, want)
		if err != nil || !good {
			t.Fatalf("proof %d does not verify: %v", index, err)
		}
		// A path with one step's side flipped must not verify.
		if len(path) > 0 {
			broken := append([]any{}, path...)
			step, _ := asObject(broken[0])
			flipped := map[string]any{"side": "left", "hash": step["hash"]}
			if step["side"] == "left" {
				flipped["side"] = "right"
			}
			broken[0] = flipped
			if good, _ := verifyInclusion(leaves[index], broken, want); good {
				t.Fatalf("proof %d verified with a flipped side", index)
			}
		}
	}
}

func TestGenesisIsRecomputed(t *testing.T) {
	vector, _ := asObject(loadVector(t, "ledger.json"))
	stated, _ := asString(vector["genesis"])
	if genesis() != stated {
		t.Fatalf("genesis is %s, the vector says %s", genesis(), stated)
	}
}

func TestLedgerVectorReproducesTheChain(t *testing.T) {
	vector, _ := asObject(loadVector(t, "ledger.json"))
	raw, _ := asArray(vector["events"])
	previous := genesis()
	hashes := []string{}
	for index, item := range raw {
		event, _ := asObject(item)
		preimage := map[string]any{}
		for _, member := range []string{"seq", "ts", "type", "data", "prev"} {
			preimage[member] = event[member]
		}
		computed, err := digestOf(preimage)
		if err != nil {
			t.Fatalf("event %d: %v", index, err)
		}
		if computed != toString(event["hash"]) {
			t.Fatalf("event %d hashes to %s, the vector says %s", index, computed, event["hash"])
		}
		if toString(event["prev"]) != previous {
			t.Fatalf("event %d does not link to its predecessor", index)
		}
		previous = computed
		hashes = append(hashes, computed)
	}
	head, _ := asString(vector["head"])
	root, _ := asString(vector["merkle_root"])
	if previous != head {
		t.Fatalf("head is %s, the vector says %s", previous, head)
	}
	if computed, _ := merkleRoot(hashes); computed != root {
		t.Fatalf("root is %s, the vector says %s", computed, root)
	}
}

func TestReceiptVectorVerifies(t *testing.T) {
	result := Verify(loadVector(t, "receipt.json"), "")
	if !result.OK {
		t.Fatalf("the vector does not verify: %v", result.Failures)
	}
	for _, name := range []string{"totals", "token-totals", "by-model-totals", "usage-source-counts", "cost-basis", "assumed-prices"} {
		found := false
		for _, check := range result.Checks {
			if check.Name == name {
				found = true
				if check.Status != "pass" {
					t.Fatalf("%s is %s: %s", name, check.Status, check.Detail)
				}
			}
		}
		if !found {
			t.Fatalf("%s is not reported at all", name)
		}
	}
}

func TestCacheVectorVerifies(t *testing.T) {
	result := Verify(loadVector(t, "cache.json"), "")
	if !result.OK {
		t.Fatalf("the cache vector does not verify: %v", result.Failures)
	}
}

func TestParseRefusals(t *testing.T) {
	cases := []struct {
		name     string
		document string
		wants    string
	}{
		{"duplicate names", `{"a":1,"a":2}`, "twice"},
		{"NaN", `{"a": NaN}`, "NaN"},
		{"Infinity", `{"a": Infinity}`, "Infinity"},
		{"negative Infinity", `{"a": -Infinity}`, "Infinity"},
		{"a byte order mark", "\xEF\xBB\xBF{}", "byte order mark"},
		{"trailing bytes", `{} {}`, "trailing"},
		{"a fractional number", `{"a": 1.5}`, "not integral"},
		{"an unterminated string", `{"a": "x}`, "unterminated"},
		{"a raw control character", "{\"a\": \"\x01\"}", "control"},
		{"a lone surrogate", `{"a": "\udc00"}`, "surrogate"},
		{"an exponent that is integral", `{"a": 4e0}`, ""},
	}
	for _, testCase := range cases {
		_, err := parseJSON([]byte(testCase.document))
		if testCase.wants == "" {
			if err != nil {
				t.Errorf("%s should be read: %v", testCase.name, err)
			}
			continue
		}
		if err == nil {
			t.Errorf("%s was accepted", testCase.name)
			continue
		}
		if !strings.Contains(err.Error(), testCase.wants) {
			t.Errorf("%s: the reason is %q, expected it to name %q", testCase.name, err, testCase.wants)
		}
	}
}

func TestIntegralFloatsAreNormalised(t *testing.T) {
	for _, text := range []string{"5.0", "5e0", "500e-2", "-0"} {
		value, err := parseJSON([]byte(`{"n": ` + text + `}`))
		if err != nil {
			t.Fatalf("%s: %v", text, err)
		}
		object, _ := asObject(value)
		if number, ok := asInt(object["n"]); !ok || number != 0 && number != 5 {
			t.Fatalf("%s parsed as %v", text, object["n"])
		}
	}
}

func TestMoneyRoundTrips(t *testing.T) {
	cases := map[string]string{
		"0.018000":   "0.018000",
		"12.50":      "12.50",
		"1.5":        "1.50", // §3.3: a whole number of cents takes two decimals
		"0":          "0.00",
		"-0.000001":  "-0.000001",
		"999999.999": "999999.999000",
	}
	for input, want := range cases {
		micros, ok := parseMicros(input)
		if !ok {
			t.Fatalf("%s did not parse", input)
		}
		if got := formatMicros(micros); got != want {
			t.Errorf("%s formats as %s, expected %s", input, got, want)
		}
	}
	for _, bad := range []string{"", "1.2345678", "one", "1..2", ".5"} {
		if _, ok := parseMicros(bad); ok {
			t.Errorf("%q parsed as money", bad)
		}
	}
}

func TestPriceTableMatchesInFileOrder(t *testing.T) {
	document := map[string]any{
		"families": []any{
			map[string]any{"pattern": "gpt-6-luna*", "input": "0.10", "cached_input": "0.01", "output": "0.50"},
			map[string]any{"pattern": "gpt-6*", "input": "10.00", "cached_input": "1.00", "output": "50.00"},
			map[string]any{"pattern": "*sonnet*", "input": "3.00", "cached_input": "0.30", "output": "15.00"},
			map[string]any{"pattern": "*", "input": "1.25", "cached_input": "0.125", "output": "10.00"},
		},
	}
	table := buildTable(document)
	if cost, assumed, ok := table.cost("gpt-6-luna-1", 1000, 0, 200); !ok || assumed || cost != 200 {
		t.Fatalf("gpt-6-luna should hit the narrow entry first: %d %v %v", cost, assumed, ok)
	}
	if cost, _, _ := table.cost("claude-sonnet-5", 1000, 0, 200); cost != 6000 {
		t.Fatalf("sonnet costs %d, expected 6000 micro-dollars", cost)
	}
	if cost, assumed, _ := table.cost("unlisted-model", 1000, 0, 200); !assumed || cost != 3250 {
		t.Fatalf("an unlisted model should be priced by the fallback: %d %v", cost, assumed)
	}
}

func TestPercentileIsNearestRank(t *testing.T) {
	values := []int64{12, 100, 2, 5, 9, 30, 1, 7, 11, 3}
	if got := percentile(values, 50); got != 7 {
		t.Fatalf("p50 is %d, expected 7", got)
	}
	if got := percentile(values, 99); got != 100 {
		t.Fatalf("p99 is %d, expected 100", got)
	}
	if got := percentile([]int64{42}, 95); got != 42 {
		t.Fatalf("a single sample's p95 is %d", got)
	}
}

func TestGlobMatch(t *testing.T) {
	cases := []struct {
		pattern, text string
		want          bool
	}{
		{"*sonnet*", "claude-sonnet-5", true},
		{"gpt-6-luna*", "gpt-6-luna-1", true},
		{"gpt-6*", "gpt-6-luna-1", true},
		{"gemini-*-pro*", "gemini-2.5-pro", true},
		{"gemini-*-flash*", "gemini-2.5-pro", false},
		{"*", "", true},
		{"exact", "exact", true},
		{"exact", "exact-not", false},
	}
	for _, testCase := range cases {
		if got := globMatch(testCase.pattern, testCase.text); got != testCase.want {
			t.Errorf("%q against %q is %v", testCase.pattern, testCase.text, got)
		}
	}
}

func TestAMalformedLedgerIsRefusedRatherThanJudged(t *testing.T) {
	directory := t.TempDir()
	path := directory + "/ledger.jsonl"
	if err := os.WriteFile(path, []byte("{\"seq\":0,\"seq\":0}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	result := Verify(loadVector(t, "receipt.json"), path)
	if result.Refusal == "" {
		t.Fatalf("a ledger with a duplicated name was judged: %v", result.Failures)
	}
	if !strings.Contains(result.Refusal, "seq") {
		t.Fatalf("the refusal does not name the repeated key: %s", result.Refusal)
	}
}

func TestAnEmptyLedgerFileIsNotRefused(t *testing.T) {
	directory := t.TempDir()
	path := directory + "/ledger.jsonl"
	if err := os.WriteFile(path, []byte("\n\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	result := Verify(loadVector(t, "receipt.json"), path)
	if result.Refusal != "" {
		t.Fatalf("blank lines were called a malformed ledger: %s", result.Refusal)
	}
}

func TestJSONOutputShape(t *testing.T) {
	result := Verify(loadVector(t, "receipt.json"), "")
	encoded, err := json.Marshal(result)
	if err != nil {
		t.Fatal(err)
	}
	for _, wanted := range []string{`"ok":true`, `"checks":[`, `"name":"receipt-format"`, `"status":"pass"`} {
		if !strings.Contains(string(encoded), wanted) {
			t.Fatalf("the JSON verdict does not contain %s: %s", wanted, encoded)
		}
	}
}

func TestEveryCheckHasOneVerdict(t *testing.T) {
	result := Verify(loadVector(t, "receipt.json"), "")
	seen := map[string]int{}
	for _, check := range result.Checks {
		seen[check.Name]++
	}
	for name, count := range seen {
		if count != 1 {
			t.Fatalf("%s is reported %d times", name, count)
		}
	}
}
