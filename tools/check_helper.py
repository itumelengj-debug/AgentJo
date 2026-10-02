"""Check the in-page helper in a real browser, on this machine.

    python tools/check_helper.py

An application is rarely one page: "Apply" loads another page, opens a tab,
or puts the form inside an iframe, and the next step can be drawn without a
page load at all. This walks a small local job site through all of that and
checks the helper is there, and right, at every step. It uses a throwaway
browser profile and a made-up profile, so nothing of yours is touched and no
real site is visited. Needs Playwright with Chromium installed, as the portal
applications do.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["AGENT_HOME"] = tempfile.mkdtemp(prefix="helper-check-")

SITE = {
    "/": '<h1>Data Engineer at Acme</h1><a id="apply" href="/details">Apply for this job</a>',
    "/details": '<a id="go" href="/form" target="_blank">Start application</a>',
    "/form": '<iframe id="f" src="/inner" style="width:900px;height:900px;border:0"></iframe>',
    "/inner": """
<script>
customElements.define("x-field", class extends HTMLElement { connectedCallback() {
  const r = this.attachShadow({mode: "open"});
  r.innerHTML = '<label for="m">Mobile number</label> <input id="m">'; } });
</script>
<form id="one">
 <p><label for="fn">First name *</label> <input id="fn"></p>
 <p><label>Email address <input id="em" type="email"></label></p>
 <div><div class="question-label">Why do you want to work at Acme?</div><textarea id="why"></textarea></div>
 <p><label for="ref">Reference email</label> <input id="ref"></p>
 <x-field></x-field>
 <button type="button" id="next" onclick="history.pushState({}, '', '/inner/2');
   document.getElementById('one').outerHTML = '<form><p><label for=np>Notice period</label> <input id=np></p></form>';">Next</button>
</form>""",
}


class _Site(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = ("<!doctype html><html><body>" + SITE.get(self.path, "")
                + "</body></html>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    import agent.portal as P
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    profile = {"full_name": "Sample Person", "email": "sample@example.com",
               "phone": "+27 82 000 0000", "availability": "30 days"}
    role = {"title": "Data Engineer", "company": "Acme"}
    hints = P.companion_hints([], [{"question": "Why do you want to work here?",
                                    "answer": "Because of the data platform.",
                                    "source": "engine"}], [])
    results = []

    def check(what, ok):
        results.append(bool(ok))
        print(("  \u2713 " if ok else "  \u2717 ") + what)

    d = P.PlaywrightDriver(headless=True)
    try:
        page = d.open(base + "/")
        check("offered on the landing page",
              d.companion(page, P.companion_payload(hints, profile, role)))
        page.click("#apply")
        page.wait_for_load_state("load")
        check("still there after clicking Apply",
              page.evaluate("!!window.__agentJoCompanion"))
        with page.context.expect_page() as info:
            page.click("#go")
        tab = info.value
        tab.wait_for_load_state("load")
        time.sleep(0.5)
        check("there in the tab Apply opened",
              tab.evaluate("!!window.__agentJoCompanion"))
        form = next((f for f in tab.frames if f.url.endswith("/inner")), None)
        check("there inside a form in an iframe",
              form is not None and form.evaluate("!!window.__agentJoCompanion"))
        if form is None:
            return 1

        def says(sel, shadow=False):
            return form.evaluate(
                "([s, sh]) => { const el = sh ? document.querySelector('x-field')"
                ".shadowRoot.querySelector(s) : document.querySelector(s);"
                " const h = window.__agentJoCompanion.explain(el).hit;"
                " return h && h.value || ''; }", [sel, shadow])

        check("'First name *' is answered from your profile", says("#fn") == "Sample")
        check("'Email address' is answered", says("#em") == "sample@example.com")
        check("a question worded differently gets its answer",
              "data platform" in says("#why"))
        check("a reference's email is not given yours", says("#ref") == "")
        check("a field inside a web component is answered",
              says("#m", True) == "+27 82 000 0000")
        box = form.locator("#em").bounding_box()
        tab.mouse.move(box["x"] + 10, box["y"] + box["height"] / 2)
        time.sleep(0.3)
        check("hovering a field shows the cursor and the answer", form.evaluate(
            "() => { const b = document.querySelector('[data-agent-jo=box]');"
            " return b.style.display === 'block' && b.innerText.includes('sample@example.com')"
            " && document.querySelector('[data-agent-jo=cursor]').style.opacity === '1'; }"))
        form.click("#next")
        time.sleep(0.3)
        check("the next step, drawn without a page load, is answered too",
              says("#np") == "30 days")
    except Exception as exc:
        print(f"  \u2717 the check stopped: {type(exc).__name__}: {exc}")
        results.append(False)
    finally:
        try:
            d.close(keep_open=False)
        except Exception:
            pass
        srv.shutdown()
    print(f"\n{sum(results)} of {len(results)} checks passed.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
