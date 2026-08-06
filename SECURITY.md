# SECURITY

Threat model for a single-user, local-first, offline tool whose input is
untrusted third-party documents.

## What is assumed hostile

**The documents.** Everything imported is untrusted: a `.docx` may be a malformed
zip, a `.md` may contain `<script>`, a PDF may be adversarial. Extraction failures
are caught per file and recorded as `extraction_quality: FAILED` rather than
crashing the run.

**Nothing else.** There is no multi-user model, no authentication, no network
service beyond a loopback GUI. If an attacker already has your device, this tool
is not the weak point.

## Controls in place

| Control | Implementation |
|---|---|
| Output escaping | Every template value is `html.escape`d unless its key is in `RAW_KEYS` (markup this server assembled itself). Substitution is single-pass, so a document containing `{{ field }}` cannot pull another field's value. |
| CSRF | POST requires an `X-Debleeder-CSRF` header matching a token generated per process and printed at startup. |
| Body limit | POST bodies over 256 KiB are rejected with 413 before being read into memory. |
| Security headers | `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, and a CSP with `default-src 'none'`. |
| Bind address | Loopback (`127.0.0.1`) by default, asserted by test. |
| No network in core | Asserted by test: no `requests`, `httpx`, `urllib.request`, or `socket` imports outside the GUI. |
| No external assets | No CDN references. The GUI is stdlib HTTP plus local templates. |
| Journal integrity | SHA-256 hash chain, each record committing to its predecessor. Tampering is detected on open. |
| Tamper response | Bytes preserved, damaged tail quarantined, recovery branch recorded, journal set read-only. Never truncated. |
| Durability | `fsync` on every journal append; `fsync` of files and directories before an atomic `rename` publish. |

## Known weaknesses

- The CSRF token is printed to stdout and has no expiry; anyone who can read the terminal can post decisions.
- No rate limiting. A local process could spam decisions.
- Path handling for template names is not sandboxed, though template names are internal constants and never user-supplied.
- `pypdf` and `python-docx` parse untrusted input in-process. A parser vulnerability is a real exposure; keep them updated.
- The seal writes into `project_root/exports`. No check prevents that path from being a symlink somewhere surprising.

## Reporting

Private alpha. There is no disclosure process yet.
