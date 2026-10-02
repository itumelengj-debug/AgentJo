"""Measure the layout of both apps in a real browser, on this machine.

    python tools/check_layout.py

Each window is drawn from this folder's own HTML, CSS and JavaScript with
made-up sample data — no server is started, nothing of yours is read, and no
site is visited — and the things that went wrong once are measured where they
went wrong: the task feed over the "needs you" rows, the greeting cut off
under the message box, a phone header that ran off the screen, a drawer under
its own backdrop, an unreadable link, gold buttons in Glass, a narrow Jobs
window cut off at the right, a blank drafts pane. Needs Playwright with
Chromium, as the portal applications do. In the standalone Jobs app, only the
Jobs window is checked.
"""
from __future__ import annotations

import json
import mimetypes
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MAIN = {
    "/api/auth/status": {"enabled": False, "authenticated": True},
    "/api/meta": {"name": "Agent Jo", "configured": True, "backend": "claude",
                  "brand": "Sample", "default_engine": "Auto", "start_engine": "Auto",
                  "engines": [{"id": "Auto", "label": "Auto",
                               "model": "smart routing + failover", "local": False}],
                  "voice": {"stt": False}},
    "/api/setup": {"configured": True, "has_key": True, "key_source": "stored", "ollama": True},
    "/api/stats": {"memories": 12, "skills": 3, "learned": {"playbooks": 1, "lessons": 2},
                   "tasks": 1, "documents": 4, "conversations": 3, "schedules": 1,
                   "cost": 1.5, "cost_by_engine": [], "budget": {}},
    "/api/dashboard": {
        "at": "06:40 UTC", "notify_level": "important", "hidden_by_level": 0,
        "dismissed": [], "counts": {"act": 1, "review": 2, "note": 0},
        "all_clear": False, "running": [],
        "needs_you": [
            {"id": "a", "severity": "act", "title": "3 drafts are waiting for you",
             "detail": "They claim more than your profile.", "panel": "jobs", "why": ""},
            {"id": "b", "severity": "review", "title": "A report is ready",
             "detail": "Twelve pages.", "panel": "files", "why": ""},
            {"id": "c", "severity": "review", "title": "A schedule failed",
             "detail": "The engine had no credit.", "panel": "schedules", "why": ""}],
        "tiles": [{"key": k, "label": k.title(), "value": str(v), "sub": "", "tone": "",
                   "panel": "", "title": ""}
                  for k, v in (("spend", 4), ("tasks", 3), ("documents", 42), ("memory", 128))],
        "all_tiles": [], "prefs": {"hidden": [], "order": []}},
    "/api/conversations": {"conversations": []},
    "/api/projects": {"projects": []},
    "/api/tasks": {"tasks": [{"id": "t", "title": "A running task", "status": "running"}]},
    "/api/schedules": {"schedules": []},
}
JOBS = {
    "/api/meta": {"app": "Agent Jo Jobs", "build": "x", "brand": "Sample",
                  "profile_ready": True, "engines": ["Local"], "default_engine": "Auto"},
    "/api/jobs/pipeline": {"stages": {"found": 4, "scored": 3, "drafted": 2, "held": 1,
                                      "applied": 1}, "total": 4, "blocked_at": "held",
                           "why": "1 draft claims more than your profile", "counts": {}},
    "/api/jobs": {"profile": {}, "auto": {"enabled": False}, "follow_ups": [], "daily": False,
                  "roles": [{"key": "r", "title": "Data Engineer", "company": "Sample Co",
                             "stage": "held", "fit": {"score": 80}}]},
    "/api/jobs/claims": {"held": [{"key": "r", "title": "Data Engineer",
                                   "company": "Sample Co", "problems": ["a claim"],
                                   "body": "Dear..."}], "claims": []},
    "/api/jobs/outcomes": {"sent": 0, "replied": 0, "overall": {"low": 0, "high": 0},
                           "findings": [], "by_source": []},
    "/api/jobs/search/config": {"sources": []},
    "/api/jobs/profile": {"profile": {}, "ready": True, "why": ""},
    "/api/jobs/auto/preview": {"sentence": "Nothing would be sent.", "dry_run": True},
    "/api/jobs/sources/suggested": {"suggested": []},
    "/api/jobs/auto/status": {"state": "idle"},
}


def _router(static: Path, data: dict):
    def handle(route):
        path = "/" + route.request.url.split("://", 1)[1].split("/", 1)[1].split("?")[0]
        if path.startswith("/api/"):
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps(data.get(path, {})))
        if path == "/avatar":
            av = ROOT / "agent_avatar.png"
            if av.exists():
                return route.fulfill(status=200, body=av.read_bytes(), content_type="image/png")
        f = static / ("index.html" if path == "/" else path.replace("/static/", "", 1).lstrip("/"))
        if f.is_file():
            return route.fulfill(status=200, body=f.read_bytes(), content_type=(
                mimetypes.guess_type(str(f))[0] or "application/octet-stream"))
        return route.fulfill(status=404, body=b"")
    return handle


def main() -> int:
    from playwright.sync_api import sync_playwright
    results = []

    def check(what, ok):
        results.append(bool(ok))
        print(("  \u2713 " if ok else "  \u2717 ") + what)

    with sync_playwright() as p:
        b = p.chromium.launch()

        def page(app, w, h, look=None):
            static = ROOT / ("web/static" if app == "main" else "web_jobs")
            pg = b.new_page(viewport={"width": w, "height": h}, color_scheme="dark")
            if look:
                pg.add_init_script(f"localStorage.setItem('agentjo-look', '{look}')")
            pg.route("**/*", _router(static, MAIN if app == "main" else JOBS))
            pg.goto("http://app.test/")
            if app == "main":
                pg.wait_for_function(
                    "document.querySelectorAll('#dashTiles > *').length >= 4", timeout=30000)
            else:
                pg.wait_for_function(
                    "document.getElementById('track').children.length === 6", timeout=30000)
            time.sleep(1.5)
            return pg

        if (ROOT / "web" / "static" / "index.html").exists():
            print("Agent Jo")
            for w, h, label in ((1440, 900, "desktop"), (1280, 800, "laptop"),
                                (1150, 800, "small laptop, feed minimised")):
                pg = page("main", w, h)
                m = pg.evaluate("""() => {
                  const it = [...document.querySelectorAll('.dash-item')], f = document.getElementById('taskFeed');
                  const fr = f.getBoundingClientRect(), th = document.getElementById('thread');
                  return { cover: it.some(e => { const r = e.getBoundingClientRect();
                             return fr.width > 0 && r.right > fr.left && r.top < fr.bottom && r.bottom > fr.top; }),
                           clipped: th.scrollHeight - th.clientHeight,
                           bar: it.length ? getComputedStyle(it[0]).borderLeftWidth : '' }; }""")
                check(f"{label}: the task feed covers no 'needs you' row", not m["cover"])
                if w >= 1280:
                    check(f"{label}: no picture on the welcome in the new look", pg.evaluate(
                        "() => document.querySelector('#emptyState .empty-mark').getBoundingClientRect().width === 0"))
                if w >= 1280:
                    check(f"{label}: the greeting fits above the message box", m["clipped"] <= 1)
                pg.close()
            check("the 'needs you' rows have no thick bar", m["bar"] == "1px")

            pg = page("main", 390, 844)
            m = pg.evaluate("""() => {
              const vis = (e) => e && getComputedStyle(e).display !== 'none' && e.getBoundingClientRect().width > 0;
              const pill = document.getElementById('enginePill').getBoundingClientRect();
              return { menus: ['menuBtn', 'navToggle'].filter(id => vis(document.getElementById(id))).length,
                       engineOnScreen: pill.right <= innerWidth && pill.left >= 0,
                       title: document.getElementById('convTitle').getBoundingClientRect().width,
                       feed: vis(document.getElementById('taskFeed')) }; }""")
            check("phone: one menu button", m["menus"] == 1)
            check("phone: the engine picker is on screen", m["engineOnScreen"])
            check("phone: the conversation title shows", m["title"] > 20)
            check("phone: the task feed doesn't cover the page", not m["feed"])
            pg.click("#navToggle")
            time.sleep(0.6)
            check("phone: the open drawer sits above its backdrop", pg.evaluate(
                "() => !!document.elementFromPoint(60, 300).closest('#sidebar')"))
            pg.close()

            pg = page("main", 1280, 800)
            pg.evaluate("openSetup()")
            time.sleep(0.5)
            m = pg.evaluate("""() => {
              const a = document.querySelector('#setupModal a'), btn = document.getElementById('setupSaveBtn');
              return { link: a ? getComputedStyle(a).color : '', button: getComputedStyle(btn).backgroundImage }; }""")
            check("the setup dialog's link is readable",
                  m["link"] and m["link"] not in ("rgb(0, 0, 238)", "rgb(85, 26, 139)"))
            check("Glass primary buttons aren't gold", "224, 176, 88" not in m["button"])
            # switching is remembered, and applied before the first paint
            pg.evaluate("applyLook('previous')")
            pg.reload()
            look = pg.evaluate("document.documentElement.getAttribute('data-look')")
            pg.evaluate("applyLook('new')")
            check("switching to Previous is remembered across a reload", look == "previous")
            pg.close()

            print("Agent Jo, previous look")
            pg = page("main", 1440, 900, look="previous")
            m = pg.evaluate("""() => ({ look: document.documentElement.getAttribute('data-look'),
              mark: document.querySelector('#emptyState .empty-mark').getBoundingClientRect().width,
              bar: getComputedStyle(document.querySelector('.dash-item')).borderLeftWidth })""")
            check("the previous look is the page as it was", m["look"] == "previous" and m["bar"] == "3px")
            check("the previous look keeps Agent Jo's picture", m["mark"] > 30)
            pg.close()

        print("Agent Jo Jobs")
        pg = page("jobs", 683, 768)
        m = pg.evaluate("""() => ({ overflow: document.scrollingElement.scrollWidth - innerWidth,
                                    mainLeft: document.querySelector('.main').getBoundingClientRect().left })""")
        check("a narrow window (half a laptop screen) reflows",
              m["overflow"] <= 1 and m["mainLeft"] < 40)
        check("the Look switch is reachable in a narrow window", pg.evaluate(
            "() => document.getElementById('lookSel').getBoundingClientRect().width > 0"))
        pg.select_option("#lookSel", "previous")
        pg.reload()
        pg.wait_for_function("document.getElementById('track').children.length === 6", timeout=30000)
        m = pg.evaluate("""() => ({ look: document.documentElement.getAttribute('data-look'),
          sel: document.getElementById('lookSel').value,
          reach: document.getElementById('lookSel').getBoundingClientRect().width > 0 })""")
        check("switching the Jobs window to Previous is remembered",
              m["look"] == "previous" and m["sel"] == "previous")
        check("the switch is still reachable in the previous look", m["reach"])
        pg.close()
        pg = page("jobs", 1024, 640)
        pg.evaluate("show('drafts')")
        time.sleep(2.5)
        check("the drafts pane is never blank", "Pick a draft" in pg.evaluate(
            "document.getElementById('draftDetail').innerText"))
        pg.close()
        b.close()
    print(f"\n{sum(results)} of {len(results)} checks passed.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
