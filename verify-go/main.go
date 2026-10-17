// docket-verify: a third, independent implementation of docket/receipt/v1.
//
//	verify-go/docket-verify receipt.json [--ledger ledger.jsonl] [--json]
//
// Exit 0 when the receipt verifies, 1 when it does not, and 1 with an empty stdout and a
// reason on stderr when the document cannot be read at all (spec §3, conformance.md §3).
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"strings"
)

func main() {
	asJSON := flag.Bool("json", false, "print the verdict as JSON")
	ledgerPath := flag.String("ledger", "", "the ledger file to check the receipt against")
	flag.Usage = func() {
		fmt.Fprintf(os.Stderr, "usage: %s <receipt.json> [--ledger <ledger.jsonl>] [--json]\n", os.Args[0])
	}
	// Go's flag package stops at the first positional argument; this CLI takes the file
	// first (as the conformance runner writes it) and the flags anywhere.
	arguments := []string{}
	for _, arg := range os.Args[1:] {
		if arg == "--json" || arg == "-json" || arg == "--ledger" || arg == "-ledger" || strings.HasPrefix(arg, "--ledger=") {
			arguments = append(arguments, arg)
		} else if strings.HasPrefix(arg, "-") {
			arguments = append(arguments, arg)
		} else {
			arguments = append(arguments, arg)
		}
	}
	reordered := []string{}
	positional := []string{}
	for index := 0; index < len(arguments); index++ {
		arg := arguments[index]
		if arg == "--ledger" || arg == "-ledger" {
			reordered = append(reordered, arg)
			if index+1 < len(arguments) {
				reordered = append(reordered, arguments[index+1])
				index++
			}
			continue
		}
		if strings.HasPrefix(arg, "-") {
			reordered = append(reordered, arg)
			continue
		}
		positional = append(positional, arg)
	}
	flag.CommandLine.Parse(append(reordered, positional...))
	if flag.NArg() != 1 {
		flag.Usage()
		os.Exit(2)
	}
	data, err := readFile(flag.Arg(0))
	if err != nil {
		fail(err)
	}
	document, err := parseJSON(data)
	if err != nil {
		// A refusal, not a verdict: empty stdout, reason on stderr, exit 1.
		fail(err)
	}
	result := Verify(document, *ledgerPath)
	if result.Refusal != "" {
		fail(errors.New(result.Refusal))
	}
	if *asJSON {
		encoded, err := json.MarshalIndent(result, "", "  ")
		if err != nil {
			fail(err)
		}
		fmt.Println(string(encoded))
	} else {
		for _, check := range result.Checks {
			fmt.Printf("  %-5s %s: %s\n", statusWord(check.Status), check.Name, check.Detail)
		}
		if result.OK {
			fmt.Printf("\nVERIFIED  %s\n", result.DocketNumber)
		} else {
			fmt.Printf("\nNOT VERIFIED  %d failure(s)\n", len(result.Failures))
		}
	}
	if !result.OK {
		os.Exit(1)
	}
}

func statusWord(status string) string {
	switch status {
	case "pass":
		return "PASS"
	case "fail":
		return "FAIL"
	default:
		return "SKIP"
	}
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, "docket-verify:", err)
	os.Exit(1)
}
