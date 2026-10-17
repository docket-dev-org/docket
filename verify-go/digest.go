package main

// Digests, the Merkle tree (spec §5) and the money arithmetic (§3.3).

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"math/big"
	"strings"
)

const genesisSeed = "docket/genesis/v1"

func sha256Hex(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

// genesis is sha256("docket/genesis/v1"), recomputed rather than copied (§4.2).
func genesis() string { return sha256Hex([]byte(genesisSeed)) }

func digestOf(value any) (string, error) {
	encoded, err := canonical(value)
	if err != nil {
		return "", err
	}
	return sha256Hex(encoded), nil
}

// RFC 6962: leaf = sha256(0x00 || leaf-bytes), node = sha256(0x01 || left || right),
// an unpaired node is promoted unchanged, and the empty tree hashes to sha256("").
func leafHash(eventHashHex string) ([]byte, error) {
	raw, err := hex.DecodeString(eventHashHex)
	if err != nil || len(raw) != 32 {
		return nil, fmt.Errorf("a Merkle leaf must be a 32-byte hex digest, got %q", eventHashHex)
	}
	buffer := make([]byte, 0, 33)
	buffer = append(buffer, 0x00)
	buffer = append(buffer, raw...)
	sum := sha256.Sum256(buffer)
	return sum[:], nil
}

func nodeHash(left, right []byte) []byte {
	buffer := make([]byte, 0, 65)
	buffer = append(buffer, 0x01)
	buffer = append(buffer, left...)
	buffer = append(buffer, right...)
	sum := sha256.Sum256(buffer)
	return sum[:]
}

func merkleRoot(eventHashes []string) (string, error) {
	if len(eventHashes) == 0 {
		return sha256Hex(nil), nil
	}
	level := make([][]byte, 0, len(eventHashes))
	for _, hash := range eventHashes {
		leaf, err := leafHash(hash)
		if err != nil {
			return "", err
		}
		level = append(level, leaf)
	}
	for len(level) > 1 {
		next := make([][]byte, 0, (len(level)+1)/2)
		for i := 0; i < len(level); i += 2 {
			if i+1 == len(level) {
				next = append(next, level[i])
				continue
			}
			next = append(next, nodeHash(level[i], level[i+1]))
		}
		level = next
	}
	return hex.EncodeToString(level[0]), nil
}

// verifyInclusion walks a path from the leaf to the root, as §5 defines it.
func verifyInclusion(eventHashHex string, path []any, rootHex string) (bool, error) {
	current, err := leafHash(eventHashHex)
	if err != nil {
		return false, err
	}
	for index, step := range path {
		object, ok := asObject(step)
		if !ok {
			return false, fmt.Errorf("Merkle path step %d is not an object", index)
		}
		side, ok := asString(object["side"])
		if !ok || (side != "left" && side != "right") {
			return false, fmt.Errorf("Merkle path step %d has no side", index)
		}
		siblingHex, ok := asString(object["hash"])
		if !ok {
			return false, fmt.Errorf("Merkle path step %d has no hash", index)
		}
		sibling, err := hex.DecodeString(siblingHex)
		if err != nil || len(sibling) != 32 {
			return false, fmt.Errorf("Merkle path step %d is not a 32-byte digest", index)
		}
		if side == "left" {
			current = nodeHash(sibling, current)
		} else {
			current = nodeHash(current, sibling)
		}
	}
	return hex.EncodeToString(current) == rootHex, nil
}

// --------------------------------------------------------------------------- //
// Money: exact decimals, integer micro-dollars (§3.3)
// --------------------------------------------------------------------------- //

func parseMicros(text string) (int64, bool) {
	if text == "" {
		return 0, false
	}
	negative := strings.HasPrefix(text, "-")
	body := text
	if negative {
		body = body[1:]
	}
	whole, fraction, hasDot := strings.Cut(body, ".")
	if whole == "" || len(whole) > 15 {
		return 0, false
	}
	for _, r := range whole {
		if r < '0' || r > '9' {
			return 0, false
		}
	}
	if hasDot {
		if len(fraction) == 0 || len(fraction) > 6 {
			return 0, false
		}
		for _, r := range fraction {
			if r < '0' || r > '9' {
				return 0, false
			}
		}
	}
	padded := (fraction + "000000")[:6]
	units, ok := new(big.Int).SetString(whole+padded, 10)
	if !ok {
		return 0, false
	}
	if !units.IsInt64() {
		return 0, false
	}
	value := units.Int64()
	if negative {
		value = -value
	}
	return value, true
}

// formatMicros writes the canonical decimal string: two decimals when the amount is a
// whole number of cents, six otherwise (§3.3).
func formatMicros(value int64) string {
	negative := value < 0
	magnitude := value
	if negative {
		magnitude = -magnitude
	}
	whole := magnitude / 1_000_000
	fraction := magnitude % 1_000_000
	text := fmt.Sprintf("%d.%06d", whole, fraction)
	if fraction%10_000 == 0 {
		text = fmt.Sprintf("%d.%02d", whole, fraction/10_000)
	}
	if negative {
		text = "-" + text
	}
	return text
}

// divide rounds a micro-dollar numerator by a denominator, half to even, which is what
// Python's Decimal.quantize does with its default rounding.
func divideHalfEven(numerator, denominator int64) (int64, error) {
	if denominator == 0 {
		return 0, errors.New("division by zero in the price table")
	}
	quotient := numerator / denominator
	remainder := numerator % denominator
	twice := 2 * remainder
	if twice > denominator || (twice == denominator && quotient%2 != 0) {
		quotient++
	}
	return quotient, nil
}
