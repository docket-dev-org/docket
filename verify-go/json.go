package main

// A JSON reader and canonical writer for docket/receipt/v1 (spec §3).
//
// `encoding/json` is deliberately not used to *read*: it collapses duplicate object
// names, keeping the last, and §3.1 makes unique names a requirement that must be
// refused rather than repaired. (It is not used to write either: key ordering, escape
// rules and the no-floats rule are all part of the format.)

import (
	"errors"
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"
)

// Num is a JSON number kept as its source text, so that a document can be read
// without passing through binary floating point. Only integral values survive
// canonicalisation (§3.2).
type Num string

var (
	errBOM = errors.New("the file begins with a UTF-8 byte order mark; §3.1 asks for UTF-8 with no BOM")
)

type parser struct {
	data []byte
	pos  int
}

func parseJSON(data []byte) (any, error) {
	if !utf8.Valid(data) {
		return nil, errors.New("the file is not valid UTF-8")
	}
	if len(data) >= 3 && data[0] == 0xEF && data[1] == 0xBB && data[2] == 0xBF {
		return nil, errBOM
	}
	p := &parser{data: data}
	p.skipSpace()
	value, err := p.value(0)
	if err != nil {
		return nil, err
	}
	p.skipSpace()
	if p.pos != len(p.data) {
		return nil, fmt.Errorf("trailing data after the JSON value at byte %d", p.pos)
	}
	return value, nil
}

func (p *parser) skipSpace() {
	for p.pos < len(p.data) {
		switch p.data[p.pos] {
		case ' ', '\t', '\n', '\r':
			p.pos++
		default:
			return
		}
	}
}

func (p *parser) value(depth int) (any, error) {
	if depth > 200 {
		return nil, errors.New("the document nests more than 200 levels deep")
	}
	if p.pos >= len(p.data) {
		return nil, errors.New("unexpected end of input")
	}
	switch c := p.data[p.pos]; c {
	case '{':
		return p.object(depth)
	case '[':
		return p.array(depth)
	case '"':
		return p.str()
	case 't':
		return p.literal("true", true)
	case 'f':
		return p.literal("false", false)
	case 'n':
		return p.literal("null", nil)
	case 'N':
		if strings.HasPrefix(string(p.data[p.pos:]), "NaN") {
			return nil, errors.New("NaN is not JSON: §3.2 requires a document whose numbers all have a JSON spelling")
		}
		return nil, fmt.Errorf("unexpected character %q", c)
	case 'I':
		if strings.HasPrefix(string(p.data[p.pos:]), "Infinity") {
			return nil, errors.New("Infinity is not JSON: §3.2 requires a document whose numbers all have a JSON spelling")
		}
		return nil, fmt.Errorf("unexpected character %q", c)
	case '-':
		if strings.HasPrefix(string(p.data[p.pos:]), "-Infinity") {
			return nil, errors.New("-Infinity is not JSON: §3.2 requires a document whose numbers all have a JSON spelling")
		}
		return p.number()
	default:
		if c >= '0' && c <= '9' {
			return p.number()
		}
		return nil, fmt.Errorf("unexpected character %q at byte %d", c, p.pos)
	}
}

func (p *parser) literal(text string, value any) (any, error) {
	if !strings.HasPrefix(string(p.data[p.pos:]), text) {
		return nil, fmt.Errorf("invalid literal at byte %d", p.pos)
	}
	p.pos += len(text)
	return value, nil
}

func (p *parser) object(depth int) (any, error) {
	p.pos++ // '{'
	out := map[string]any{}
	p.skipSpace()
	if p.pos < len(p.data) && p.data[p.pos] == '}' {
		p.pos++
		return out, nil
	}
	for {
		p.skipSpace()
		if p.pos >= len(p.data) || p.data[p.pos] != '"' {
			return nil, fmt.Errorf("expected an object name at byte %d", p.pos)
		}
		key, err := p.str()
		if err != nil {
			return nil, err
		}
		if _, seen := out[key.(string)]; seen {
			// RFC 8259 §4 leaves a repeated name undefined; §3.1 makes it a refusal.
			return nil, fmt.Errorf("the object names the key %q twice; a verifier must not guess which value was meant", key)
		}
		p.skipSpace()
		if p.pos >= len(p.data) || p.data[p.pos] != ':' {
			return nil, fmt.Errorf("expected ':' after an object name at byte %d", p.pos)
		}
		p.pos++
		p.skipSpace()
		item, err := p.value(depth + 1)
		if err != nil {
			return nil, err
		}
		out[key.(string)] = item
		p.skipSpace()
		if p.pos >= len(p.data) {
			return nil, errors.New("unexpected end of input inside an object")
		}
		if p.data[p.pos] == ',' {
			p.pos++
			continue
		}
		if p.data[p.pos] == '}' {
			p.pos++
			return out, nil
		}
		return nil, fmt.Errorf("expected ',' or '}' at byte %d", p.pos)
	}
}

func (p *parser) array(depth int) (any, error) {
	p.pos++ // '['
	out := []any{}
	p.skipSpace()
	if p.pos < len(p.data) && p.data[p.pos] == ']' {
		p.pos++
		return out, nil
	}
	for {
		p.skipSpace()
		item, err := p.value(depth + 1)
		if err != nil {
			return nil, err
		}
		out = append(out, item)
		p.skipSpace()
		if p.pos >= len(p.data) {
			return nil, errors.New("unexpected end of input inside an array")
		}
		if p.data[p.pos] == ',' {
			p.pos++
			continue
		}
		if p.data[p.pos] == ']' {
			p.pos++
			return out, nil
		}
		return nil, fmt.Errorf("expected ',' or ']' at byte %d", p.pos)
	}
}

func (p *parser) str() (any, error) {
	p.pos++ // '"'
	var b strings.Builder
	for {
		if p.pos >= len(p.data) {
			return nil, errors.New("unterminated string")
		}
		c := p.data[p.pos]
		switch {
		case c == '"':
			p.pos++
			return b.String(), nil
		case c == '\\':
			p.pos++
			if p.pos >= len(p.data) {
				return nil, errors.New("unterminated escape")
			}
			esc := p.data[p.pos]
			p.pos++
			switch esc {
			case '"', '\\', '/':
				b.WriteByte(esc)
			case 'b':
				b.WriteByte('\b')
			case 'f':
				b.WriteByte('\f')
			case 'n':
				b.WriteByte('\n')
			case 'r':
				b.WriteByte('\r')
			case 't':
				b.WriteByte('\t')
			case 'u':
				if p.pos+4 > len(p.data) {
					return nil, errors.New("truncated \\u escape")
				}
				code, err := strconv.ParseUint(string(p.data[p.pos:p.pos+4]), 16, 32)
				if err != nil {
					return nil, errors.New("invalid \\u escape")
				}
				p.pos += 4
				r := rune(code)
				if r >= 0xD800 && r <= 0xDBFF {
					if p.pos+6 > len(p.data) || p.data[p.pos] != '\\' || p.data[p.pos+1] != 'u' {
						return nil, errors.New("a surrogate pair is half-written")
					}
					low, err := strconv.ParseUint(string(p.data[p.pos+2:p.pos+6]), 16, 32)
					if err != nil || low < 0xDC00 || low > 0xDFFF {
						return nil, errors.New("a surrogate pair is half-written")
					}
					p.pos += 6
					r = 0x10000 + ((r - 0xD800) << 10) + (rune(low) - 0xDC00)
				} else if r >= 0xDC00 && r <= 0xDFFF {
					return nil, errors.New("a lone low surrogate is not a character")
				}
				b.WriteRune(r)
			default:
				return nil, fmt.Errorf("invalid escape %q", esc)
			}
		case c < 0x20:
			return nil, fmt.Errorf("a raw control character (0x%02x) in a string", c)
		default:
			b.WriteByte(c)
			p.pos++
		}
	}
}

func (p *parser) number() (any, error) {
	start := p.pos
	if p.pos < len(p.data) && p.data[p.pos] == '-' {
		p.pos++
	}
	for p.pos < len(p.data) && p.data[p.pos] >= '0' && p.data[p.pos] <= '9' {
		p.pos++
	}
	isFloat := false
	if p.pos < len(p.data) && p.data[p.pos] == '.' {
		isFloat = true
		p.pos++
		for p.pos < len(p.data) && p.data[p.pos] >= '0' && p.data[p.pos] <= '9' {
			p.pos++
		}
	}
	if p.pos < len(p.data) && (p.data[p.pos] == 'e' || p.data[p.pos] == 'E') {
		isFloat = true
		p.pos++
		if p.pos < len(p.data) && (p.data[p.pos] == '+' || p.data[p.pos] == '-') {
			p.pos++
		}
		for p.pos < len(p.data) && p.data[p.pos] >= '0' && p.data[p.pos] <= '9' {
			p.pos++
		}
	}
	raw := string(p.data[start:p.pos])
	if raw == "" || raw == "-" {
		return nil, fmt.Errorf("invalid number at byte %d", start)
	}
	if !isFloat {
		if _, err := strconv.ParseInt(raw, 10, 64); err != nil {
			return nil, fmt.Errorf("the integer %s does not fit in 64 bits", raw)
		}
		return Num(raw), nil
	}
	value, err := strconv.ParseFloat(raw, 64)
	if err != nil || math.IsInf(value, 0) || math.IsNaN(value) {
		return nil, fmt.Errorf("the number %s has no finite value", raw)
	}
	if value != math.Trunc(value) {
		// §3.2: a hashed structure must not carry a fractional number.
		return nil, fmt.Errorf("the number %s is not integral; §3.2 requires decimals as strings", raw)
	}
	if math.Abs(value) > 9.007199254740992e15 {
		return nil, fmt.Errorf("the integral float %s is too large to normalise exactly", raw)
	}
	return Num(strconv.FormatInt(int64(value), 10)), nil
}

// --------------------------------------------------------------------------- //
// Canonical form (§3.1–3.2)
// --------------------------------------------------------------------------- //

func canonical(value any) ([]byte, error) {
	var b strings.Builder
	if err := writeCanonical(&b, value); err != nil {
		return nil, err
	}
	return []byte(b.String()), nil
}

func writeCanonical(b *strings.Builder, value any) error {
	switch v := value.(type) {
	case nil:
		b.WriteString("null")
	case bool:
		if v {
			b.WriteString("true")
		} else {
			b.WriteString("false")
		}
	case string:
		writeJSONString(b, v)
	case Num:
		text := string(v)
		if strings.ContainsAny(text, ".eE") {
			return fmt.Errorf("the number %s reached canonical form unnormalised", text)
		}
		b.WriteString(text)
	case int:
		b.WriteString(strconv.Itoa(v))
	case int64:
		b.WriteString(strconv.FormatInt(v, 10))
	case float64:
		if v != math.Trunc(v) {
			return fmt.Errorf("the float %v is not integral", v)
		}
		b.WriteString(strconv.FormatInt(int64(v), 10))
	case []any:
		b.WriteByte('[')
		for i, item := range v {
			if i > 0 {
				b.WriteByte(',')
			}
			if err := writeCanonical(b, item); err != nil {
				return err
			}
		}
		b.WriteByte(']')
	case map[string]any:
		keys := make([]string, 0, len(v))
		for key := range v {
			keys = append(keys, key)
		}
		sort.Strings(keys) // UTF-8 byte order is Unicode code point order
		b.WriteByte('{')
		for i, key := range keys {
			if i > 0 {
				b.WriteByte(',')
			}
			writeJSONString(b, key)
			b.WriteByte(':')
			if err := writeCanonical(b, v[key]); err != nil {
				return err
			}
		}
		b.WriteByte('}')
	default:
		return fmt.Errorf("cannot canonicalise a %T", value)
	}
	return nil
}

// writeJSONString matches Python's json.dumps(..., ensure_ascii=False): only the two
// mandatory escapes and the five short control escapes; everything else literal UTF-8.
func writeJSONString(b *strings.Builder, text string) {
	b.WriteByte('"')
	for _, r := range text {
		switch r {
		case '"':
			b.WriteString("\\\"")
		case '\\':
			b.WriteString("\\\\")
		case '\b':
			b.WriteString("\\b")
		case '\f':
			b.WriteString("\\f")
		case '\n':
			b.WriteString("\\n")
		case '\r':
			b.WriteString("\\r")
		case '\t':
			b.WriteString("\\t")
		default:
			if r < 0x20 {
				b.WriteString(fmt.Sprintf("\\u%04x", r))
			} else {
				b.WriteRune(r)
			}
		}
	}
	b.WriteByte('"')
}

// --------------------------------------------------------------------------- //
// Accessors: every one of them takes untrusted input and says so in its signature
// --------------------------------------------------------------------------- //

func asObject(value any) (map[string]any, bool) {
	object, ok := value.(map[string]any)
	return object, ok
}

func asArray(value any) ([]any, bool) {
	array, ok := value.([]any)
	return array, ok
}

func asString(value any) (string, bool) {
	text, ok := value.(string)
	return text, ok
}

// asInt accepts a JSON integer and an integral float (§3.2 says a float with an
// integral value is normalised), and nothing else — a bool is not a number.
func asInt(value any) (int64, bool) {
	number, ok := value.(Num)
	if !ok {
		return 0, false
	}
	parsed, err := strconv.ParseInt(string(number), 10, 64)
	if err != nil {
		return 0, false
	}
	return parsed, true
}

func asDecimalMicros(value any) (int64, bool) {
	text, ok := asString(value)
	if !ok {
		return 0, false
	}
	return parseMicros(text)
}
