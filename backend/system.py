"""Everything the checks need from the machine, behind one object.

The checks in checks.py never call subprocess, httpx, ssl or ctypes
themselves; they ask a System. The tests hand them a fake one, which is how
the whole of the farm PC - Tailscale, scheduled tasks, a dead Boord - can be
played out on a Linux laptop with no network.
"""
import ctypes
import datetime as dt
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import time
from typing import List, Optional

import httpx

IS_WINDOWS = sys.platform == "win32"

# No console window flashes up on the farm PC's screen each minute when the
# service (running as SYSTEM, but on a PC someone may be sitting at) shells
# out to PowerShell or tailscale.exe.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Probe:
    """One HTTP request's outcome.

    `answered` follows watchdog.ps1's rule: ANY HTTP status - 200, 404, 500 -
    means a server is listening and serving. Only a refused connection, a
    timeout or a TLS failure counts as not answering. The dashboard shows the
    code too, so a 500 is visible without being called an outage.
    """

    def __init__(self, answered: bool, status: Optional[int] = None,
                 ms: Optional[int] = None, error: str = "", body: object = None) -> None:
        self.answered = answered
        self.status = status
        self.ms = ms
        self.error = error
        self.body = body


class System:
    def now(self) -> dt.datetime:
        """Naive UTC, like every timestamp in the sibling apps."""
        return dt.datetime.utcnow()

    def run(self, args: List[str], timeout: float = 20) -> tuple:
        """(returncode, stdout, stderr). A missing program is returncode 127
        rather than an exception, so a farm without Tailscale gets a red
        Tailscale card and not a dead monitor."""
        try:
            p = subprocess.run(args, capture_output=True, timeout=timeout,
                               creationflags=_NO_WINDOW)
        except FileNotFoundError:
            return 127, "", f"{args[0]} not found"
        except subprocess.TimeoutExpired:
            return 124, "", f"{os.path.basename(args[0])} did not answer within {int(timeout)} s"
        except OSError as e:
            return 126, "", str(e)
        # tailscale.exe and PowerShell write UTF-8 when asked for JSON;
        # anything else is decoded leniently because a stray byte in a
        # Windows error message must not take the check down.
        return (p.returncode, p.stdout.decode("utf-8", "replace"),
                p.stderr.decode("utf-8", "replace"))

    def powershell(self, script: str, timeout: float = 30) -> tuple:
        return self.run(["powershell", "-NoProfile", "-NonInteractive",
                         "-ExecutionPolicy", "Bypass", "-Command", script], timeout)

    def http(self, url: str, timeout: float = 10, want_json: bool = False,
             headers: Optional[dict] = None) -> Probe:
        start = time.monotonic()
        try:
            # Redirects are not followed: a redirect IS an answer (Notes' "/"
            # sends you to /app/), and following it would only measure the
            # second request.
            with httpx.Client(timeout=timeout, follow_redirects=False) as c:
                r = c.get(url, headers=headers or {})
        except httpx.TimeoutException:
            return Probe(False, error=f"no answer within {int(timeout)} s")
        except httpx.ConnectError as e:
            msg = str(e) or "connection refused"
            if "CERTIFICATE" in msg.upper() or "SSL" in msg.upper():
                return Probe(False, error="TLS certificate problem: " + msg[:160])
            return Probe(False, error="connection refused" if "refused" in msg.lower() else msg[:160])
        except httpx.HTTPError as e:
            return Probe(False, error=str(e)[:160] or e.__class__.__name__)
        ms = int((time.monotonic() - start) * 1000)
        body = None
        if want_json and r.status_code == 200:
            try:
                body = r.json()
            except ValueError:
                body = None
        return Probe(True, r.status_code, ms, body=body)

    def tls_expiry(self, host: str, port: int, timeout: float = 10) -> tuple:
        """(notAfter as naive UTC, "") or (None, why). Verified against the
        system's trust store, so a certificate the phones would refuse is
        reported as a problem rather than read out regardless."""
        ctx = ssl.create_default_context()
        try:
            with socket.create_connection((host, port), timeout=timeout) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as tls:
                    cert = tls.getpeercert()
        except ssl.SSLCertVerificationError as e:
            return None, f"certificate not trusted: {e.verify_message}"
        except (OSError, ssl.SSLError) as e:
            return None, str(e)[:160] or e.__class__.__name__
        not_after = cert.get("notAfter") if cert else None
        if not not_after:
            return None, "no certificate presented"
        stamp = ssl.cert_time_to_seconds(not_after)
        return dt.datetime.utcfromtimestamp(stamp), ""

    def disk(self, path: str) -> tuple:
        """(total bytes, free bytes) of the drive holding `path`."""
        usage = shutil.disk_usage(path)
        return usage.total, usage.free

    def memory(self) -> Optional[tuple]:
        """(total bytes, available bytes), or None where it can't be read."""
        if IS_WINDOWS:
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return stat.ullTotalPhys, stat.ullAvailPhys
            return None
        try:
            info = {}
            with open("/proc/meminfo", encoding="ascii") as fh:
                for line in fh:
                    k, v = line.split(":", 1)
                    info[k] = int(v.split()[0]) * 1024
            return info["MemTotal"], info.get("MemAvailable", info.get("MemFree", 0))
        except (OSError, KeyError, ValueError):
            return None

    def uptime_s(self) -> Optional[float]:
        if IS_WINDOWS:
            return ctypes.windll.kernel32.GetTickCount64() / 1000.0
        try:
            with open("/proc/uptime", encoding="ascii") as fh:
                return float(fh.read().split()[0])
        except (OSError, ValueError):
            return None

    def read_tail(self, path: str, max_bytes: int = 64 * 1024) -> Optional[str]:
        """The end of a text file, or None if it isn't there. Only the tail:
        the watchdog log is capped at 1 MB, and reading all of it every
        minute to look at the last day would be waste."""
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - max_bytes))
                return fh.read().decode("utf-8", "replace")
        except OSError:
            return None

    def newest_file(self, folder: str, max_files: int = 5000) -> Optional[tuple]:
        """(path, modified time as naive UTC) of the newest file anywhere
        under `folder`, or None if it holds no files. Raises OSError if the
        folder itself can't be read. Capped, because a misconfigured line
        pointing at the root of a drive must not make the check walk the
        whole disk every minute."""
        if not os.path.isdir(folder):
            raise FileNotFoundError(folder)
        best = None
        seen = 0
        for root, _dirs, files in os.walk(folder):
            for name in files:
                seen += 1
                if seen > max_files:
                    break
                path = os.path.join(root, name)
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                if best is None or mtime > best[1]:
                    best = (path, mtime)
            if seen > max_files:
                break
        if best is None:
            return None
        return best[0], dt.datetime.utcfromtimestamp(best[1])

    def process_running(self, image: str) -> Optional[bool]:
        """Whether a process with this .exe name is running in any session,
        or None where that can't be asked (not Windows). tasklist rather
        than PowerShell: it starts in a fraction of the time, and only the
        image name is looked for in its output, so the PC's display
        language does not matter."""
        if not IS_WINDOWS:
            return None
        rc, out, _err = self.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH", "/FO", "CSV"], 15)
        if rc != 0:
            return None
        return image.lower() in out.lower()

    def is_windows(self) -> bool:
        return IS_WINDOWS


def parse_json(text: str) -> object:
    try:
        return json.loads(text)
    except ValueError:
        return None
