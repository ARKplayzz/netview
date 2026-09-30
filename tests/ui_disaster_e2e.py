"""End-to-end UI test of the disaster trace in a real (headless) Chrome.

    py -3.11 netview/tests/ui_disaster_e2e.py [--run]

Drives the actual page over the DevTools protocol: picks From/To, opens the
Disaster panel, ticks a node, checks the source can't be ticked, presses Run
and checks the in-page confirmation, and - with --run - confirms and waits for
the real disaster trace, then screenshots the result section. Screenshots are
written next to this file (ui_*.png). Needs NetView on :5057 and Chrome.
"""
import base64
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websocket

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9223
OUT = Path(__file__).resolve().parent
SRC, DST, IP, VICTIM = "Finance PC", "ISP-1 (Terrestrial Fibre)", "100.11.0.1", "EdgeRouter-HQ-2"


class Page:
    def __init__(self):
        tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json").read())
        tab = next(t for t in tabs if t.get("type") == "page")
        self.ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=60,
                                              origin=f"http://127.0.0.1:{PORT}")
        self.n = 0

    def cmd(self, method, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.n:
                if "error" in m:
                    raise RuntimeError(m["error"])
                return m.get("result", {})

    def js(self, expr):
        r = self.cmd("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if r.get("exceptionDetails"):
            raise RuntimeError(r["exceptionDetails"].get("exception", {}).get("description", r["exceptionDetails"]))
        return r["result"].get("value")

    def shot(self, name, selector=None):
        clip = None
        if selector:
            b = self.js(f"(()=>{{const e=document.querySelector({json.dumps(selector)}); if(!e) return null; "
                        f"e.scrollIntoView(); const r=e.getBoundingClientRect(); "
                        f"return {{x:r.x+scrollX,y:r.y+scrollY,width:r.width,height:Math.min(r.height,1800)}}}})()")
            if b:
                clip = dict(b, scale=1)
        img = self.cmd("Page.captureScreenshot", format="png", captureBeyondViewport=True, **({"clip": clip} if clip else {}))
        p = OUT / f"ui_{name}.png"
        p.write_bytes(base64.b64decode(img["data"]))
        return p


def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}{('  - ' + str(detail)) if detail else ''}", flush=True)
    return ok


def main():
    run = "--run" in sys.argv
    prof = tempfile.mkdtemp()
    chrome = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={PORT}", f"--user-data-dir={prof}",
                               "--window-size=1400,1000", "--no-first-run", f"--remote-allow-origins=http://127.0.0.1:{PORT}",
                               "http://127.0.0.1:5057/"])
    ok = True
    try:
        time.sleep(4)
        pg = Page()
        pg.cmd("Page.enable")
        pg.cmd("Emulation.setDeviceMetricsOverride", width=1400, height=1000, deviceScaleFactor=1, mobile=False)
        for _ in range(60):
            if pg.js("typeof NODES !== 'undefined' && NODES.length > 0 && typeof disasterPanel === 'function'"):
                break
            time.sleep(2)
        ok &= check("page loaded with inventory and disaster.js", pg.js("NODES.length") > 0)
        pg.js(f"setTraceEnds({json.dumps(SRC)}, {json.dumps(DST)}); trace.ip = {json.dumps(IP)}; trace.capture = false; "
              "renderPathHead(); renderPathBody(); true")
        ok &= check("Disaster button present", pg.js("[...document.querySelectorAll('#path-head button')].some(b=>b.textContent.startsWith('Disaster'))"))
        pg.js("[...document.querySelectorAll('#path-head button')].find(b=>b.textContent.startsWith('Disaster')).click(); true")
        ok &= check("panel opens", pg.js("!!document.querySelector('.dz-panel')"))
        n_items = pg.js("document.querySelectorAll('.dz-item').length")
        n_up = pg.js("NODES.filter(n=>!n.ghost && n.up).length")
        ok &= check("lists every running node", n_items == n_up, f"{n_items} listed, {n_up} running")
        src_disabled = pg.js(f"[...document.querySelectorAll('.dz-item')].find(l=>l.textContent.includes({json.dumps(SRC)}) && l.textContent.includes('source')).querySelector('input').disabled")
        ok &= check("source cannot be ticked", src_disabled is True)
        pg.js("const f=document.getElementById('dz-filter'); f.value='EdgeRouter-HQ'; f.dispatchEvent(new Event('input')); true")
        ok &= check("filter narrows the list", pg.js("document.querySelectorAll('.dz-item').length") == 2,
                    pg.js("[...document.querySelectorAll('.dz-item')].map(l=>l.textContent.trim())"))
        pg.js(f"[...document.querySelectorAll('.dz-item')].find(l=>l.textContent.trim().startsWith({json.dumps(VICTIM)})).querySelector('input').click(); true")
        ok &= check("tick adds a chip", pg.js(f"[...document.querySelectorAll('.dz-chip')].some(c=>c.textContent.includes({json.dumps(VICTIM)}))"))
        ok &= check("Run button relabelled", pg.js("[...document.querySelectorAll('#path-head button')].some(b=>b.textContent==='Run disaster trace')"))
        ok &= check("Repeat disabled while a disaster is chosen", pg.js("[...document.querySelectorAll('#path-head input[type=checkbox]')].some(c=>c.parentElement.textContent.includes('Repeat') && c.disabled)"))
        pg.js("[...document.querySelectorAll('#path-head button')].find(b=>b.textContent==='Run disaster trace').click(); true")
        ok &= check("Run asks for confirmation (nothing started yet)",
                    pg.js("!!document.getElementById('dz-confirm-go') && !trace.job"))
        print("screenshot:", pg.shot("panel", ".dz-panel"))
        if run:
            pg.js("document.getElementById('dz-confirm-go').click(); true")
            for _ in range(60):                  # the POST rebuilds the inventory first
                if pg.js("!!(trace.job && trace.job.id)"):
                    break
                time.sleep(2)
            ok &= check("job started after confirm", pg.js("!!(trace.job && trace.job.id)"))
            banner_seen = False
            t0 = time.time()
            while time.time() - t0 < 1500:
                time.sleep(10)
                pg.js("pollDisaster(); true")
                banner_seen |= bool(pg.js("!document.getElementById('disaster-banner').hidden"))
                if not pg.js("!!trace.job"):
                    break
            ok &= check("banner shown during the test", banner_seen)
            for _ in range(30):                  # drawn as soon as the job ends
                if pg.js("!!document.querySelector('.dz-result')"):
                    break
                time.sleep(1)
            ok &= check("result has a disaster section", pg.js("!!document.querySelector('.dz-result')"),
                        pg.js("trace.error"))
            ok &= check("outage strip drawn", pg.js("document.querySelectorAll('.dz-strip i').length") > 0)
            ok &= check("restore line says restored", "Restored" in (pg.js("(document.querySelector('.dz-restore')||{}).textContent") or ""),
                        pg.js("(document.querySelector('.dz-restore')||{}).textContent"))
            print("screenshot:", pg.shot("result", ".dz-result"))
            print("screenshot:", pg.shot("full"))
    finally:
        chrome.terminate()
    print("UI E2E:", "ALL PASS" if ok else "FAILURES ABOVE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
