"""Measure Agent Jo's layout in a real browser, on this machine.

    python tools/check_layout.py

Each window is drawn from this folder's own HTML, CSS and JavaScript with
made-up sample data — no server is started, nothing of yours is read, and no
site is visited — and the things that went wrong once are measured where they
went wrong: the task feed over the "needs you" rows, the greeting cut off
under the message box, a phone header that ran off the screen, a drawer under
its own backdrop, an unreadable link, gold buttons in Glass. All of it twice:
in Glass, and in Symbolic Synapse (the skin drawn over Glass), which then has
checks of its own — its fonts come from the app, its lattice is drawn and
stops when it can't be seen, nothing scrolls sideways. Needs Playwright with
Chromium (`python -m pip install playwright && python -m playwright install
chromium`) — Agent Jo itself no longer needs it.
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
    "/api/challenges": {"backlog": 0, "report": {"at": "06:40 UTC", "briefs": [
        {"title": "Ward-level municipal service complaints", "confidence": "high",
         "problem": "Residents report unreliable refuse collection and leaks that are "
                    "hard to quantify and often ignored until elections.",
         "who": "Residents", "data": "Call-centre logs", "first_engagement": "A pilot ward",
         "why_it_might_fail": "Data access", "sources": []},
        {"title": "Clinic queue times", "confidence": "medium",
         "problem": "Patients wait hours with no idea how long.", "who": "Patients",
         "data": "Clinic registers", "first_engagement": "One clinic",
         "why_it_might_fail": "Paper records", "sources": []}]}},
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

        def page(app, w, h, look=None, theme="glass", data=None, motion="no-preference",
                 hook=""):
            static = ROOT / "web/static"
            pg = b.new_page(viewport={"width": w, "height": h}, color_scheme="dark",
                            reduced_motion=motion)
            if look:
                pg.add_init_script(f"localStorage.setItem('agentjo-look', '{look}')")
            # the theme as Settings stores it, past the one-time moves to Glass
            if theme:
                pg.add_init_script(
                    f"localStorage.setItem('agentjo-theme', '{theme}');"
                    "localStorage.setItem('agentjo-theme-glass-migrated', '1');"
                    "localStorage.setItem('agentjo-theme-glass-default', '1');"
                    "localStorage.setItem('agentjo-theme-synapse-default', '1');" + hook)
            elif hook:
                pg.add_init_script(hook)
            pg.route("**/*", _router(static, data or MAIN))
            pg.goto("http://app.test/")
            pg.wait_for_function(
                "document.querySelectorAll('#dashTiles > *').length >= 4"
                if data is None else "typeof state === 'object'", timeout=30000)
            pg.wait_for_timeout(1500)   # not time.sleep: the page only runs while Playwright does
            return pg

        for theme in ("glass", "synapse"):
            print("Agent Jo — " + {"glass": "Glass", "synapse": "Symbolic Synapse"}[theme])
            for w, h, label in ((1440, 900, "desktop"), (1280, 800, "laptop"),
                                (1150, 800, "small laptop, feed minimised")):
                pg = page("main", w, h, theme=theme)
                m = pg.evaluate("""() => {
                  const it = [...document.querySelectorAll('.dash-item')], f = document.getElementById('taskFeed');
                  const fr = f.getBoundingClientRect(), th = document.getElementById('thread');
                  return { cover: it.some(e => { const r = e.getBoundingClientRect();
                             return fr.width > 0 && r.right > fr.left && r.top < fr.bottom && r.bottom > fr.top; }),
                           clipped: th.scrollHeight - th.clientHeight,
                           bar: it.length ? getComputedStyle(it[0]).borderLeftWidth : '' }; }""")
                check(f"{label}: the task feed covers no 'needs you' row", not m["cover"])
                if w >= 1280:
                    check(f"{label}: the welcome keeps Agent Jo's picture", pg.evaluate(
                        "() => document.querySelector('#emptyState .empty-mark').getBoundingClientRect().width > 30"))
                if w >= 1280:
                    check(f"{label}: the greeting fits above the message box", m["clipped"] <= 1)
                pg.close()
            check("the 'needs you' rows have no thick bar", m["bar"] == "1px")

            pg = page("main", 390, 844, theme=theme)
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
            pg.wait_for_timeout(600)
            check("phone: the open drawer sits above its backdrop", pg.evaluate(
                "() => !!document.elementFromPoint(60, 300).closest('#sidebar')"))
            pg.close()

            pg = page("main", 1280, 800, theme=theme)
            pg.evaluate("openSetup()")
            pg.wait_for_timeout(500)
            m = pg.evaluate("""() => {
              const a = document.querySelector('#setupModal a'), btn = document.getElementById('setupSaveBtn');
              return { link: a ? getComputedStyle(a).color : '', button: getComputedStyle(btn).backgroundImage }; }""")
            check("the setup dialog's link is readable",
                  m["link"] and m["link"] not in ("rgb(0, 0, 238)", "rgb(85, 26, 139)"))
            check("primary buttons aren't gold", "224, 176, 88" not in m["button"])
            # one look now (the New/Previous switch was removed): set before
            # the first paint, with the picture on the welcome
            m = pg.evaluate("""() => ({ look: document.documentElement.getAttribute('data-look'),
              mark: document.querySelector('#emptyState .empty-mark').getBoundingClientRect().width,
              sw: !!document.getElementById('model_LOOK') })""")
            check("one look, and the welcome keeps Agent Jo's picture",
                  m["look"] == "previous" and m["mark"] > 30 and not m["sw"])
            pg.close()

            # every panel, at phone width and desktop: nothing runs off the
            # right edge, and opening one closes the one before
            for w in (390, 1280):
                pg = page("main", w, 900, theme=theme)
                ids = pg.evaluate("[...document.querySelectorAll('.foot-btn')].map(b => b.id).filter(Boolean)")
                bad = []
                for i in ids:
                    pg.evaluate("closeAllModals()")
                    pg.evaluate(f"document.getElementById('{i}').click()")
                    pg.wait_for_timeout(350)
                    r = pg.evaluate("""() => {
                      const ms = [...document.querySelectorAll('.modal-backdrop:not(.vt-ghost)')]
                        .filter(x => !x.hidden && getComputedStyle(x).display !== 'none');
                      if (!ms.length) return null;
                      const box = ms[0].querySelector('.modal'), br = box.getBoundingClientRect();
                      const cut = [...box.querySelectorAll('button,input,select,textarea')].filter(e => {
                        const r = e.getBoundingClientRect();
                        return r.width > 0 && (r.right > br.right + 1 || r.left < br.left - 1); });
                      return { n: ms.length, over: box.scrollWidth - box.clientWidth, cut: cut.length }; }""")
                    if r and (r["over"] > 1 or r["cut"] or r["n"] > 1):
                        bad.append(i)
                check(f"{w}px: all {len(ids)} panels fit, one at a time", not bad and len(ids) >= 20)
                if bad:
                    print("      ", ", ".join(bad))
                pg.close()

        # ---- Symbolic Synapse, on its own -------------------------------- #
        print("Agent Jo — Symbolic Synapse, its own parts")
        asked = []
        at_load = ("document.addEventListener('DOMContentLoaded', () => {"
                   " window.__skinAtLoad = document.documentElement.getAttribute('data-skin'); });")
        pg = page("main", 1440, 900, theme="synapse", hook=at_load)
        pg.on("request", lambda r: asked.append(r.url))
        pg.reload()
        pg.wait_for_function("document.querySelectorAll('#dashTiles > *').length >= 4", timeout=30000)
        pg.wait_for_timeout(1500)
        check("the skin is on before the first paint",
              pg.evaluate("window.__skinAtLoad") == "synapse"
              and pg.evaluate("document.documentElement.getAttribute('data-theme')") == "glass")
        fonts = pg.evaluate("""async () => { await document.fonts.ready;
          return [document.fonts.check('600 20px Archivo'),
                  document.fonts.check('400 15px "IBM Plex Sans"'),
                  document.fonts.check('400 12px "IBM Plex Mono"')]; }""")
        woff = [u for u in asked if u.endswith(".woff")]
        check("its typefaces load, from the app itself",
              all(fonts) and woff and all(u.startswith("http://app.test/static/fonts/") for u in woff)
              and not any("fonts.g" in u for u in asked))
        m = pg.evaluate("""() => {
          const c = document.querySelector('#emptyState .syn-lattice');
          if (!c || !c.width) return { drawn: 0 };
          const px = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
          let n = 0; for (let i = 3; i < px.length; i += 4) if (px[i] > 8) n++;
          const img = document.querySelector('#emptyState .empty-mark img').getBoundingClientRect();
          const t = document.getElementById('greeting').getBoundingClientRect();
          const at = (x, y) => document.elementFromPoint(x, y);
          return { drawn: n,
                   picture: at(img.x + img.width / 2, img.y + img.height / 2) === document.querySelector('#emptyState .empty-mark img'),
                   words: at(t.x + t.width / 2, t.y + t.height / 2).id === 'greeting',
                   running: !!_syn.raf }; }""")
        check("the lattice is drawn behind Agent Jo, under the words",
              m["drawn"] > 2000 and m.get("picture") and m.get("words"))
        check("the lattice turns while the welcome is up", m.get("running"))
        pg.evaluate("_syn.input = performance.now() - 10 * 60 * 1000")
        pg.wait_for_timeout(300)
        idle = pg.evaluate("_syn.raf")
        pg.mouse.move(700, 500)
        pg.mouse.move(720, 520)
        pg.wait_for_timeout(300)
        check("left alone it holds still, and a touch of the mouse wakes it",
              not idle and pg.evaluate("!!_syn.raf"))
        pg.evaluate("""() => { state.conversations.unshift({ id: 'x', title: 'x', unsaved: true,
            messages: [{ role: 'user', text: 'hello' }, { role: 'assistant', text: 'hi' }] });
          state.activeId = 'x'; renderConvList(); renderActive(); }""")
        pg.wait_for_timeout(300)
        check("and stops when a conversation replaces it", not pg.evaluate("_syn.raf"))
        pg.evaluate("applyTheme('glass')")
        m = pg.evaluate("""() => ({ skin: document.documentElement.hasAttribute('data-skin'),
          canvas: !!document.querySelector('.syn-lattice'), raf: _syn.raf,
          brand: getComputedStyle(document.querySelector('.brand-co')).display })""")
        check("choosing another theme takes all of it away",
              not m["skin"] and not m["canvas"] and not m["raf"] and m["brand"] == "none")
        pg.evaluate("applyTheme('synapse'); newConversation();")
        pg.wait_for_timeout(400)
        check("and choosing it again brings it back", pg.evaluate(
            "!!document.querySelector('#emptyState .syn-lattice') && !!_syn.raf"))
        m = pg.evaluate("""() => ({ btn: getComputedStyle(document.getElementById('newChatBtn')).backgroundColor,
          wordmark: getComputedStyle(document.querySelector('.brand-co')).display })""")
        check("the site's cyan call to action and its wordmark",
              m["btn"] == "rgb(47, 211, 227)" and m["wordmark"] == "block")
        pg.close()

        # the main look and the default (171): a fresh install opens on it,
        # before the first paint; someone on Glass, the old default, moves to
        # it once, and Glass picked again afterwards stays
        pg = page("main", 1440, 900, theme=None, hook=at_load)
        check("a fresh install opens on Symbolic Synapse, from the first paint",
              pg.evaluate("window.__skinAtLoad") == "synapse"
              and pg.evaluate("[currentTheme(), document.documentElement.getAttribute('data-skin')]")
              == ["synapse", "synapse"])
        pg.close()
        pg = page("main", 1280, 800, theme=None, hook=at_load + (
            "if (!sessionStorage.getItem('seeded')) { sessionStorage.setItem('seeded', '1');"
            " localStorage.setItem('agentjo-theme', 'glass');"
            " localStorage.setItem('agentjo-theme-glass-migrated', '1');"
            " localStorage.setItem('agentjo-theme-glass-default', '1'); }"))
        moved = (pg.evaluate("window.__skinAtLoad"), pg.evaluate("currentTheme()"))
        pg.evaluate("applyTheme('glass')")
        pg.reload()
        pg.wait_for_function("document.querySelectorAll('#dashTiles > *').length >= 4", timeout=30000)
        pg.wait_for_timeout(800)
        kept = (pg.evaluate("window.__skinAtLoad"), pg.evaluate("currentTheme()"))
        check("Glass, the old default, moves to it once; picked again, Glass stays",
              moved == ("synapse", "synapse") and kept == (None, "glass"))
        pg.close()

        # no bar down the left of a rounded card: the brief's tier is a dot
        for theme in ("synapse", "glass"):
            pg = page("main", 1280, 800, theme=theme)
            pg.evaluate("closeAllModals(); document.getElementById('challengesBtn').click()")
            pg.wait_for_function("document.querySelectorAll('#chList .log-row').length >= 2",
                                 timeout=10000)
            pg.wait_for_timeout(400)
            m = pg.evaluate("""() => [...document.querySelectorAll('#chList .log-row')].map(r => {
              const s = getComputedStyle(r), b = getComputedStyle(r, '::before');
              return { shadow: s.boxShadow, left: s.borderLeftWidth, dotW: b.width, dotH: b.height,
                       round: b.borderTopLeftRadius, dot: b.backgroundColor }; })""")
            check(f"{theme}: a brief has no bar down its left edge, just a dot",
                  m and all(x["shadow"] == "none" and x["left"] == "0px" and x["dotW"] == "8px"
                            and x["dotH"] == "8px" and x["round"] == "50%" for x in m)
                  and m[0]["dot"] != m[1]["dot"])
            pg.close()

        # nothing scrolls sideways, at any width, with the lattice in place
        wide = []
        for w, h in ((1440, 900), (1280, 800), (1150, 800), (900, 800), (800, 900), (390, 844)):
            pg = page("main", w, h, theme="synapse")
            m = pg.evaluate("""() => { const t = document.getElementById('thread');
              return [t.scrollWidth - t.clientWidth, document.documentElement.scrollWidth - innerWidth]; }""")
            if max(m) > 1:
                wide.append(f"{w}px")
            pg.close()
        check("nothing scrolls sideways, from phone to desktop", not wide)
        if wide:
            print("      ", ", ".join(wide))

        # with less motion asked for: drawn once, then still
        pg = page("main", 1280, 800, theme="synapse", motion="reduce")
        m = pg.evaluate("""() => { const c = document.querySelector('#emptyState .syn-lattice');
          const px = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
          let n = 0; for (let i = 3; i < px.length; i += 4) if (px[i] > 8) n++;
          return { drawn: n, raf: _syn.raf }; }""")
        check("with reduced motion the lattice is drawn once and stays still",
              m["drawn"] > 2000 and not m["raf"])
        pg.close()

        b.close()
    print(f"\n{sum(results)} of {len(results)} checks passed.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
