# Security Policy

## 🔒 Threat Model & Guarantees

AUDAPACK is designed with security and data integrity as foundational requirements:

1. **Loopback Isolation**:
   The HTTP Bridge daemon binds exclusively to `127.0.0.1:17843`. It rejects remote or external connections and never opens public listening sockets.

2. **Secret Token Authentication**:
   Requests from the browser userscript must present a 256-bit secret token via HTTP headers (`X-Bridge-Token`). The token is stored locally in `%LOCALAPPDATA%\AUDAPACK\secrets\bridge_token.txt` with user-restricted NTFS permissions and is never checked into Git.

3. **Path Traversal Protection**:
   All audit ingest paths and project archive destinations are strictly validated against directory traversal escapes (e.g., rejecting `..`, UNC shares, or illegal characters).

4. **Atomic Packaging**:
   Archives are never written directly over existing files. Packaging writes to `.part` files, performs CRC validation via `zipfile.testzip()`, and atomically replaces the target archive upon complete validation.

---

## 🛡️ Reporting Vulnerabilities

If you discover a security vulnerability, please report it responsibly by contacting the maintainers directly or opening a private security advisory on GitHub.

Please include:
- Description of the vulnerability and attack vector.
- Minimal reproducible example or proof-of-concept.
- Expected vs. actual behavior.

We strive to acknowledge reports within 48 hours and release fixes promptly.
