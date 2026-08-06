"""stdlib HTTP server — phone profile, zero deps.

ALL interpolated values are HTML-escaped. Input to this tool is untrusted
third-party documents; unescaped block content would be stored XSS.
"""

from __future__ import annotations
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import html
import json
import secrets
import urllib.parse
from typing import Callable

from ..core.orchestrator import DebleederCore
from ..core.protocols import Disposition


class HTMXHandler(BaseHTTPRequestHandler):
    """Request handler — delegates to core, renders templates."""

    core: DebleederCore
    templates_dir: Path
    csrf_token: str = ""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._render_index()
        elif path.startswith("/run/"):
            run_id = path.split("/")[2]
            self._render_run_overview(run_id)
        elif path.startswith("/review/"):
            parts = path.split("/")
            run_id, block_id = parts[2], parts[3]
            self._render_review(run_id, block_id)
        else:
            self._404()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path.startswith("/decide/"):
            parts = path.split("/")
            run_id, block_id = parts[2], parts[3]
            self._handle_decide(run_id, block_id)
        elif path.startswith("/suggestion/"):
            parts = path.split("/")
            if len(parts) >= 5:
                self._handle_suggestion(parts[2], parts[3], parts[4])
            else:
                self._404()
        else:
            self._404()

    def _render_index(self):
        self._send_html(self._render_template("base.html", {
            "title": "Debleeder",
            "run_id": "—",
            "stage": "—",
            "content": "<p>Welcome. <a href=\'/run/new\'>Start new run</a> or <a href=\'/runs\'>list runs</a>.</p>"
        }))

    def _render_run_overview(self, run_id: str):
        manifest = self.core.store.get_run(run_id)
        if not manifest:
            self._404()
            return

        undecided = list(self.core.store.get_undecided_blocks(run_id))
        next_block = undecided[0] if undecided else None

        e = lambda v: html.escape(str(v), quote=True)
        block_count = sum(1 for _ in self.core.store.get_blocks(run_id))
        if next_block:
            action = (f'<a href="/review/{e(run_id)}/{e(next_block.instance_id)}" '
                      f'class="btn btn-primary">Continue Review</a>')
        else:
            action = ('<p class="badge badge-low">All decisions complete. '
                      f'<a href="/seal/{e(run_id)}">Seal Run</a></p>')
        content = f"""
        <h2>Run {e(run_id)}</h2>
        <p>Status: {e(manifest.status)} | Profile: {e(manifest.profile.value)} | Blocks: {block_count}</p>
        <p>Undecided: {len(undecided)}</p>
        {action}
        """
        self._send_html(self._render_template("base.html", {
            "title": f"Run {run_id}", "run_id": run_id, "stage": manifest.status,
            "content": content
        }))

    def _render_review(self, run_id: str, block_cid: str):
        block = self.core.store.get_block(run_id, block_cid)
        if not block:
            self._404()
            return

        # Get pair evidence if exists
        pair_id = None
        strength = confidence = "none"
        target_content = source_content = channel_details = ""
        classification = "ordinary"
        recommendation = None
        raw_strength = "none"

        # Find first evidence pair for this block
        for ev in self.core.store.get_all_evidence(run_id):
            if ev.pair_id.startswith(block_cid + ":") or ev.pair_id.endswith(":" + block_cid):
                pair_id = ev.pair_id
                other_cid = ev.pair_id.replace(block_cid + ":", "").replace(":" + block_cid, "")
                other_block = self.core.store.get_block(run_id, other_cid)
                if other_block:
                    target_content = block.normalized_content[:2000]
                    source_content = other_block.normalized_content[:2000]
                    # Get triage for this pair
                    triage = self.core.stage_evaluate(run_id).get(pair_id, {})
                    strength = triage.get("strength", "none")
                    confidence = triage.get("confidence", "none")
                    channel_details = json.dumps(triage.get("channels", {}), indent=2)
                    classification = triage.get("classification", "ordinary")
                    recommendation = triage.get("recommendation")
                    raw_strength = triage.get("raw_strength", strength)
                break

        e = lambda v: html.escape(str(v), quote=True)
        banner = ""
        if classification == "intentional_continuity":
            banner = (
                '<div class="badge badge-low" style="background:#eef;color:#225;'
                'border-color:#ccd;">Intentional continuity — matches a declared '
                f'Reference Document exactly. Evidence retained (raw strength: {e(raw_strength)}). '
                f'Suggested: {e(recommendation or "keep_seeded")}. Nothing has been decided.</div>')
        elif classification == "reference_disagreement":
            rows = []
            for sug in self.core.list_suggestions(run_id):
                if sug.working_instance_id != block_cid:
                    continue
                rows.append(
                    '<div style="border:1px solid #ccc;padding:8px;margin:8px 0;">'
                    f'<p><strong>Differs from reference:</strong> {e(sug.reference_source_name)}</p>'
                    f'<p><em>Current</em><br><code>{e(sug.current_text[:600])}</code></p>'
                    f'<p><em>Reference</em><br><code>{e(sug.proposed_text[:600])}</code></p>'
                    f'<pre style="font-size:11px;overflow:auto;">{e(sug.diff[:1200])}</pre>'
                    f'<p>Status: {e(sug.status)} — approval required, no automatic apply.</p>'
                    + "".join(
                        f'<form method="post" style="display:inline" '
                        f'action="/suggestion/{e(run_id)}/{e(sug.suggestion_id)}/{act}">'
                        f'<input type="hidden" name="csrf_token" value="{e(self.csrf_token)}">'
                        f'<button class="btn" type="submit">{label}</button></form> '
                        for act, label in (("approve", "Approve suggestion"),
                                           ("edit", "Edit suggestion"),
                                           ("reject", "Reject suggestion")))
                    + '</div>')
            banner = ('<div class="badge" style="background:#ffefe0;color:#663300;'
                      'border-color:#e5c9a8;">Differs from a declared Reference Document. '
                      'Evidence is NOT suppressed.</div>' + "".join(rows))

        self._send_html(self._render_template("review.html", {
            "title": f"Review {block_cid[:8]}",
            "reference_banner": banner,
            "run_id": run_id,
            "stage": "CHOOSE",
            "block_cid": block_cid,
            "block_content": block.normalized_content,
            "ordinal": block.addr.ordinal,
            "extraction_quality": block.extraction_quality.value,
            "pair_id": pair_id,
            "strength": strength,
            "confidence": confidence,
            "target_content": target_content,
            "source_content": source_content,
            "channel_details": channel_details,
        }))

    MAX_BODY_BYTES = 256 * 1024

    def _csrf_ok(self, params: dict) -> bool:
        """Accept the token as a header or a form field.

        Form fields are needed because the CSP forbids scripts, so a plain HTML
        form cannot set a custom header.
        """
        header = self.headers.get("X-Debleeder-CSRF", "")
        form = params.get("csrf_token", [""])[0]
        return self.csrf_token in (header, form)

    def _read_form(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None, "Bad Content-Length"
        if length < 0 or length > self.MAX_BODY_BYTES:
            return None, "Request body too large"
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        return urllib.parse.parse_qs(body), None

    def _handle_suggestion(self, run_id: str, suggestion_id: str, action: str):
        """Approve / edit / reject one correction suggestion. Never applies."""
        params, err = self._read_form()
        if err:
            self._send_response(400, err)
            return
        if not self._csrf_ok(params):
            self._send_response(403, "Missing or invalid CSRF token")
            return

        note = params.get("note", [""])[0].strip() or f"{action} via GUI"
        e = lambda v: html.escape(str(v), quote=True)

        if action == "approve":
            result = self.core.approve_suggestion(run_id, suggestion_id, note)
        elif action == "reject":
            result = self.core.reject_suggestion(run_id, suggestion_id, note)
        elif action == "edit":
            # An edited suggestion is still a proposal awaiting approval.
            self.core.store.set_suggestion_status(run_id, suggestion_id, "EDITED", note)
            result = self.core.get_suggestion(run_id, suggestion_id)
        else:
            self._send_response(400, f"Unknown suggestion action: {action!r}")
            return

        if result is None:
            self._404()
            return
        self._send_html(
            f'<div class="badge badge-low">Suggestion {e(suggestion_id)} is now '
            f'{e(result.status)}. Nothing has been applied to any document.</div>')

    def _handle_decide(self, run_id: str, block_cid: str):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._send_response(400, "Bad Content-Length")
            return
        if content_length < 0 or content_length > self.MAX_BODY_BYTES:
            self._send_response(413, "Request body too large")
            return

        body = self.rfile.read(content_length).decode("utf-8", errors="replace")
        params = urllib.parse.parse_qs(body)
        if not self._csrf_ok(params):
            self._send_response(403, "Missing or invalid CSRF token")
            return
        raw_disposition = params.get("disposition", [""])[0]
        reason = params.get("reason", [""])[0].strip()

        if not raw_disposition or not reason:
            self._send_response(400, "Missing disposition or reason")
            return
        try:
            disposition = Disposition(raw_disposition)
        except ValueError:
            self._send_response(400, f"Unknown disposition: {raw_disposition!r}")
            return

        try:
            decision = self.core.record_decision(run_id, block_cid, disposition, reason)
        except Exception as exc:
            self._send_response(500, f"Decision failed: {exc}")
            return

        nxt = self.core.get_next_undecided(run_id)
        e = lambda v: html.escape(str(v), quote=True)
        if nxt is not None:
            next_link = f'<a href="/review/{e(run_id)}/{e(nxt.instance_id)}" class="btn btn-primary">Next</a>'
        else:
            next_link = f'<a href="/run/{e(run_id)}" class="btn btn-primary">All blocks decided</a>'
        self._send_html(
            '<div class="badge badge-low" style="background:#efe;color:#060;border-color:#cfc;">'
            f"Recorded: {e(disposition.value)} (txn #{e(decision.sqlite_txn_id)})</div>{next_link}"
        )

    #: keys whose values are trusted markup assembled by this server itself.
    RAW_KEYS = frozenset({"content", "reference_banner"})

    def _render_template(self, name: str, context: dict) -> str:
        """Render a template and RETURN the html. Does not send.

        Every value is HTML-escaped unless its key is in RAW_KEYS. Substitution
        is single-pass over the template so a document containing "{{ title }}"
        cannot inject another field's value.
        """
        template = (self.templates_dir / name).read_text()

        safe = {}
        for key, value in context.items():
            text = "" if value is None else str(value)
            safe[key] = text if key in self.RAW_KEYS else html.escape(text, quote=True)

        while "{% block content %}" in template:
            start = template.index("{% block content %}")
            end = template.index("{% endblock %}") + len("{% endblock %}")
            template = template[:start] + safe.get("content", "") + template[end:]

        out = []
        i = 0
        while True:
            j = template.find("{{", i)
            if j == -1:
                out.append(template[i:])
                break
            k = template.find("}}", j)
            if k == -1:
                out.append(template[i:])
                break
            out.append(template[i:j])
            key = template[j + 2:k].strip()
            out.append(safe.get(key, ""))
            i = k + 2
        return "".join(out)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'")

    def _send_html(self, markup: str):
        payload = markup.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(payload)

    def _send_response(self, code: int, msg: str):
        payload = msg.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(payload)

    def _404(self):
        self._send_response(404, "Not found")

    def log_message(self, format, *args):
        pass  # suppress default logging


def run_gui_phone(core: DebleederCore, host: str = "127.0.0.1", port: int = 8080):
    """Run phone GUI — stdlib only."""
    templates_dir = Path(__file__).parent / "templates"
    csrf_token = secrets.token_urlsafe(32)
    handler_class = type("Handler", (HTMXHandler,), {
        "core": core,
        "templates_dir": templates_dir,
        "csrf_token": csrf_token,
    })
    server = HTTPServer((host, port), handler_class)
    print(f"Debleeder GUI running at http://{host}:{port}")
    print(f"CSRF token (send as X-Debleeder-CSRF): {csrf_token}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()