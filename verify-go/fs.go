package main

import "os"

// readFile is the one place the verifier touches the filesystem, so that the rest of the
// package can be tested without one.
func readFile(path string) ([]byte, error) { return os.ReadFile(path) }
