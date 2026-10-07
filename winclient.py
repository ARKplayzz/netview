"""Windows desktops as NetView test sources (2026-10-06).

Windows puts nothing on its serial console, so NetView installs a small bridge
(winbridge/nvbridge.ps1) that answers a FIXED set of checks on COM1: ping, tcp,
dns, dhcp, ip, time. Nothing else can be run through it.

Install (once per PC, `install()`), driven through CML's VNC keyboard:
  1. log in if the PC is at its login screen (.env P45_WIN10_PASSWORD[_ALT]);
  2. open an elevated PowerShell (Win+R, powershell, Ctrl+Shift+Enter, UAC Yes);
  3. type a one-line loader that carries the SHA-256 of the setup script; it
     reads the script from COM1 and runs it ONLY if the hash matches;
  4. send the script over the serial console; it installs the bridge as a
     startup task (SYSTEM) and starts it.

After that the PC answers on its console with the prompt "NV> ".
"""
from __future__ import annotations

import base64
import hashlib
import re
import socket
import threading
import time
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent / "winbridge" / "nvbridge.ps1"
PROMPT = "NV> "
_PING = re.compile(r"nvping seq=(\d+) sent=([\d.]+) (ok rtt=(\d+)|lost)")


# ------------------------------------------------------------------ install
def setup_script() -> bytes:
    b64 = base64.b64encode(BRIDGE.read_bytes()).decode()
    ps = f"""$dir = 'C:\\ProgramData\\NetView'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
[IO.File]::WriteAllBytes("$dir\\nvbridge.ps1", [Convert]::FromBase64String('{b64}'))
$act = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$dir\\nvbridge.ps1`""
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$pri = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName 'NetViewBridge' -Action $act -Trigger (New-ScheduledTaskTrigger -AtStartup) -Settings $set -Principal $pri -Force | Out-Null
Start-ScheduledTask -TaskName 'NetViewBridge'
'NetView: bridge installed'
"""
    return ps.encode("utf-8")


def loader(sha256: str) -> str:
    """The one line typed into the elevated PowerShell. It runs the script it is
    sent over COM1 only if its SHA-256 matches the hash typed with it."""
    return ("Stop-ScheduledTask NetViewBridge -EA 0;"                          # an older bridge holds COM1
            "Get-CimInstance Win32_Process|?{$_.CommandLine -like '*nvbridge.ps1*'}|%{Stop-Process $_.ProcessId -Force -EA 0};"
            "$p=New-Object IO.Ports.SerialPort COM1,115200;$p.NewLine=\"`r\";$p.Open();$p.WriteLine('NVREADY');"
            "$b='';while(($l=$p.ReadLine().Trim()) -ne 'NVEND'){$b+=$l};$p.Close();"
            "$d=[Convert]::FromBase64String($b);"
            "$h=-join([Security.Cryptography.SHA256]::Create().ComputeHash($d)|%{$_.ToString('x2')});"
            f"if($h -eq '{sha256}'){{md -f C:\\ProgramData\\NetView|Out-Null;"
            "[IO.File]::WriteAllBytes('C:\\ProgramData\\NetView\\setup.ps1',$d);"
            "powershell -NoProfile -ExecutionPolicy Bypass -File C:\\ProgramData\\NetView\\setup.ps1}"
            "else{'NetView: setup rejected (hash mismatch)'}")


def install(cml, label: str, passwords: list[str], log=print, shots: Path | None = None) -> bool:
    """Install the bridge on one Windows PC through its VNC keyboard + serial console."""
    from console import Console, _drain                 # noqa: PLC0415
    from vnc import K_CTRL, K_RETURN, K_SHIFT, K_SUPER, VNC  # noqa: PLC0415

    script = setup_script()
    sha = hashlib.sha256(script).hexdigest()

    def shot(v, name):
        if shots:
            v.save(shots / f"winsetup_{re.sub(r'[^A-Za-z0-9]+', '_', label)}_{name}.png")

    with VNC(cml, label, write=True) as v:
        v.tap(K_CTRL)                                    # wake a blanked display
        time.sleep(3)
        shot(v, "0_start")
        if v.lit() < 5000:
            raise RuntimeError("the screen is still dark - is the PC running?")
        # At a login screen? (No taskbar yet: try each password; stop when the desktop appears.)
        for pw in passwords:
            if _desktop(v):
                break
            v.click(v.w // 2, int(v.h * 0.64))
            time.sleep(0.5)
            v.type(pw + "\n")
            time.sleep(15)
        shot(v, "1_desktop")
        if not _desktop(v):
            raise RuntimeError("could not reach the desktop (login failed?)")
        v.combo(K_SUPER, ord("r"))
        time.sleep(2)
        v.type("powershell")
        v.combo(K_CTRL, K_SHIFT, K_RETURN)               # run as administrator
        time.sleep(5)
        shot(v, "2_uac")
        v.combo(0xffe9, ord("y"))                        # Alt+Y on the UAC prompt
        time.sleep(6)
        shot(v, "3_powershell")
        con = Console(cml)
        con.open()
        try:
            key = con._key_for(label, 0)
            con._ch.send(f"connect {key}\n")
            time.sleep(2)
            _drain(con._ch, 400, 2000)
            con._ch.settimeout(0.5)
            log(f"{label}: typing the loader")
            v.type(loader(sha) + "\n", gap=0.015)
            buf = _read_until(con._ch, "NVREADY", 30)
            if "NVREADY" not in buf:
                shot(v, "4_no_loader")
                raise RuntimeError("the loader did not answer on COM1")
            b64 = base64.b64encode(script).decode()
            for i in range(0, len(b64), 512):
                con._ch.send(b64[i:i + 512] + "\r")
                time.sleep(0.05)
            con._ch.send("NVEND\r")
            log(f"{label}: setup sent ({len(script)} bytes, sha256 {sha[:12]})")
            buf = _read_until(con._ch, "bridge ready", 60)
            shot(v, "4_installed")
            ok = "bridge ready" in buf
        finally:
            try:
                con._ch.send("\x1d")                     # Ctrl-] back to the console server
            except Exception:                            # noqa: BLE001
                pass
            con.close()
        v.type("exit\n")                                 # close the admin PowerShell
    return ok


def _desktop(v) -> bool:
    """The Windows taskbar: a dark strip along the bottom of the screen."""
    v.refresh()
    w, h, fb = v.w, v.h, v.fb
    y = h - 12
    dark = sum(1 for x in range(0, w, 8) if max(fb[(y * w + x) * 3:(y * w + x) * 3 + 3]) < 60)
    return dark > (w // 8) * 0.6


def _read_until(ch, marker: str, timeout: float) -> str:
    buf, t0 = "", time.time()
    while time.time() - t0 < timeout and marker not in buf:
        try:
            buf += ch.recv(65536).decode("utf-8", errors="replace")
        except socket.timeout:
            pass
    return buf


# ------------------------------------------------------------------- client
class WinBridge:
    """One console session to a Windows PC's bridge.

        with WinBridge(cml, "WFH user (win10)") as b:
            print(b.run("dns fileserver.p45.nz"))
    """

    def __init__(self, cml, label: str):
        self.cml, self.label = cml, label
        self.con = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *a):
        self.close()

    def open(self, timeout: float = 20) -> None:
        from console import Console, _drain              # noqa: PLC0415
        self.con = Console(self.cml)
        self.con.open()
        key = self.con._key_for(self.label, 0)
        self.con._ch.send(f"connect {key}\n")
        time.sleep(1.5)
        _drain(self.con._ch, 300, 1500)
        self.con._ch.settimeout(0.5)
        t0 = time.time()
        while time.time() - t0 < timeout:
            self.con._ch.send("\r")
            if PROMPT in _read_until(self.con._ch, PROMPT, 3):
                return
        raise RuntimeError(f"{self.label}: no NetView bridge on the console (not installed?)")

    def run(self, command: str, timeout: float = 30) -> str:
        self.con._ch.send(command + "\r")
        out = _read_until(self.con._ch, PROMPT, timeout)
        return out.replace(PROMPT, "").strip()

    def close(self) -> None:
        if self.con:
            try:
                self.con._ch.send("\x1d")
            except Exception:                            # noqa: BLE001
                pass
            self.con.close()
            self.con = None


def has_bridge(cml, label: str) -> bool:
    try:
        with WinBridge(cml, label) as b:
            return "nv hello" in b.run("hello", 10)
    except Exception:                                    # noqa: BLE001
        return False


class WinContinuousPing:
    """Same contract as console.ContinuousPing, from a Windows PC's bridge:
    `replies` {seq: {"t": local arrival time, "rtt_ms": x}}, `on_reply(seq, t)`."""

    def __init__(self, cml, node: str, dest: str, interval_s: float = 0.2, line: int = 0, on_reply=None,
                 checks: list[str] | None = None, on_line=None):
        """`checks`: bridge watch tokens ("dns=name@server", "tcp=host:port") run every
        second alongside the pings; their result lines go to on_line(line, local_time)."""
        self.cml, self.node, self.dest, self.interval_s, self.on_reply = cml, node, dest, interval_s, on_reply
        self.checks, self.on_line = list(checks or []), on_line
        self.replies: dict[int, dict] = {}
        self.started_at: float | None = None
        self.error: str | None = None
        self.raw_tail = ""
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, wait_s: float = 60) -> bool:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(wait_s)
        return self.started_at is not None and self.error is None

    def _run(self) -> None:
        b = WinBridge(self.cml, self.node)
        try:
            b.open()
            ch = b.con._ch
            ch.settimeout(0.2)
            iv = int(self.interval_s * 1000)
            ch.send(((f"watch {self.dest} {iv} 1000 " + " ".join(self.checks)) if self.checks
                     else f"ping {self.dest} {iv}") + "\r")
            self.started_at = time.time()
            self._ready.set()
            buf = ""
            while not self._stop.is_set():
                try:
                    chunk = ch.recv(65536).decode("utf-8", errors="replace")
                except socket.timeout:
                    continue
                now = time.time()
                buf += chunk
                *lines, buf = buf.split("\n")
                for ln in lines:
                    if self.on_line and ln.startswith(("nvdns", "nvtcp")):
                        try:
                            self.on_line(ln, now)
                        except Exception:                # noqa: BLE001
                            pass
                        continue
                    m = _PING.search(ln)
                    if m and m.group(4) is not None:
                        seq = int(m.group(1))
                        self.replies[seq] = {"t": now, "rtt_ms": float(m.group(4))}
                        if self.on_reply:
                            try:
                                self.on_reply(seq, now)
                            except Exception:            # noqa: BLE001
                                pass
            ch.send("\r")                                # any input stops the bridge's ping
            self.raw_tail = _read_until(ch, PROMPT, 5)[-600:]
        except Exception as e:                           # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self._ready.set()
            b.close()

    def stop(self, timeout: float = 15) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
