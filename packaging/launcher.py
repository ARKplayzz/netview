"""NetView.exe entry point - a double-click launcher for people who don't run Python.

What it does:
  1. First run (or `NetView.exe --setup`): asks for the CML server address,
     username and password, signs in to check them, lists the labs that account
     can see and lets you pick one. The answers are saved in
     %LOCALAPPDATA%\\NetView\\netview.env - on YOUR machine only; nothing is
     ever written back into the download or the repo.
  2. Starts NetView (the same Flask app as `py -3.11 netview/app.py`) and opens
     http://127.0.0.1:5057 in your browser.
  3. Keep the black window open while you use NetView; closing it stops NetView.

P45 resume (v1.2.0): the build carries the P45 lab's own sign-in (CML address,
user, lab, device and Windows console passwords - Andrew's call, 2026-10-01:
it is a local CML instance). On start NetView offers "P45 resume", which signs
straight into the P45 lab; it is the default and is picked automatically after
a few seconds. `NetView.exe --p45` skips the menu. Your own saved settings and
--setup still work as before.

The exe carries NetView, the lab helper scripts it imports and the lab's data
files, laid out exactly as in the project repo, and runs them from there.
"""
from __future__ import annotations

# --- Imported here so PyInstaller bundles them: NetView's own modules are shipped
# as plain source files, which the freezer does not analyse. ---
import argparse  # noqa: F401
import base64  # noqa: F401
import collections  # noqa: F401
import csv  # noqa: F401
import datetime  # noqa: F401
import getpass
import hashlib  # noqa: F401
import io  # noqa: F401
import ipaddress  # noqa: F401
import json
import os
import re  # noqa: F401
import runpy
import secrets  # noqa: F401
import socket
import ssl
import sys
import threading
import time
import typing  # noqa: F401
import urllib.error
import urllib.request
import uuid  # noqa: F401
import webbrowser
from pathlib import Path

import flask  # noqa: F401
import jinja2  # noqa: F401
import paramiko  # noqa: F401
import werkzeug  # noqa: F401
import yaml  # noqa: F401

PORT = 5057
APP_URL = f"http://127.0.0.1:{PORT}"
CONFIG_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "NetView"
CONFIG = CONFIG_DIR / "netview.env"
P45_WAIT_S = 5


def read_env(path: Path) -> dict[str, str]:
    cfg = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


def bundle_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))


def ask(prompt: str, default: str | None = None) -> str:
    shown = f"{prompt} [{default}]: " if default else f"{prompt}: "
    while True:
        v = input(shown).strip()
        if v or default:
            return v or default
        print("  (required)")


def cml_call(url: str, path: str, verify: bool, token: str | None = None, body=None):
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url + path, method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, context=ctx, timeout=20) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def setup() -> dict[str, str]:
    print("\n=== NetView first-time setup ===")
    print("You need the address of the CML server and a CML login.\n")
    while True:
        url = ask("CML server address (e.g. https://10.0.0.5)").rstrip("/")
        if not url.startswith("http"):
            url = "https://" + url
        user = ask("CML username", "admin")
        pw = getpass.getpass("CML password (typing is hidden): ")
        verify = ask("Check the server's certificate? Labs usually use a self-signed one (y/n)", "n")
        verify_ssl = verify.lower().startswith("y")
        try:
            token = cml_call(url, "/api/v0/authenticate", verify_ssl, body={"username": user, "password": pw})
            labs = cml_call(url, "/api/v0/labs?show_all=true", verify_ssl, token) or []
        except urllib.error.HTTPError as e:
            print(f"\n  Sign-in failed (HTTP {e.code}). Check the username and password.\n")
            continue
        except Exception as e:  # noqa: BLE001
            print(f"\n  Could not reach {url}: {e}\n  Check the address and that you're on the lab network.\n")
            continue
        if not labs:
            print("\n  Signed in, but this account can't see any labs.\n")
            continue
        print("\nLabs on this server:")
        titles = []
        for i, lab_id in enumerate(labs, 1):
            try:
                t = (cml_call(url, f"/api/v0/labs/{lab_id}", verify_ssl, token) or {}).get("lab_title", "")
            except Exception:  # noqa: BLE001
                t = ""
            titles.append(t)
            print(f"  {i}. {t or '(untitled)'}  [{lab_id}]")
        pick = ask("Number of the lab to use", "1")
        try:
            lab = labs[int(pick) - 1]
        except (ValueError, IndexError):
            print("  Not a number from the list - let's try again.\n")
            continue
        print("\nNetView logs in to the lab's routers and switches to run traces.")
        dev = getpass.getpass("Lab device password (ask the lab owner; typing is hidden): ")
        cfg = {"CML_URL": url, "CML_USERNAME": user, "CML_PASSWORD": pw,
               "CML_VERIFY_SSL": "true" if verify_ssl else "false", "CML_LAB_ID": lab,
               "P45_DEVICE_PASSWORD": dev}
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text("".join(f"{k}={v}\n" for k, v in cfg.items()), encoding="utf-8")
        print(f"\nSaved to {CONFIG}\n(Run 'NetView.exe --setup' any time to change it.)\n")
        return cfg


def load_config() -> dict[str, str] | None:
    if not CONFIG.exists():
        return None
    cfg = read_env(CONFIG)
    return cfg if cfg.get("CML_URL") else None


def load_p45() -> dict[str, str] | None:
    """The P45 lab preset build_exe.py bundles (absent in a build made with --no-p45)."""
    f = bundle_root() / "netview" / "p45-preset.env"
    if not f.exists():
        return None
    cfg = read_env(f)
    return cfg if cfg.get("CML_URL") else None


def p45_resume(cfg: dict[str, str]) -> dict[str, str]:
    """Sign in with the preset and say what state the lab is in. NetView runs
    either way - it reports an unreachable CML on the page - so this only informs."""
    url, verify = cfg["CML_URL"].rstrip("/"), cfg.get("CML_VERIFY_SSL", "false") == "true"
    print(f"\nP45 resume: signing in to {url} as {cfg.get('CML_USERNAME')} ...")
    try:
        token = cml_call(url, "/api/v0/authenticate", verify,
                         body={"username": cfg["CML_USERNAME"], "password": cfg["CML_PASSWORD"]})
        lab = cml_call(url, f"/api/v0/labs/{cfg['CML_LAB_ID']}", verify, token) or {}
        state = lab.get("state", "?")
        print(f"  signed in; lab '{lab.get('lab_title', cfg['CML_LAB_ID'])}' is {state}")
        if state != "STARTED":
            print("  The lab is not fully started - start it in CML for live traces.")
    except urllib.error.HTTPError as e:
        print(f"  CML refused the P45 sign-in (HTTP {e.code}) - run 'NetView.exe --setup' to enter it by hand.")
    except Exception as e:  # noqa: BLE001
        print(f"  Could not reach {url}: {e}\n  NetView will start anyway and show what it can.")
    return cfg


def timed_choice(prompt: str, default: str, wait_s: int) -> str:
    """One keypress, or `default` after wait_s seconds (Windows console);
    a plain input() elsewhere."""
    try:
        import msvcrt
    except ImportError:
        return (input(prompt).strip() or default)[:1].upper()
    print(prompt, end="", flush=True)
    end = time.time() + wait_s
    while time.time() < end:
        if msvcrt.kbhit():
            k = msvcrt.getwch()
            print(k if k.isprintable() else "")
            return default if k in ("\r", "\n") else k.upper()
        time.sleep(0.05)
    print(default)
    return default


def choose() -> dict[str, str]:
    saved, p45 = load_config(), load_p45()
    if "--setup" in sys.argv:
        return setup()
    if p45 and "--p45" in sys.argv:
        return p45_resume(p45)
    if not p45:
        return saved or setup()
    print("\n  P  P45 resume - straight into the P45 lab")
    # Only offer R when the saved settings point somewhere other than P45.
    same = lambda k: saved.get(k) == p45.get(k)  # noqa: E731
    if saved and not (same("CML_URL") and same("CML_LAB_ID")):
        print(f"  R  Resume my saved settings ({saved['CML_URL']})")
    else:
        saved = None
    print("  S  Set up a different CML server")
    while True:
        k = timed_choice(f"\nChoose [P] (P45 resume in {P45_WAIT_S} s): ", "P", P45_WAIT_S)
        if k == "P":
            return p45_resume(p45)
        if k == "R" and saved:
            return saved
        if k == "S":
            return setup()
        print("  P, R or S.")


def port_in_use() -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def main() -> int:
    print("NetView - P45 lab network viewer")
    cfg = choose()
    if port_in_use():
        print(f"NetView already seems to be running - opening {APP_URL}")
        webbrowser.open(APP_URL)
        time.sleep(3)
        return 0
    root = bundle_root()
    # Hand the settings to cml_api in memory (it reads CML_* / P45_* from the
    # environment). Never write them into the unpacked bundle: a hard stop
    # (Task Manager, a crash) leaves that temp folder behind.
    os.environ.update(cfg)
    for p in (root / ".claude" / "skills" / "cml" / "scripts", root / "netview"):
        sys.path.insert(0, str(p))
    os.chdir(root / "netview")
    print(f"\nStarting NetView on {APP_URL} ...")
    print("Keep this window open while you use NetView. Close it to stop NetView.\n")
    threading.Timer(2.5, lambda: webbrowser.open(APP_URL)).start()
    runpy.run_path(str(root / "netview" / "app.py"), run_name="__main__")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except SystemExit:
        raise
    except BaseException as e:  # keep the window open so a non-technical user can read it
        import traceback
        traceback.print_exc()
        input(f"\nNetView stopped with an error: {e}\nPress Enter to close.")
        sys.exit(1)
