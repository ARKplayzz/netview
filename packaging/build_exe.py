"""Build dist/NetView.exe - the one-file download for people who don't run Python.

    py -3.11 netview/packaging/build_exe.py        (from the CML project root)

Needs the full CML project checkout (NetView imports the lab helper scripts and
reads data/ at runtime) and a Python with: pyinstaller flask pyyaml jinja2
paramiko. It stages exactly what NetView uses, in the project's own layout, and
REFUSES to build if any value from the project's .env (CML password, VPN key...)
appears in a staged file - the exe must never carry anyone's credentials;
each user enters their own on first run (see launcher.py).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

NETVIEW = Path(__file__).resolve().parents[1]
REPO = NETVIEW.parent
STAGE = NETVIEW / "packaging" / "build" / "stage"
DIST = NETVIEW / "packaging" / "dist"

INCLUDE = [
    # (source relative to the project root, glob) - exactly what NetView imports.
    # Deliberately NOT: data/topology.json and templates/ (both hold the lab's
    # device password inside node configs; topology.json is only NetView's
    # offline fallback, which reports itself missing cleanly), or any other
    # skill script.
    ("netview", "*.py"), ("netview/static", "*"), ("netview/templates", "*"),
    (".claude/skills/cml/scripts", "cml_api.py"), (".claude/skills/cml/scripts", "console.py"),
    (".claude/skills/cml/scripts", "render_configs.py"), (".claude/skills/cml/scripts", "probe_matrix.py"),
    (".claude/skills/cml/scripts", "dhcp_check.py"),
    (".claude/skills/winserver/scripts", "win_console.py"),
    ("data", "manifest.yml"), ("data", "address-lock.json"),
]


def secrets_from_env() -> list[str]:
    env = REPO / ".env"
    vals = []
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                v = line.split("=", 1)[1].strip()
                if len(v) >= 6 and v.lower() not in ("true", "false"):
                    vals.append(v)
    return vals


def main() -> int:
    if STAGE.exists():
        shutil.rmtree(STAGE)
    n = 0
    for rel, pattern in INCLUDE:
        for f in (REPO / rel).glob(pattern):
            if f.is_file() and "__pycache__" not in f.parts and f.name != "trace_suite.py":
                dst = STAGE / rel / f.name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst)
                n += 1
    leaks = []
    for s in secrets_from_env():
        for f in STAGE.rglob("*"):
            if f.is_file() and s in f.read_text(encoding="utf-8", errors="ignore"):
                leaks.append(f"{f.relative_to(STAGE)} contains a value from .env")
    if leaks:
        print("REFUSING TO BUILD - credentials would ship in the exe:\n  " + "\n  ".join(leaks))
        return 1
    print(f"staged {n} files; no .env values found in any of them")

    adds = []
    for top in ("netview", ".claude", "data"):
        adds += ["--add-data", f"{STAGE / top}{';' if sys.platform == 'win32' else ':'}{top}"]
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
           "--name", "NetView", "--console",
           "--distpath", str(DIST), "--workpath", str(NETVIEW / "packaging" / "build" / "work"),
           "--specpath", str(NETVIEW / "packaging" / "build"),
           "--collect-submodules", "flask", "--collect-submodules", "werkzeug",
           "--collect-submodules", "jinja2", "--collect-submodules", "paramiko",
           "--collect-data", "flask",
           *adds, str(NETVIEW / "packaging" / "launcher.py")]
    r = subprocess.run(cmd)
    if r.returncode == 0:
        exe = DIST / "NetView.exe"
        print(f"\nbuilt {exe} ({exe.stat().st_size / 1e6:.1f} MB)")
    return r.returncode


if __name__ == "__main__":
    raise SystemExit(main())
