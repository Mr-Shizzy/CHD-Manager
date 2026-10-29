"""
CHD Manager - convert game disc images between formats (CHD, PBP, CSO/ZSO, ECM, RVZ, WBFS and more)

Put this file next to chdman.exe (or point it at chdman.exe in Settings, Output page).
Needs only Python 3 (tkinter is included with it). Double-click to run.

What it does
  * Queue of disc images (.cue .gdi .iso .toc, lone .bin) -> .chd, and .chd -> extract / verify
  * Detects CD vs DVD automatically (and the platform: PS1, PS2, PSP, Dreamcast, Saturn, Sega CD)
  * Compression presets, threads, hunk size, overwrite / skip / rename, output folder
  * Verify after create; optional "send sources to Recycle Bin" ONLY after a verified CHD
  * Live progress, log, summary of space saved
"""
import bisect
import bz2
import ctypes
import hashlib
import json
import lzma
import os
import platform
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import zlib
import webbrowser
import tkinter as tk
from ctypes import wintypes
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_NAME = "CHD Manager"
APP_VER = "2.0.1"
FROZEN = getattr(sys, "frozen", False)          # True when running as the packaged .exe
HERE = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
SETTINGS_FILE = HERE / "chd_manager_settings.json"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

CD_MAX_BYTES = 737_280_000          # 80-minute CD: 360,000 sectors * 2048 bytes
SRC_EXTS = {".cue", ".gdi", ".toc", ".iso", ".bin", ".pbp", ".cdi", ".mds", ".mdf", ".nrg", ".ccd", ".img", ".sub", ".m3u", ".cso", ".zso", ".ecm", ".gcm", ".gcz", ".rvz", ".wia", ".wbfs", ".ciso", ".wux", ".wud"}
ARCHIVE_EXTS = {".zip", ".7z", ".rar", ".tar", ".gz", ".tgz"}
CHDMAN = None                        # path to chdman.exe, set at startup

THEMES = {
    "dark": dict(bg="#12141a", panel="#1a1d26", panel2="#1f2330", field="#242a3b", btn="#2a3042", btn_hover="#37405c",
                 border="#2d3348", head="#222737", fg="#e8eaf2", muted="#8d93a8", sel="#2f3f78", trough="#242a3b",
                 accent="#7c5cff", accent2="#9b82ff", g1="#6d4dff", g2="#1fc8e8",
                 info="#e8eaf2", cmd="#6ea8ff", ok="#3ddc97", err="#ff6b6b", warn="#ffb454", work="#6ea8ff", skip="#8d93a8"),
    "light": dict(bg="#f3f4f8", panel="#ffffff", panel2="#f5f6fb", field="#ffffff", btn="#e3e6ef", btn_hover="#d3d9ea",
                  border="#cfd4e3", head="#eceff7", fg="#1b1e2b", muted="#6b7185", sel="#cfd9ff", trough="#dfe3ef",
                  accent="#6a4cf0", accent2="#8068ff", g1="#6a4cf0", g2="#0fb5d6",
                  info="#1b1e2b", cmd="#0b57d0", ok="#1a7f37", err="#c62828", warn="#b26a00", work="#0b57d0", skip="#777777"),
}


class _Tip:
    """A small hover hint for a widget; text_fn() is called each time so it can reflect the current state."""

    def __init__(self, widget, text_fn):
        self.w, self.fn, self.tw, self.job = widget, text_fn, None, None
        widget.bind("<Enter>", self._enter, add="+")
        widget.bind("<Leave>", self._leave, add="+")
        widget.bind("<ButtonPress>", self._leave, add="+")

    def _enter(self, e=None):
        self._leave()
        self.job = self.w.after(500, self._show)

    def _leave(self, e=None):
        if self.job:
            self.w.after_cancel(self.job)
            self.job = None
        if self.tw:
            self.tw.destroy()
            self.tw = None

    def _show(self):
        txt = self.fn()
        if not txt:
            return
        tw = tk.Toplevel(self.w)
        tw.wm_overrideredirect(True)
        tw.geometry(f"+{self.w.winfo_rootx()}+{self.w.winfo_rooty() - 32}")
        tk.Label(tw, text=txt, bg="#2a3042", fg="#ffffff", padx=9, pady=4, font=("Segoe UI", 9)).pack()
        self.tw = tw


def lerp_hex(c1, c2, t):
    t = max(0.0, min(1.0, t))
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


# --------------------------------------------------------------------------- helpers
def fmt_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def fmt_time(sec):
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60:02d}s"
    return f"{sec // 3600}h {(sec % 3600) // 60:02d}m"


def find_chdman(saved=""):
    for cand in (saved, str(HERE / "chdman.exe")):
        if cand and Path(cand).is_file():
            return str(Path(cand))
    from shutil import which
    return which("chdman")


def restore_bundled_chdman():
    """Packaged exe only: if chdman.exe is missing, copy the spare one baked into the exe next to it."""
    src = Path(getattr(sys, "_MEIPASS", "")) / "chdman.exe"
    dst = HERE / "chdman.exe"
    if FROZEN and src.is_file() and not dst.exists():
        try:
            shutil.copy2(src, dst)
            return str(dst)
        except OSError:
            return None
    return None


def run_capture(cmd, timeout=120):
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW,
                           stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return -1, str(e)


def chdman_info():
    """-> (version string, supports createdvd)"""
    if not CHDMAN:
        return "", False
    _, text = run_capture([CHDMAN])
    m = re.search(r"manager ([\d.]+) \((.*?)\)", text)
    return (m.group(1) if m else "?"), ("createdvd" in text)


# ----------------------------------------------------------- chdman updates (GitHub)
RELEASE_API = "https://api.github.com/repos/mamedev/mame/releases/latest"


def ver_to_int(s):
    """'0.289' or 'mame0289' -> 289"""
    digits = re.sub(r"\D", "", s or "")
    return int(digits) if digits else 0


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VER}"})
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_latest():
    """Latest official MAME release -> dict(tag, num, exe=(name,url,size), sums_url)."""
    with http_get(RELEASE_API) as r:
        data = json.loads(r.read().decode("utf-8"))
    arch = "arm64" if "ARM" in platform.machine().upper() else "(?:x64|64bit)"   # MAME renamed 64bit -> x64 in 0.281
    exe, sums = None, None
    for a in data.get("assets", []):
        if re.fullmatch(rf"mame\d+b_{arch}\.exe", a["name"]):
            exe = (a["name"], a["browser_download_url"], a["size"])
        elif a["name"] == "SHA256SUMS":
            sums = a["browser_download_url"]
    if not exe or not sums:
        raise RuntimeError("Could not find the Windows download or checksum file in the latest release "
                           "(MAME may have renamed it). Get it by hand from https://github.com/mamedev/mame/releases/latest "
                           "and use the Browse button in Settings, Output page.")
    for url in (exe[1], sums):
        if not url.startswith("https://github.com/mamedev/mame/"):
            raise RuntimeError("Unexpected download address: " + url)
    return {"tag": data["tag_name"], "num": ver_to_int(data["tag_name"]), "exe": exe, "sums": sums}


def find_7z():
    for c in (r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe"):
        if Path(c).is_file():
            return c
    return shutil.which("7z")


def install_7zip():
    """Install 7-Zip from the official winget source. Windows shows its own permission (UAC) prompt.
    -> (ok, message)"""
    if not shutil.which("winget"):
        return False, "no-winget"
    rc, text = run_capture(["winget", "install", "-e", "--id", "7zip.7zip", "--silent",
                            "--accept-package-agreements", "--accept-source-agreements"], timeout=900)
    if find_7z():
        return True, ""
    return False, text.strip()[-300:] or f"winget exited with code {rc}"


def chd_kind(path):
    return chd_info(path)[0]


def chd_info(path):
    """(disc kind, track count) read from 'chdman info -v'."""
    rc, text = run_capture([CHDMAN, "info", "-v", "-i", str(path)])
    if rc != 0:
        return "?", 0
    tracks = len(re.findall(r"^\s*TRACK:", text, re.M))
    return _kind_from_tags(text), tracks


def chd_probe(path):
    """(disc kind, [track type per track, e.g. 'MODE2_RAW' or 'AUDIO']) read from 'chdman info -v'."""
    rc, text = run_capture([CHDMAN, "info", "-v", "-i", str(path)])
    if rc != 0:
        return "?", []
    return _kind_from_tags(text), re.findall(r"^\s*TRACK:\s*\d+\s+TYPE:(\S+)", text, re.M)


def _kind_from_tags(text):
    tags = set(re.findall(r"Tag='(.{4})'", text))
    if tags & {"CHGT", "CHGD"}:
        return "GD-ROM"
    if tags & {"CHT2", "CHTR", "CHCD"}:
        return "CD"
    if "DVD " in tags:
        return "DVD"
    if "GDDD" in tags:
        return "HD"
    if tags & {"AVAU", "AVLD"}:
        return "LD"
    return "Raw"


def parse_cue(p):
    refs = []
    try:
        for line in p.read_text("utf-8", "replace").splitlines():
            m = re.match(r'\s*FILE\s+(?:"([^"]+)"|(\S+))', line, re.I)
            if m:
                refs.append(p.parent / (m.group(1) or m.group(2)))
    except OSError:
        pass
    return refs


def parse_gdi(p):
    refs = []
    try:
        for line in p.read_text("utf-8", "replace").splitlines()[1:]:
            m = re.match(r'\s*\d+\s+\d+\s+\d+\s+\d+\s+(?:"([^"]+)"|(\S+))', line)
            if m:
                refs.append(p.parent / (m.group(1) or m.group(2)))
    except OSError:
        pass
    return refs


def parse_toc(p):
    refs = []
    try:
        for line in p.read_text("utf-8", "replace").splitlines():
            m = re.search(r'(?:DATA)?FILE\s+"([^"]+)"', line, re.I)
            if m:
                refs.append(p.parent / m.group(1))
    except OSError:
        pass
    return refs


# ----------------------------------------------------------- disc sniffing (platform)
def cue_data_start(p):
    """(file, byte offset) of the first non-audio track in a .cue, so discs that begin with audio tracks
    (common for Dreamcast self-boot discs) can still be recognized."""
    sizes = {"MODE1/2352": 2352, "MODE2/2352": 2352, "MODE1/2048": 2048, "MODE2/2336": 2336}
    cur_file, cur_type = None, None
    try:
        for line in p.read_text("utf-8", "replace").splitlines():
            t = line.strip()
            m = re.match(r'FILE\s+(?:"([^"]+)"|(\S+))', t, re.I)
            if m:
                cur_file = p.parent / (m.group(1) or m.group(2))
                continue
            m = re.match(r"TRACK\s+\d+\s+(\S+)", t, re.I)
            if m:
                cur_type = m.group(1).upper()
                continue
            m = re.match(r"INDEX\s+01\s+(\d+):(\d+):(\d+)", t, re.I)
            if m and cur_file and cur_type and cur_type != "AUDIO":
                frames = (int(m.group(1)) * 60 + int(m.group(2))) * 75 + int(m.group(3))
                return cur_file, frames * sizes.get(cur_type, 2352)
    except OSError:
        pass
    return None


class DiscReader:
    def __init__(self, path, sector_size, data_offset, base=0):
        self.path, self.ss, self.off, self.base = path, sector_size, data_offset, base

    def sector(self, lba):
        with open(self.path, "rb") as f:
            f.seek(self.base + lba * self.ss + self.off)
            return f.read(2048)


def _iso_platform(r, pvd):
    rec = pvd[156:190]
    extent = struct.unpack("<I", rec[2:6])[0]
    size = struct.unpack("<I", rec[10:14])[0]
    data = b"".join(r.sector(extent + i) for i in range(min(8, (size + 2047) // 2048)))
    pos, names = 0, []
    while pos < len(data):
        ln = data[pos]
        if ln == 0:
            pos = (pos // 2048 + 1) * 2048
            continue
        nl = data[pos + 32]
        name = data[pos + 33:pos + 33 + nl].decode("latin1").upper()
        names.append((name, struct.unpack("<I", data[pos + 2:pos + 6])[0],
                      struct.unpack("<I", data[pos + 10:pos + 14])[0]))
        pos += ln
    for name, ext, sz in names:
        if name.startswith("PSP_GAME") or name.startswith("UMD_DATA"):
            return "PSP"
    for name, ext, sz in names:
        if name.startswith("SYSTEM.CNF"):
            txt = r.sector(ext)[:max(1, min(sz, 2048))].decode("latin1", "replace").upper()
            if "BOOT2" in txt:
                return "PS2"
            if "BOOT" in txt:
                return "PS1"
    return None


def sniff_platform(path, base=0):
    try:
        for ss, off in ((2048, 0), (2352, 16), (2352, 24)):
            r = DiscReader(path, ss, off, base)
            s0, s16 = r.sector(0), r.sector(16)
            if s0.startswith(b"SEGA SEGAKATANA"):
                return "Dreamcast"
            if len(s16) >= 6 and s16[1:6] == b"CD001":
                plat = _iso_platform(r, s16)
                if plat:
                    return plat
            if s0.startswith(b"SEGA SEGASATURN"):
                return "Saturn"
            if s0.startswith(b"SEGADISCSYSTEM") or s0.startswith(b"SEGA MEGA DRIVE"):
                return "Sega CD"
    except Exception:  # noqa: BLE001
        pass
    return ""


def decide_type(ext, size, platform):
    """-> (CD|DVD, reason)"""
    if ext in (".cue", ".gdi", ".toc", ".bin"):
        return "CD", "CUE/GDI/TOC images are CD-based"
    if ext in (".pbp", ".cdi"):
        return "CD", "PBP and CDI images are CD-based"
    if platform == "PSP":
        return "DVD", "PSP UMD image"
    if size <= CD_MAX_BYTES:
        return "CD", f"{fmt_size(size)} fits on a CD (limit {fmt_size(CD_MAX_BYTES)})"
    return "DVD", f"{fmt_size(size)} is larger than a CD ({fmt_size(CD_MAX_BYTES)})"


# ------------------------------------------------------------------ recycle bin
class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", ctypes.c_void_p),
                ("pTo", ctypes.c_void_p), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]


def recycle(paths):
    """Send files to the Recycle Bin (recoverable). Returns True on success."""
    paths = [str(p) for p in paths if Path(p).exists()]
    if not paths:
        return True
    joined = "\0".join(paths) + "\0\0"
    buf = ctypes.create_unicode_buffer(joined, len(joined) + 1)
    op = _SHFILEOPSTRUCTW()
    op.wFunc = 3                       # FO_DELETE
    op.pFrom = ctypes.cast(buf, ctypes.c_void_p).value
    op.fFlags = 0x40 | 0x10 | 0x4 | 0x400   # ALLOWUNDO | NOCONFIRMATION | SILENT | NOERRORUI
    res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return res == 0 and not op.fAnyOperationsAborted


def delete_forever(paths):
    """Delete files for good (not recoverable; frees the space at once). Returns True when every one is gone."""
    ok = True
    for p in paths:
        try:
            if Path(p).exists():
                Path(p).unlink()
        except OSError:
            ok = False
    return ok


# ------------------------------------------------- CHD Manager's own updates (GitHub releases)
APP_REPO = "Mr-Shizzy/CHD-Manager"
APP_RELEASE_API = f"https://api.github.com/repos/{APP_REPO}/releases/latest"
APP_URL_PREFIX = f"https://github.com/{APP_REPO}/"
SWAP_BAT = """@echo off
setlocal
set "NEW={new}"
set "OLD={old}"
for /l %%i in (1,1,90) do (
  move /y "%NEW%" "%OLD%" >nul 2>&1 && goto swapped
  timeout /t 1 /nobreak >nul
)
echo Could not replace the program. The new version was saved as: %NEW%
pause
exit /b 1
:swapped
set "PYINSTALLER_RESET_ENVIRONMENT=1"
start "" "%OLD%"
(goto) 2>nul & del "%~f0"
"""


def ver_tuple(s):
    """'v1.2.0' / '1.2' -> (1, 2, 0)"""
    nums = [int(x) for x in re.findall(r"\d+", s or "")][:3]
    return tuple(nums + [0] * (3 - len(nums)))


def fetch_app_release():
    """Latest CHD Manager release -> dict(tag, name, url, size, sha, page, notes). Only trusts github.com/<repo>/."""
    with http_get(APP_RELEASE_API) as r:
        data = json.loads(r.read().decode("utf-8"))
    asset = next((a for a in data.get("assets", []) if re.fullmatch(r"CHD-Manager-.+-win64\.zip", a["name"])), None)
    if not asset:
        raise RuntimeError("The latest release has no Windows zip.")
    if not asset["browser_download_url"].startswith(APP_URL_PREFIX):
        raise RuntimeError("Unexpected download address: " + asset["browser_download_url"])
    body = data.get("body") or ""
    m = re.search(r"\b([0-9a-fA-F]{64})\b", body)
    return {"tag": data["tag_name"], "name": asset["name"], "url": asset["browser_download_url"],
            "size": asset["size"], "sha": m.group(1) if m else "", "page": data.get("html_url", APP_URL_PREFIX + "releases"),
            "notes": body.strip()}


def stage_app_update(info, dest_dir, progress=None):
    """Download the release zip, verify its SHA-256, and save the new exe next to the current one as
    'CHD Manager.new.exe'. Returns that path. Refuses anything without a matching checksum."""
    import zipfile
    if not info["url"].startswith(APP_URL_PREFIX):
        raise RuntimeError("Unexpected download address.")
    if not info["sha"]:
        raise RuntimeError("This release has no published checksum, so it can't be verified. Download it by hand.")
    dest_dir = Path(dest_dir)
    tmp = Path(tempfile.mkdtemp(prefix="chdm_app_update_"))
    try:
        zpath, h, got = tmp / info["name"], hashlib.sha256(), 0
        with http_get(info["url"], 60) as r, open(zpath, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                got += len(chunk)
                if progress:
                    progress(100 * got / max(info["size"], 1), f"Downloading {info['name']}: {fmt_size(got)} / {fmt_size(info['size'])}")
        if h.hexdigest().lower() != info["sha"].lower():
            raise RuntimeError("Checksum mismatch - the download was discarded.")
        with zipfile.ZipFile(zpath) as z:
            names = {Path(n).name: n for n in z.namelist() if not n.endswith("/")}
            if "CHD Manager.exe" not in names:
                raise RuntimeError("The zip does not contain CHD Manager.exe.")
            exe_bytes = z.read(names["CHD Manager.exe"])
            extras = {n: z.read(names[n]) for n in ("LICENSE", "THIRD_PARTY_NOTICES.txt") if n in names}
        if exe_bytes[:2] != b"MZ":
            raise RuntimeError("CHD Manager.exe in the zip is not a Windows program.")
        new = dest_dir / "CHD Manager.new.exe"
        new.write_bytes(exe_bytes)
        for n, b in extras.items():
            (dest_dir / n).write_bytes(b)
        return new
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def spawn_swap(script):
    """Run the swap script detached. The environment is cleaned first: a PyInstaller exe that is relaunched from
    inside itself inherits its parent's bookkeeping variables and, when the new exe has the same path as the old
    one, tries to reuse the old run's temp folder, which no longer exists ('Failed to load Python DLL')."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("_PYI") and k != "_MEIPASS2"}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    subprocess.Popen(["cmd", "/c", str(script)], creationflags=CREATE_NO_WINDOW | 0x00000008, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, env=env)


def write_swap_script(new_exe, old_exe):
    """A tiny batch file that waits for the running program to exit, swaps in the new exe, and starts it."""
    bat = Path(tempfile.gettempdir()) / f"chdm_swap_{os.getpid()}.bat"
    text = SWAP_BAT.format(new=str(new_exe), old=str(old_exe)).replace("\n", "\r\n")
    bat.write_bytes(text.encode("utf-8"))              # bytes: text mode would turn \r\n into \r\r\n
    return bat


# ------------------------------------------------------------------ smart suggestions
def total_ram_gb():
    class MEMSTAT(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    try:
        m = MEMSTAT()
        m.dwLength = ctypes.sizeof(MEMSTAT)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.ullTotalPhys / 1024 ** 3
    except Exception:  # noqa: BLE001
        return None


# chdman's own defaults (measured with chdman 0.289): CD hunk 19,584 bytes, DVD hunk 4,096 bytes
def build_suggestions(jobs, out_hint=None):
    """Settings that suit this PC and this queue. -> dict(values, rows, warnings, notes, summary)"""
    cores = os.cpu_count() or 4
    ram = total_ram_gb()
    conv = [j for j in jobs if j.kind == "convert" and not j.missing]
    n_cd = sum(1 for j in conv if j.type == "CD")
    n_dvd = len(conv) - n_cd
    total = sum(j.size for j in conv)

    # threads: leave headroom for Windows. (Measured: chdman needs only ~30-50 MB even at 24 threads, so RAM
    # is not a limit; CPU contention is what makes a weak PC feel frozen.)
    if cores <= 2:
        threads, why_t = 1, f"Detected {cores} thread(s). Using 1 leaves the rest for Windows."
    elif cores <= 11:
        threads, why_t = cores - 1, f"Detected {cores} threads. Using {cores - 1} leaves 1 free so Windows stays responsive."
    else:
        threads, why_t = cores - 2, f"Detected {cores} threads. Using {cores - 2} leaves 2 free so Windows stays responsive."
    if ram and ram < 2:
        threads = min(threads, 2)
        why_t += f" Only {ram:.1f} GB RAM, so capped at 2."
    shown_t = f"{threads} of {cores}"
    weak = cores <= 4
    potato = weak or bool(ram and ram < 8)   # few threads or under 8 GB RAM

    rows = [
        ("Compression", "Default",
         "chdman's default tries several codecs per block and keeps the best one. In tests on 40+ real discs, LZMA-only "
         "came out 0.5% larger, zlib ('Fast') about 8% larger, and zstd about 4% larger and up to 3x slower."),
        ("Hunk size", "Default",
         "chdman picks 19,584 bytes for CD images and 4,096 bytes for DVD images. In tests, doubling the hunk saved "
         "0.6-10% (about 5% on average, least on big images), and doubled hunks booted in DuckStation and Flycast, "
         "but I haven't tried every emulator, so stay on default unless you've tested yours."),
        ("Threads", shown_t, why_t + " In testing, speed stopped improving at around 16 threads." + (
            " Expect long conversions on a PC this size - that's normal; leave it running." if weak else "")),
        ("Low priority", "On" if potato else "Off",
         ("This looks like a modest PC (" + f"{cores} threads" + (f", {ram:.0f} GB RAM" if ram else "") + "), so chdman runs "
          "below your other programs and the PC stays usable. It still uses all free CPU time when you're idle.")
         if potato else
         f"Not needed: {cores} threads" + (f" and {ram:.0f} GB RAM" if ram else "") +
         " is plenty, and the spare threads already keep Windows responsive. You can still tick it in Settings, Convert page."),
        ("If output exists", "Skip", "Never overwrites a CHD you already made; re-run safely after an interruption."),
        ("Verify after create", "On", "Checks each CHD's SHA-1 before you trust it (and before any source is recycled)."),
    ]
    warnings, notes = [], []

    if total:
        est = total * 0.70      # typical CHD is 40-70% of the source; assume the worse end
        try:
            free = shutil.disk_usage(out_hint or conv[0].path.parent).free
            if free < est * 1.1:
                warnings.append(f"Low disk space: about {fmt_size(est)} may be needed but only {fmt_size(free)} is free "
                                "where the CHDs will be written. Pick another Output folder.")
        except OSError:
            pass
        if total > 20 * 1024 ** 3:
            notes.append(f"{fmt_size(total)} queued - this will take a while. Verify roughly doubles the disk reading, "
                         "but it's worth leaving on for a batch this size.")
    plats = {j.platform for j in conv if j.platform}
    if any(j.platform == "PS2" and j.type == "DVD" for j in conv):
        notes.append("Big PS2 ISOs become DVD-type CHDs ('createdvd'). Many PS2 CHDs you find online are CD-type instead. "
                     "Both are valid CHDs, and both booted in a PCSX2 nightly test, but check one game boots before converting a collection.")
    if any(j.platform == "PSP" for j in conv):
        notes.append("PSP images are treated as DVD-type CHDs.")
    if "Dreamcast" in plats:
        notes.append("Dreamcast .gdi images are CD-type CHDs; keep the .gdi and all its track files together.")
    if any(j.lone_bin for j in conv):
        notes.append("A lone .bin gets a temporary CUE assuming MODE2/2352 (right for most PS1 games). "
                     "If a game fails to boot, find its real .cue.")
    unknown = [j for j in conv if j.path.suffix.lower() == ".iso" and not j.platform]
    if unknown:
        notes.append(f"{len(unknown)} ISO(s) have no recognizable platform; CD/DVD was chosen by size only. "
                     "Right-click to force CD or DVD if one is wrong.")

    values = {"compression": "Default", "custom_comp": "", "hunk": "", "threads": threads, "exists": "Skip",
              "verify_after": True, "low_priority": potato}
    summary = (f"This PC: {cores} threads" + (f", {ram:.0f} GB RAM" if ram else "") + "\n"
               + (f"Queue: {len(conv)} to convert, {fmt_size(total)} ({n_cd} CD, {n_dvd} DVD)" if conv
                  else "Queue: nothing to convert yet - add files and this updates."))
    return {"values": values, "rows": rows, "warnings": warnings, "notes": notes, "summary": summary}


# ------------------------------------------- PBP and CDI images: unpacked to CUE/BIN, then handed to chdman
class UnpackError(Exception):
    pass


CD_SYNC = b"\x00" + b"\xff" * 10 + b"\x00"


def _bcd(n):
    return ((n // 10) << 4) | (n % 10)


def _unbcd(b):
    return (b >> 4) * 10 + (b & 15)


def cue_time(frames):
    return "%02d:%02d:%02d" % (frames // 4500, frames // 75 % 60, frames % 75)


def pbp_discs(path):
    """Byte offset of each disc inside a PlayStation .pbp (one for most games, up to five for multi-disc)."""
    with open(path, "rb") as f:
        head = f.read(0x28)
        if len(head) < 0x28 or head[:4] != b"\x00PBP":
            raise UnpackError("not a PBP file")
        psar = struct.unpack("<I", head[0x24:0x28])[0]
        f.seek(psar)
        magic = f.read(16)
        if magic.startswith(b"PSISOIMG0000"):
            return [psar]
        if magic.startswith(b"PSTITLEIMG000000"):
            f.seek(psar + 0x200)
            offs = [o for o in struct.unpack("<5I", f.read(20)) if o]
            if offs:
                return [psar + o for o in offs]
    raise UnpackError("this PBP is not a PlayStation 1 game (PSP games and other PBPs can't be converted)")


def pbp_toc(f, base):
    """-> ([(is_data, start_lba)], total_sectors) for one disc section."""
    f.seek(base + 0x800)
    raw = f.read(0x400)
    tracks, total = [], 0
    for i in range(0, len(raw) - 9, 10):
        e = raw[i:i + 10]
        point = e[2]
        if point == 0:
            break
        lba = (_unbcd(e[7]) * 60 + _unbcd(e[8])) * 75 + _unbcd(e[9]) - 150
        if point == 0xA2:
            total = lba
        elif point < 0xA0:
            tracks.append((e[0] == 0x41, lba))
    if not tracks or total <= 0:
        raise UnpackError("the track table in this PBP is unreadable")
    return tracks, total


def unpack_pbp_disc(path, base, outdir, stem, progress, stopped):
    """Decompress one disc of a PBP into outdir/stem.bin + stem.cue. Returns the cue path."""
    outdir = Path(outdir)
    bin_path, cue_path = outdir / (stem + ".bin"), outdir / (stem + ".cue")
    with open(path, "rb") as f:
        tracks, total = pbp_toc(f, base)
        blocks = -(-total // 16)
        f.seek(base + 0x4000)
        index = f.read(blocks * 32)
        if len(index) < blocks * 32:
            raise UnpackError("the block table in this PBP is cut short")
        data0, full = base + 0x100000, 16 * 2352
        left = total * 2352
        with open(bin_path, "wb") as g:
            for b in range(blocks):
                off, ln = struct.unpack("<IH", index[b * 32:b * 32 + 6])
                f.seek(data0 + off)
                raw = f.read(ln)
                try:
                    chunk = raw if ln == full else zlib.decompress(raw, -15)
                except zlib.error:
                    raise UnpackError(f"block {b} of this PBP is damaged") from None
                if len(chunk) != full and b != blocks - 1:
                    raise UnpackError(f"block {b} of this PBP has the wrong size")
                g.write(chunk[:left])
                left -= len(chunk)
                if b % 64 == 0:
                    progress(b / blocks)
                    if stopped():
                        raise UnpackError("stopped")
    lines = [f'FILE "{bin_path.name}" BINARY']
    for n, (is_data, lba) in enumerate(tracks, 1):
        lines += [f"  TRACK {n:02d} {'MODE2/2352' if is_data else 'AUDIO'}", f"    INDEX 01 {cue_time(max(lba, 0))}"]
    cue_path.write_text("\n".join(lines) + "\n", "utf-8")
    progress(1.0)
    return cue_path


CDI_V2, CDI_V3, CDI_V35 = 0x80000004, 0x80000005, 0x80000006
CDI_MARK = bytes([0, 0, 1, 0, 0, 0, 255, 255, 255, 255])


def parse_cdi(path):
    """Read a DiscJuggler .cdi. -> list of tracks: dict(session, mode 0=audio/1/2, ss=sector size, pregap,
    length, start_lba, position=byte offset of the track in the file)."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        if size < 16:
            raise UnpackError("file too short to be a CDI")
        f.seek(size - 8)
        version, hdr = struct.unpack("<II", f.read(8))
        if version not in (CDI_V2, CDI_V3, CDI_V35) or hdr == 0 or hdr > size:
            raise UnpackError("not a DiscJuggler image this program understands")
        f.seek(size - hdr if version == CDI_V35 else hdr)

        def u(n):
            b = f.read(n)
            if len(b) != n:
                raise UnpackError("the CDI's track table is cut short")
            return int.from_bytes(b, "little")

        def skip(n):
            f.seek(n, 1)

        sessions = u(2)
        if not 0 < sessions < 100:
            raise UnpackError("no sessions found in this CDI")
        tracks, position = [], 0
        for s in range(1, sessions + 1):
            for _ in range(u(2)):
                if u(4) != 0:
                    skip(8)
                if f.read(10) != CDI_MARK or f.read(10) != CDI_MARK:
                    raise UnpackError("this CDI uses a track layout that isn't supported")
                skip(4)
                skip(u(1))
                skip(19)
                if u(4) == 0x80000000:
                    skip(8)
                skip(2)
                pregap, length = u(4), u(4)
                skip(6)
                mode = u(4)
                skip(12)
                start_lba, total = u(4), u(4)
                skip(16)
                size_code = u(4)
                if size_code not in (0, 1, 2) or mode > 2:
                    raise UnpackError("this CDI has a track type that isn't supported")
                ss = (2048, 2336, 2352)[size_code]
                skip(29)
                if version != CDI_V2:
                    skip(5)
                    if u(4) == 0xFFFFFFFF:
                        skip(78)
                tracks.append(dict(session=s, mode=mode, ss=ss, pregap=pregap, length=length,
                                   start_lba=start_lba, position=position))
                position += total * ss
            skip(4 + 8 + (0 if version == CDI_V2 else 1))
        if position > size or not tracks:
            raise UnpackError("the CDI's track table doesn't match the file")
        return tracks


def unpack_cdi(path, outdir, stem, progress, stopped):
    """Write each track of a CDI as outdir/stem (Track NN).bin plus stem.cue. Returns the cue path.
    Mode 2 sectors stored without their 16-byte header get a proper one back, carrying the track's real
    address. The empty gap between sessions (as on Dreamcast CD-Rs) is not stored: players such as Flycast
    refuse CHDs with pregaps and place the second session themselves."""
    outdir = Path(outdir)
    tracks = parse_cdi(path)
    all_sectors = sum(t["length"] for t in tracks) or 1
    done = 0
    lines = []
    with open(path, "rb") as f:
        for n, t in enumerate(tracks, 1):
            mode, ss = t["mode"], t["ss"]
            if mode == 0 and ss != 2352:
                raise UnpackError("this CDI has an audio track with an unusual sector size")
            if mode == 1 and ss == 2336 or mode == 2 and ss == 2048:
                raise UnpackError("this CDI has a track type that isn't supported")
            start = t["start_lba"] + t["pregap"] - 150          # where the track's INDEX 01 really is
            name = f"{stem} (Track {n:02d}).bin"
            kind = "AUDIO" if mode == 0 else "MODE1/2048" if ss == 2048 else f"MODE{mode}/2352"
            lines += [f'FILE "{name}" BINARY', f"  TRACK {n:02d} {kind}"]
            lines.append("    INDEX 01 00:00:00")
            f.seek(t["position"] + t["pregap"] * ss)
            step = 4096
            with open(outdir / name, "wb") as g:
                for first in range(0, t["length"], step):
                    count = min(step, t["length"] - first)
                    raw = f.read(count * ss)
                    if len(raw) != count * ss:
                        raise UnpackError("the CDI file is shorter than its track table says")
                    if mode == 2 and ss == 2336:
                        parts = []
                        for i in range(count):
                            a = start + first + i + 150
                            parts.append(CD_SYNC + bytes((_bcd(a // 4500), _bcd(a // 75 % 60), _bcd(a % 75), 2)))
                            parts.append(raw[i * 2336:(i + 1) * 2336])
                        raw = b"".join(parts)
                    g.write(raw)
                    done += count
                    progress(done / all_sectors)
                    if stopped():
                        raise UnpackError("stopped")
    cue_path = outdir / (stem + ".cue")
    cue_path.write_text("\n".join(lines) + "\n", "utf-8")
    return cue_path


# ------------------- MDS/MDF, NRG, CloneCD and plain IMG: read into a track list, unpacked to CUE/BIN
IMAGE_EXTS = {".mds", ".mdf", ".nrg", ".ccd", ".img"}
VALID_STRIDES = (2048, 2336, 2352, 2368, 2448)


def _sniff_kind(f, off, stride):
    """AUDIO / MODE1 / MODE2 from the first sector of a track."""
    if stride == 2048:
        return "MODE1"
    if stride == 2336:
        return "MODE2"
    f.seek(off)
    s = f.read(16)
    if s[:12] != CD_SYNC:
        return "AUDIO"
    return "MODE2" if s[15] == 2 else "MODE1"


def _mk_track(path, f, size, off, stride, count, kind=None, gap=0):
    """One track of a disc image: file, byte offset of its first sector, bytes per sector as stored (stride),
    payload sector size (ss), sector count, kind, and how many sectors of the next track's pregap follow it."""
    if stride not in VALID_STRIDES:
        raise UnpackError(f"this image stores sectors of {stride} bytes, which isn't supported")
    if count <= 0 or off < 0:
        raise UnpackError("this image's track table is damaged")
    if off + count * stride > size:
        count = (size - off) // stride
        gap = 0
        if count <= 0:
            raise UnpackError("the image file is shorter than its track table says")
    return dict(file=Path(path), off=off, stride=stride, ss=min(stride, 2352), count=count, gap=gap,
                kind=kind or _sniff_kind(f, off, stride))


def read_mds(path):
    """Alcohol 120% .mds (version 1.x) + its .mdf data file -> track list."""
    path = Path(path)
    d = path.read_bytes()
    if len(d) < 0x58 or d[:16] != b"MEDIA DESCRIPTOR":
        raise UnpackError("not an Alcohol .mds file")
    if d[16] != 1:
        raise UnpackError("this .mds is a newer version than this program understands (only version 1 is supported)")
    try:
        if struct.unpack_from("<H", d, 0x14)[0] != 1:
            raise UnpackError("multi-session .mds images aren't supported")
        so = struct.unpack_from("<I", d, 0x50)[0]
        _s, s_end, _n, nblk, _sp, _f, _l, _z, dbo = struct.unpack_from("<iiHBBHHII", d, so)
        blocks = [d[dbo + i * 0x50:dbo + (i + 1) * 0x50] for i in range(nblk)]
        trk = sorted((b for b in blocks if len(b) == 0x50 and 1 <= b[4] <= 99), key=lambda b: b[4])
        if not trk:
            raise UnpackError("no tracks found in this .mds")
        if any(struct.unpack_from("<I", b, 0x30)[0] != 1 for b in trk):
            raise UnpackError("this .mds spreads the disc over several data files, which isn't supported")
        footer = struct.unpack_from("<I", trk[0], 0x34)[0]
        noff, wide = struct.unpack_from("<II", d, footer)
        raw = d[noff:noff + 520]
        name = raw.decode("utf-16le", "ignore").split("\0")[0] if wide else raw.split(b"\0")[0].decode("latin1")
    except struct.error:
        raise UnpackError("this .mds file is damaged") from None
    cands = []
    if name.startswith("*."):
        cands.append(path.with_suffix(name[1:]))
    elif name:
        cands.append(path.parent / Path(name).name)
    cands += [path.with_suffix(e) for e in (".mdf", ".img")]
    img = next((c for c in cands if c.is_file()), None)
    if not img:
        raise UnpackError("the data file (.mdf) that belongs to this .mds wasn't found next to it")
    size = img.stat().st_size
    out = []
    with open(img, "rb") as f:
        for i, b in enumerate(trk):
            stride = struct.unpack_from("<H", b, 0x10)[0]
            start, off = struct.unpack_from("<IQ", b, 0x24)
            end = struct.unpack_from("<I", trk[i + 1], 0x24)[0] if i + 1 < len(trk) else s_end
            out.append(_mk_track(img, f, size, off, stride, end - start, "AUDIO" if b[0] == 0xA9 else None))
    return out


NRG_TAO_STRIDE = {0: 2048, 2: 2336, 3: 2352, 5: 2352, 6: 2352, 7: 2352, 0x0f: 2448, 0x10: 2448, 0x11: 2448}


def read_nrg(path):
    """Nero .nrg (v1 and v2) -> track list."""
    path = Path(path)
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        if size < 16:
            raise UnpackError("not a Nero .nrg file")
        f.seek(size - 12)
        t = f.read(12)
        if t[:4] == b"NER5":
            cpos = struct.unpack(">Q", t[4:])[0]
        elif t[4:8] == b"NERO":
            cpos = struct.unpack(">I", t[8:])[0]
        else:
            raise UnpackError("not a Nero .nrg file")
        if not 0 < cpos < size:
            raise UnpackError("the track table of this .nrg is damaged")
        f.seek(cpos)
        chunks = []
        while f.tell() + 8 <= size:
            cid = f.read(4)
            ln = struct.unpack(">I", f.read(4))[0]
            if cid == b"END!":
                break
            chunks.append((cid, f.read(ln)))
        ids = [c for c, _ in chunks]
        if ids.count(b"SINF") > 1 or ids.count(b"DAOX") + ids.count(b"DAOI") > 1:
            raise UnpackError("multi-session .nrg images aren't supported")
        out = []
        dao = next(((c, b) for c, b in chunks if c in (b"DAOX", b"DAOI")), None)
        if dao:
            wide = dao[0] == b"DAOX"
            ent, body = (42 if wide else 30), dao[1]
            n = body[21] - body[20] + 1 if len(body) >= 22 else 0
            if n < 1 or len(body) < 22 + n * ent:
                raise UnpackError("this .nrg uses a track layout that isn't understood")
            rows = []
            for i in range(n):
                e = body[22 + i * ent:22 + (i + 1) * ent]
                stride = struct.unpack(">H", e[12:14])[0]
                rows.append((stride,) + struct.unpack(">3Q" if wide else ">3I", e[18:]))
            for i, (stride, idx0, idx1, end) in enumerate(rows):
                if stride not in VALID_STRIDES or end <= idx1 or (end - idx1) % stride:
                    raise UnpackError("this .nrg uses a track layout that isn't understood")
                gap = 0
                if i + 1 < len(rows) and rows[i + 1][0] == stride and rows[i + 1][2] >= end:
                    gap = (rows[i + 1][2] - end) // stride
                out.append(_mk_track(path, f, cpos, idx1, stride, (end - idx1) // stride, gap=gap))
        else:
            for cid, body in chunks:
                if cid == b"ETN2" and len(body) >= 32:
                    off, ln, mode, _u, _sec, cnt = struct.unpack(">QQIIII", body[:32])
                elif cid == b"ETNF" and len(body) >= 20:
                    off, ln, mode, _u, _sec = struct.unpack(">5I", body[:20])
                    cnt = 0
                else:
                    continue
                stride = ln // cnt if cnt and ln % cnt == 0 else NRG_TAO_STRIDE.get(mode, 0)
                if stride not in VALID_STRIDES or ln % stride:
                    raise UnpackError("this .nrg has a track type that isn't supported")
                out.append(_mk_track(path, f, cpos, off, stride, ln // stride))
        if not out:
            raise UnpackError("no tracks found in this .nrg")
        return out


def read_ccd(path):
    """CloneCD .ccd + .img (raw 2352-byte sectors) -> track list. The .sub file is not used."""
    path = Path(path)
    text = path.read_text("latin1")
    img = path.with_suffix(".img")
    if not img.is_file():
        raise UnpackError("the .img file that belongs to this .ccd wasn't found next to it")
    sess = re.search(r"^\s*Sessions\s*=\s*(\d+)", text, re.M | re.I)
    if sess and int(sess.group(1)) > 1:
        raise UnpackError("multi-session CloneCD images aren't supported")
    trk = []
    for m in re.finditer(r"^\[TRACK (\d+)\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S | re.I):
        mode = re.search(r"^\s*MODE\s*=\s*(\d+)", m.group(2), re.M | re.I)
        idx = re.search(r"^\s*INDEX 1\s*=\s*(\d+)", m.group(2), re.M | re.I)
        if not (mode and idx):
            raise UnpackError("this .ccd has a track without a mode or start position")
        trk.append((int(m.group(1)), int(mode.group(1)), int(idx.group(1))))
    if not trk:                                     # some writers only give the [Entry] table of contents
        for m in re.finditer(r"^\[Entry \d+\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S | re.I):
            pt = re.search(r"^\s*Point\s*=\s*(0x[0-9a-f]+|\d+)", m.group(1), re.M | re.I)
            ctl = re.search(r"^\s*Control\s*=\s*(0x[0-9a-f]+|\d+)", m.group(1), re.M | re.I)
            lba = re.search(r"^\s*PLBA\s*=\s*(-?\d+)", m.group(1), re.M | re.I)
            if pt and ctl and lba and 1 <= int(pt.group(1), 0) <= 99:
                trk.append((int(pt.group(1), 0), 0 if not int(ctl.group(1), 0) & 4 else 1, int(lba.group(1))))
    trk.sort()
    if not trk:
        raise UnpackError("no tracks found in this .ccd")
    size = img.stat().st_size
    if size % 2352:
        raise UnpackError("the .img isn't made of 2352-byte sectors, so it can't belong to this .ccd")
    total, out = size // 2352, []
    with open(img, "rb") as f:
        for i, (_n, mode, start) in enumerate(trk):
            end = trk[i + 1][2] if i + 1 < len(trk) else total
            out.append(_mk_track(img, f, size, start * 2352, 2352, end - start, "AUDIO" if mode == 0 else None))
    return out


def read_plain_img(path):
    """A bare .img: either raw 2352-byte sectors (a BIN) or a 2048-byte ISO. One data track."""
    path = Path(path)
    size = path.stat().st_size
    with open(path, "rb") as f:
        raw = f.read(12) == CD_SYNC
        if raw and size % 2352 == 0:
            return [_mk_track(path, f, size, 0, 2352, size // 2352)]
        if not raw and size % 2048 == 0 and size:
            return [_mk_track(path, f, size, 0, 2048, size // 2048)]
    raise UnpackError("can't tell what kind of disc image this .img is")


def image_primary(path):
    """The file that describes an image set: dropping a .mdf means its .mds, and so on."""
    path = Path(path)
    if path.suffix.lower() == ".mdf" and path.with_suffix(".mds").is_file():
        return path.with_suffix(".mds")
    if path.suffix.lower() == ".sub" and path.with_suffix(".ccd").is_file():
        return path.with_suffix(".ccd")
    return path


def read_image(path):
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".mds":
        return read_mds(path)
    if ext == ".nrg":
        return read_nrg(path)
    if ext == ".ccd":
        return read_ccd(path)
    if ext == ".img":
        if path.with_suffix(".ccd").is_file():
            return read_ccd(path.with_suffix(".ccd"))
        if path.with_suffix(".mds").is_file():
            return read_mds(path.with_suffix(".mds"))
        return read_plain_img(path)
    raise UnpackError("not a disc image type this program reads")


def image_sources(path, tracks):
    """Every file that belongs to an image set (for size, and for the Recycle Bin after a verified conversion)."""
    path = Path(path)
    primary = path
    if path.suffix.lower() == ".img":
        primary = next((path.with_suffix(e) for e in (".ccd", ".mds") if path.with_suffix(e).is_file()), path)
    files = [path, primary] + [t["file"] for t in tracks]
    if primary.suffix.lower() == ".ccd" and primary.with_suffix(".sub").is_file():
        files.append(primary.with_suffix(".sub"))
    seen, out = set(), []
    for f in files:
        if str(f).lower() not in seen:
            seen.add(str(f).lower())
            out.append(f)
    return out


def unpack_tracks(tracks, outdir, stem, progress, stopped, iso_ok=False):
    """Write a track list as CUE/BIN (one BIN per track) in outdir. 2336-byte mode 2 sectors get their sync and
    header back, sectors stored with subchannel data lose it (a CHD can't hold it). With iso_ok, a lone 2048-byte
    data track becomes stem.iso instead. Returns the .cue (or .iso) path."""
    outdir = Path(outdir)
    total = sum(t["count"] for t in tracks) or 1
    done = lba = 0
    lines = []
    for n, t in enumerate(tracks, 1):
        stride, ss, kind = t["stride"], t["ss"], t["kind"]
        count = t["count"]
        if n < len(tracks) and tracks[n]["stride"] == stride:
            count += t["gap"]                       # the next track's pregap is part of this track's sectors
        if kind == "AUDIO":
            cue_kind = "AUDIO"
        elif ss == 2048 and kind == "MODE1":
            cue_kind = "MODE1/2048"
        elif ss in (2336, 2352) and kind == "MODE2" or ss == 2352 and kind == "MODE1":
            cue_kind = "MODE2/2352" if kind == "MODE2" else "MODE1/2352"
        else:
            raise UnpackError("this image has a track type that isn't supported")
        if kind == "AUDIO" and ss != 2352:
            raise UnpackError("this image has an audio track with an unusual sector size")
        iso = iso_ok and len(tracks) == 1 and cue_kind == "MODE1/2048"
        name = f"{stem}.iso" if iso else f"{stem}.bin" if len(tracks) == 1 else f"{stem} (Track {n:02d}).bin"
        lines += [f'FILE "{name}" BINARY', f"  TRACK {n:02d} {cue_kind}", "    INDEX 01 00:00:00"]
        with open(t["file"], "rb") as f, open(outdir / name, "wb") as g:
            f.seek(t["off"])
            for first in range(0, count, 2048):
                c = min(2048, count - first)
                raw = f.read(c * stride)
                if len(raw) != c * stride:
                    raise UnpackError("the image file is shorter than its track table says")
                if stride != ss:
                    raw = b"".join(raw[i * stride:i * stride + ss] for i in range(c))
                if ss == 2336:
                    parts = []
                    for i in range(c):
                        a = lba + first + i + 150
                        parts.append(CD_SYNC + bytes((_bcd(a // 4500), _bcd(a // 75 % 60), _bcd(a % 75), 2)))
                        parts.append(raw[i * 2336:(i + 1) * 2336])
                    raw = b"".join(parts)
                g.write(raw)
                done += c
                progress(done / total)
                if stopped():
                    raise UnpackError("stopped")
        lba += count
        if iso:
            return outdir / name
    cue_path = outdir / (stem + ".cue")
    cue_path.write_text("\n".join(lines) + "\n", "utf-8")
    return cue_path


def image_platform(tracks):
    for t in tracks:
        if t["kind"] != "AUDIO":
            return sniff_platform(t["file"], t["off"]) if t["stride"] in (2048, 2352) else ""
    return ""


# ------------------------------------------------------ plain 2048-byte ISO -> raw 2352-byte sectors (for PBP)
def _edc_tables():
    t1 = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ (0xD8018001 if c & 1 else 0)
        t1.append(c)
    t2 = []
    for x in range(65536):                    # two zero bytes fed to a CRC whose low 16 bits are x
        c = x
        for _ in range(2):
            c = (c >> 8) ^ t1[c & 0xFF]
        t2.append(c)
    return t2


def _ecc_tables():
    f = [((i << 1) ^ (0x11D if i & 0x80 else 0)) & 0xFF for i in range(256)]
    b = [0] * 256
    for i in range(256):
        b[i ^ f[i]] = i

    def kth(n):                               # byte -> parity contribution of a byte that is followed by n more steps
        out = bytearray(256)
        for t in range(256):
            v = t
            for _ in range(n + 1):
                v = f[v]
            out[t] = b[v ^ t]
        return bytes(out)
    return kth


_EDC_T2 = None
_ECC_P_TABLES = _ECC_Q_TABLES = _ECC_Q_PERM = None


def _ecc_setup():
    global _EDC_T2, _ECC_P_TABLES, _ECC_Q_TABLES, _ECC_Q_PERM
    if _EDC_T2 is not None:
        return
    _EDC_T2 = _edc_tables()
    kth = _ecc_tables()
    _ECC_P_TABLES = [kth(24 - k) for k in range(24)]
    _ECC_Q_TABLES = [kth(43 - k) for k in range(43)]
    perm = [0] * (52 * 43)
    for major in range(52):
        index = (major >> 1) * 86 + (major & 1)
        for minor in range(43):
            perm[minor * 52 + major] = index
            index += 88
            if index >= 2236:
                index -= 2236
    _ECC_Q_PERM = perm


def _ecc_parity(rows, tables, width):
    a = b = 0
    for k, tab in enumerate(tables):
        row = rows[k * width:(k + 1) * width]
        a ^= int.from_bytes(row.translate(tab), "little")
        b ^= int.from_bytes(row, "little")
    return a.to_bytes(width, "little") + (a ^ b).to_bytes(width, "little")


def raw_form1_sector(lba, user, sub=b"\x00\x00\x08\x00\x00\x00\x08\x00"):
    """The 2352-byte Mode 2 Form 1 sector for 2048 bytes of data: sync, MSF header, subheader, data, EDC and ECC."""
    import operator
    _ecc_setup()
    msf = lba + 150
    body = bytearray(b"\x00\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\x00")
    body += bytes((_bcd(msf // 4500), _bcd(msf // 75 % 60), _bcd(msf % 75), 2)) + sub + user
    c = 0
    for w in struct.unpack("<1028H", body[16:2072]):
        c ^= w
        c = _EDC_T2[c & 0xFFFF] ^ (c >> 16)
    body += struct.pack("<I", c)
    src = bytes(4) + bytes(body[16:2076])                 # the address is treated as zero for Mode 2
    p = _ecc_parity(src[:2064], _ECC_P_TABLES, 86)
    q = _ecc_parity(bytes(operator.itemgetter(*_ECC_Q_PERM)(src + p)), _ECC_Q_TABLES, 52)
    return bytes(body) + p + q


def cook_to_raw(iso, dst, progress, stopped):
    """A plain ISO (2048-byte sectors) -> one raw MODE2/2352 BIN. The usual way of making a PS1 disc image from an ISO:
    data sectors are rebuilt in full, but CD-XA audio and video (Form 2 sectors) can't come from a plain ISO."""
    total = os.path.getsize(iso) // 2048
    with open(iso, "rb") as f, open(dst, "wb") as g:
        for n in range(total):
            g.write(raw_form1_sector(n, f.read(2048)))
            if n % 512 == 0:
                progress(n / max(total, 1))
                if stopped():
                    raise UnpackError("stopped")
    progress(1.0)


def _edc(data):
    c = 0
    for w in struct.unpack("<%dH" % (len(data) // 2), data):
        c ^= w
        c = _EDC_T2[c & 0xFFFF] ^ (c >> 16)
    return c


def fix_sector(s):
    """A raw Mode 1 or Mode 2 Form 1 sector with its EDC and ECC recomputed from its own data (header and subheader kept).
    Any other kind of sector (audio, Form 2) is returned as it was."""
    import operator
    if len(s) == 2352 and s[:12] == b"\x00" + b"\xff" * 10 + b"\x00" and s[15] == 1:
        return mode1_sector(s[12:15], s[16:2064])
    if len(s) != 2352 or s[:12] != b"\x00" + b"\xff" * 10 + b"\x00" or s[15] != 2 or s[18] & 0x20 or s[22] & 0x20:
        return s
    _ecc_setup()
    body = bytearray(s[:2072])
    body += struct.pack("<I", _edc(bytes(body[16:2072])))
    src = bytes(4) + bytes(body[16:2076])
    p = _ecc_parity(src[:2064], _ECC_P_TABLES, 86)
    q = _ecc_parity(bytes(operator.itemgetter(*_ECC_Q_PERM)(src + p)), _ECC_Q_TABLES, 52)
    return bytes(body) + p + q


def repair_bin(src, dst, progress, stopped):
    """Copy a raw BIN, rebuilding the EDC/ECC of every Mode 2 Form 1 sector. -> number of sectors that changed."""
    total = max(1, os.path.getsize(src) // 2352)
    changed = 0
    with open(src, "rb") as f, open(dst, "wb") as g:
        n = 0
        while True:
            s = f.read(2352)
            if not s:
                break
            fixed = fix_sector(s)
            changed += fixed != s
            g.write(fixed)
            n += 1
            if n % 512 == 0:
                progress(n / total)
                if stopped():
                    raise UnpackError("stopped")
    progress(1.0)
    return changed


def sniff_raw_image(path):
    """'MODE1/2352' or 'MODE2/2352' if the file starts with a raw CD sector (sync pattern), else '' (a plain image)."""
    try:
        with open(path, "rb") as f:
            h = f.read(16)
    except OSError:
        return ""
    if len(h) == 16 and h[:12] == b"\x00" + b"\xff" * 10 + b"\x00" and h[15] in (1, 2):
        return f"MODE{h[15]}/2352"
    return ""


# ------------------------------------------------------------------ Mode 1 sectors, ECM, LZ4
_SYNC = b"\x00" + b"\xff" * 10 + b"\x00"
_EDC_T1 = None


def _edc_crc(c, data):
    """Continue the CD EDC checksum c over data (any length); start from 0."""
    global _EDC_T1
    _ecc_setup()
    n = len(data) & ~1
    if n:
        for w in struct.unpack("<%dH" % (n // 2), data[:n]):
            c ^= w
            c = _EDC_T2[c & 0xFFFF] ^ (c >> 16)
    if len(data) > n:
        if _EDC_T1 is None:
            t1 = []
            for i in range(256):
                x = i
                for _ in range(8):
                    x = (x >> 1) ^ (0xD8018001 if x & 1 else 0)
                t1.append(x)
            _EDC_T1 = t1
        for b in data[n:]:
            c = (c >> 8) ^ _EDC_T1[(c ^ b) & 0xFF]
    return c


def _pq(src):
    """P and Q parity (172 + 104 bytes) of the 2064 bytes a sector's ECC covers."""
    import operator
    _ecc_setup()
    p = _ecc_parity(src, _ECC_P_TABLES, 86)
    return p + _ecc_parity(bytes(operator.itemgetter(*_ECC_Q_PERM)(src + p)), _ECC_Q_TABLES, 52)


def mode1_sector(addr, user):
    """A full 2352-byte Mode 1 sector from its 3 address bytes and 2048 bytes of data: sync, header, EDC, ECC."""
    body = bytearray(_SYNC) + bytes(addr) + b"\x01" + user
    body += struct.pack("<I", _edc_crc(0, bytes(body))) + bytes(8)
    return bytes(body) + _pq(bytes(body[12:2076]))


def xa_body(flags_data, form2=False):
    """The 2336 bytes after the header of a Mode 2 sector: flags (4 bytes + the same again), data, EDC and (Form 1) ECC.
    flags_data = the 4 flag bytes followed by 2048 (Form 1) or 2324 (Form 2) bytes of data."""
    body = bytearray(flags_data[:4] + flags_data)
    body += struct.pack("<I", _edc_crc(0, bytes(body)))
    if form2:
        return bytes(body)
    return bytes(body) + _pq(bytes(4) + bytes(body))         # the address counts as zero in a Mode 2 sector's ECC


def ecm_head(typ, num):
    """The type/count bytes of an ECM record (num >= 1)."""
    num -= 1
    c = typ | ((num & 0x1F) << 2)
    num >>= 5
    out = bytearray()
    while num:
        out.append(c | 0x80)
        c = num & 0x7F
        num >>= 7
    out.append(c)
    return bytes(out)


def ecm_decode(src, dst, progress, stopped):
    """ECM (Neill Corlett's error-code-modeler format) -> the original CD image. -> the track type to put in a CUE:
    'MODE1/2352', 'MODE2/2352', 'MODE2/2336' or ''. The checksum stored at the end of the file is checked."""
    total = max(1, os.path.getsize(src))
    crc, first, done = 0, b"", 0
    with open(src, "rb") as f, open(dst, "wb") as g:
        if f.read(4) != b"ECM\x00":
            raise UnpackError("this is not an ECM file")

        def need(n):
            b = f.read(n)
            if len(b) != n:
                raise UnpackError("the ECM file is cut short")
            return b
        while True:
            c = need(1)[0]
            typ, num, bits = c & 3, (c >> 2) & 0x1F, 5
            while c & 0x80:
                c = need(1)[0]
                num |= (c & 0x7F) << bits
                bits += 7
            if num == 0xFFFFFFFF:
                break
            num += 1
            if typ == 0:
                while num:
                    chunk = need(min(num, 1 << 20))
                    if len(first) < 16:
                        first += chunk[:16]
                    g.write(chunk)
                    crc = _edc_crc(crc, chunk)
                    num -= len(chunk)
            else:
                for _ in range(num):
                    if typ == 1:
                        a = need(3)
                        sec = mode1_sector(a, need(2048))
                    elif typ == 2:
                        sec = xa_body(need(2052))
                    else:
                        sec = xa_body(need(2328), True)
                    if len(first) < 16:
                        first += sec[:16] if typ == 1 else b""
                    g.write(sec)
                    crc = _edc_crc(crc, sec)
                    done += 1
                    if done % 256 == 0:
                        progress(f.tell() / total)
                        if stopped():
                            raise UnpackError("stopped")
            progress(f.tell() / total)
            if stopped():
                raise UnpackError("stopped")
        if struct.unpack("<I", need(4))[0] != crc:
            raise UnpackError("the ECM file is damaged (its checksum doesn't match)")
        size = g.tell()
    progress(1.0)
    if len(first) >= 16 and first[:12] == _SYNC and first[15] in (1, 2) and size % 2352 == 0:
        return f"MODE{first[15]}/2352"
    return "MODE2/2336" if size and size % 2336 == 0 else ""


def ecm_encode(src, dst, progress, stopped):
    """A raw 2352-byte-sector CD image -> ECM. Mode 1 sectors and Mode 2 Form 1/Form 2 sectors that carry correct EDC/ECC
    lose those bytes; everything else is stored as it is."""
    total = os.path.getsize(src)
    n = max(1, total // 2352)
    crc = 0
    state = {"typ": None, "count": 0, "parts": [], "bytes": 0}

    def flush(g):
        if state["typ"] is None:
            return
        g.write(ecm_head(state["typ"], state["bytes"] if state["typ"] == 0 else state["count"]))
        g.write(b"".join(state["parts"]))
        state.update(typ=None, count=0, parts=[], bytes=0)

    def add(g, typ, payload):
        if state["typ"] != typ or state["bytes"] > (1 << 22) or state["count"] >= (1 << 16):
            flush(g)
        state["typ"] = typ
        state["parts"].append(payload)
        state["bytes"] += len(payload)
        state["count"] += 1
    with open(src, "rb") as f, open(dst, "wb") as g:
        g.write(b"ECM\x00")
        for i in range(n + 1):
            s = f.read(2352)
            if not s:
                break
            crc = _edc_crc(crc, s)
            if len(s) == 2352 and s[:12] == _SYNC and s[15] == 1 and mode1_sector(s[12:15], s[16:2064]) == s:
                add(g, 1, s[12:15] + s[16:2064])
            elif len(s) == 2352 and s[:12] == _SYNC and s[15] == 2 and s[16:20] == s[20:24] and not s[18] & 0x20 \
                    and xa_body(s[20:2072]) == s[16:]:
                add(g, 0, s[:16])
                add(g, 2, s[20:2072])
            elif len(s) == 2352 and s[:12] == _SYNC and s[15] == 2 and s[16:20] == s[20:24] and s[18] & 0x20 \
                    and xa_body(s[20:2348], True) == s[16:]:
                add(g, 0, s[:16])
                add(g, 3, s[20:2348])
            else:
                add(g, 0, s)
            if i % 256 == 0:
                progress(i / n)
                if stopped():
                    raise UnpackError("stopped")
        flush(g)
        g.write(ecm_head(0, 0x100000000) + struct.pack("<I", crc))
    progress(1.0)


def verify_ecm(ecm, image, progress, stopped):
    """True when the ECM file decodes to exactly this image."""
    tmp = Path(tempfile.mkdtemp(prefix="chdm_ecm_"))
    try:
        out = tmp / "t.bin"
        ecm_decode(ecm, out, progress, stopped)
        return out.stat().st_size == Path(image).stat().st_size and files_equal(out, image)
    except UnpackError:
        if stopped():
            raise
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def files_equal(a, b):
    with open(a, "rb") as f, open(b, "rb") as g:
        while True:
            x, y = f.read(1 << 22), g.read(1 << 22)
            if x != y:
                return False
            if not x:
                return True


def lz4_decompress(src, size):
    """One LZ4 block -> exactly `size` bytes (extra bytes after the block are ignored)."""
    out, i = bytearray(), 0
    try:
        while len(out) < size:
            tok = src[i]
            i += 1
            ll = tok >> 4
            if ll == 15:
                while True:
                    b = src[i]
                    i += 1
                    ll += b
                    if b != 255:
                        break
            out += src[i:i + ll]
            i += ll
            if len(out) >= size:
                break
            off = src[i] | src[i + 1] << 8
            i += 2
            ml = tok & 15
            if ml == 15:
                while True:
                    b = src[i]
                    i += 1
                    ml += b
                    if b != 255:
                        break
            ml += 4
            if off == 0 or off > len(out):
                raise ValueError
            start = len(out) - off
            if off >= ml:
                out += out[start:start + ml]
            else:
                seg = bytes(out[start:])
                out += (seg * (ml // off + 1))[:ml]
    except (IndexError, ValueError):
        raise UnpackError("a block of this ZSO is damaged") from None
    return bytes(out[:size])


def lz4_compress(data):
    """One LZ4 block (greedy matching; valid for any LZ4 decoder)."""
    n = len(data)
    out = bytearray()

    def seq(lits, off, ml):
        ll = len(lits)
        out.append(min(ll, 15) << 4 | (min(ml - 4, 15) if off else 0))
        if ll >= 15:
            r = ll - 15
            while r >= 255:
                out.append(255)
                r -= 255
            out.append(r)
        out.extend(lits)
        if off:
            out.extend((off & 255, off >> 8))
            if ml - 4 >= 15:
                r = ml - 4 - 15
                while r >= 255:
                    out.append(255)
                    r -= 255
                out.append(r)
    anchor = i = 0
    table = {}
    while i <= n - 12:
        key = data[i:i + 4]
        cand = table.get(key)
        table[key] = i
        if cand is not None and i - cand <= 65535:
            m, maxm = 4, n - 5 - i
            while m + 8 <= maxm and data[cand + m:cand + m + 8] == data[i + m:i + m + 8]:
                m += 8
            while m < maxm and data[cand + m] == data[i + m]:
                m += 1
            seq(data[anchor:i], i - cand, m)
            i += m
            anchor = i
        else:
            i += 1
    seq(data[anchor:n], 0, 0)
    return bytes(out)


# ------------------------------------------------------------------ GameCube / Wii disc images (ISO, WBFS, CISO, GCZ, WIA, RVZ)
# The WIA/RVZ, GCZ, CISO and WBFS layouts and the lagged-Fibonacci "junk" generator follow the Dolphin emulator's
# DiscIO code (GPL-2.0-or-later), re-written here in Python.
GC_MAGIC, WII_MAGIC = 0xC2339F3D, 0x5D1C9EA3
WII_ISO_SL, WII_ISO_DL = 4699979776, 8511160320
CL, CLD = 0x8000, 0x7C00                        # a Wii cluster, and the part of it that is data (the rest is hashes)
GCZ_MAGIC = b"\x01\xc0\x0b\xb1"
_BC = None


def _bcrypt():
    global _BC
    if _BC is None:
        c = ctypes
        dll = c.WinDLL("bcrypt")
        P, V, U = c.POINTER, c.c_void_p, c.c_ulong
        dll.BCryptOpenAlgorithmProvider.argtypes = [P(V), c.c_wchar_p, c.c_wchar_p, U]
        dll.BCryptSetProperty.argtypes = [V, c.c_wchar_p, V, U, U]
        dll.BCryptGenerateSymmetricKey.argtypes = [V, P(V), V, U, c.c_char_p, U, U]
        for fn in (dll.BCryptEncrypt, dll.BCryptDecrypt):
            fn.argtypes = [V, c.c_char_p, U, V, V, U, V, U, P(U), U]
        _BC = dll
    return _BC


class AesCbc:
    """AES-128 in CBC mode through Windows' own crypto library (no extra package needed)."""

    def __init__(self, key):
        dll = _bcrypt()
        self.dll = dll
        alg = ctypes.c_void_p()
        if dll.BCryptOpenAlgorithmProvider(ctypes.byref(alg), "AES", None, 0):
            raise UnpackError("Windows' AES library isn't available")
        mode = ctypes.create_unicode_buffer("ChainingModeCBC")
        dll.BCryptSetProperty(alg, "ChainingMode", ctypes.cast(mode, ctypes.c_void_p), ctypes.sizeof(mode), 0)
        self.key = ctypes.c_void_p()
        dll.BCryptGenerateSymmetricKey(alg, ctypes.byref(self.key), None, 0, bytes(key), 16, 0)

    def crypt(self, data, iv=bytes(16), decrypt=False):
        n = len(data)
        out = ctypes.create_string_buffer(n)
        res = ctypes.c_ulong()
        ivb = ctypes.create_string_buffer(bytes(iv), 16)
        fn = self.dll.BCryptDecrypt if decrypt else self.dll.BCryptEncrypt
        if fn(self.key, bytes(data), n, None, ivb, 16, out, n, ctypes.byref(res), 0):
            raise UnpackError("AES failed")
        return out.raw


# ---- the lagged-Fibonacci generator that fills the unused parts of GameCube and Wii discs
def lfg_forward(buf):
    """Advance the 521-word state (a bytearray of 2084 bytes) by one full turn."""
    fb = int.from_bytes
    buf[0:128] = (fb(buf[0:128], "little") ^ fb(buf[1956:2084], "little")).to_bytes(128, "little")
    for k in range(128, 2048, 128):
        buf[k:k + 128] = (fb(buf[k:k + 128], "little") ^ fb(buf[k - 128:k], "little")).to_bytes(128, "little")
    buf[2048:2084] = (fb(buf[2048:2084], "little") ^ fb(buf[1920:1956], "little")).to_bytes(36, "little")


def lfg_backward(buf, start_word=0, end_word=521):
    """The inverse of lfg_forward over the words start_word..end_word (the whole state by default)."""
    fb = int.from_bytes
    hi = min(end_word, 521)
    stop = max(32, start_word)
    while hi > stop:
        lo = max(hi - 32, stop)
        a, b = lo * 4, hi * 4
        buf[a:b] = (fb(buf[a:b], "little") ^ fb(buf[a - 128:b - 128], "little")).to_bytes(b - a, "little")
        hi = lo
    top = min(end_word, 32)
    if top > start_word:
        a, b = start_word * 4, top * 4
        buf[a:b] = (fb(buf[a:b], "little") ^ fb(buf[a + 1956:b + 1956], "little")).to_bytes(b - a, "little")


def _lfg_load(words):
    """521 seed-expanded words -> the generator state (the output shuffle is applied here, as Dolphin does)."""
    buf = bytearray(struct.pack(">521I", *[(x & 0xFF00FFFF) | ((x >> 2) & 0x00FF0000) for x in words]))
    for _ in range(4):
        lfg_forward(buf)
    return buf


def _lfg_expand(seed_words):
    w = list(seed_words) + [0] * 504
    for i in range(17, 521):
        w[i] = ((w[i - 17] << 23) & 0xFFFFFFFF) ^ (w[i - 16] >> 9) ^ w[i - 1]
    return w


class Lfg:
    def __init__(self, seed68, skip=0):
        self.buf = _lfg_load(_lfg_expand(struct.unpack(">17I", seed68)))
        self.pos = 0
        self.skip(skip)

    def skip(self, n):
        self.pos += n
        while self.pos >= 2084:
            lfg_forward(self.buf)
            self.pos -= 2084

    def get(self, n):
        out = []
        while n > 0:
            take = min(n, 2084 - self.pos)
            out.append(bytes(self.buf[self.pos:self.pos + take]))
            self.pos += take
            n -= take
            if self.pos == 2084:
                lfg_forward(self.buf)
                self.pos = 0
        return b"".join(out)


def lfg_find_seed(data, word_offset):
    """The seed of the generator that produced `data` (>= 521 words, starting `word_offset` words into the stream), or None."""
    if len(data) < 2084:
        return None
    words = struct.unpack(">521I", data[:2084])
    for x in words:
        if (x & 0x00C00000) != ((x >> 2) & 0x00C00000):
            return None
    mod, div = word_offset % 521, word_offset // 521
    buf = bytearray(2084)
    # the words as the generator holds them (output shuffle already applied, i.e. the bytes as they appear on disc)
    buf[mod * 4:] = data[:(521 - mod) * 4]
    buf[:mod * 4] = data[(521 - mod) * 4:2084]
    lfg_backward(buf, 0, mod)
    for _ in range(div):
        lfg_backward(buf)
    for _ in range(4):
        lfg_backward(buf)
    w = list(struct.unpack(">521I", bytes(buf)))
    # undo the shuffle: bits 16 and 17 are rebuilt from neighbouring words
    for i in range(17):
        w[i] = (w[i] & 0xFF00FFFF) | ((w[i] << 2) & 0x00FC0000) | (((w[i + 16] ^ w[i + 15]) << 9) & 0x00030000)
    seed = struct.pack(">17I", *w[:17])
    for i in range(17, 521):
        calc = ((w[i - 17] << 23) & 0xFFFFFFFF) ^ (w[i - 16] >> 9) ^ w[i - 1]
        if (calc & 0xFFFCFFFF) != ((w[i] & 0xFF00FFFF) | ((w[i] << 2) & 0x00FC0000)):
            return None
        w[i] = calc
    return seed


def common_prefix(a, b):
    n = min(len(a), len(b))
    if a[:n] == b[:n]:
        return n
    lo, step = 0, 4096
    while lo + step <= n and a[lo:lo + step] == b[lo:lo + step]:
        lo += step
    while lo < n and a[lo] == b[lo]:
        lo += 1
    return lo


def rvz_unpack(buf, data_offset):
    """Undo RVZ packing: (size, bytes) pieces, or (size | 0x80000000, 68-byte seed) pieces of generated junk."""
    out, i, n = [], 0, len(buf)
    produced = 0
    while i < n:
        size = struct.unpack_from(">I", buf, i)[0]
        i += 4
        if size & 0x80000000:
            size &= 0x7FFFFFFF
            seed = bytes(buf[i:i + 68])
            i += 68
            out.append(Lfg(seed, (data_offset + produced) % CL).get(size))
        else:
            out.append(bytes(buf[i:i + size]))
            i += size
        produced += size
    return b"".join(out)


# ---- reading the containers: every one of them presents the disc as the bytes of the ISO
class DiscImage:
    size = 0
    kind = "ISO"

    def read(self, off, n):
        raise NotImplementedError

    def close(self):
        pass

    def info(self):
        h = self.read(0, 0x60)
        if h[:4] == b"WUP-":                                  # Wii U: "WUP-P-ARPE-02-10" (product code, version)
            return dict(platform="Wii U", id=h[6:10].decode("ascii", "replace"), title="")
        wii = struct.unpack(">I", h[0x18:0x1C])[0] == WII_MAGIC
        gc = struct.unpack(">I", h[0x1C:0x20])[0] == GC_MAGIC
        return dict(platform="Wii" if wii else "GameCube" if gc else "", id=h[:6].decode("ascii", "replace"),
                    title=h[0x20:0x60].split(b"\0")[0].decode("ascii", "replace").strip())


class IsoDisc(DiscImage):
    def __init__(self, path):
        self.f = open(path, "rb")
        self.size = os.path.getsize(path)

    def read(self, off, n):
        self.f.seek(off)
        return self.f.read(n).ljust(n, b"\0") if off < self.size else bytes(n)

    def close(self):
        self.f.close()


class WuxDisc(DiscImage):
    """Wii U disc compressed with WUX: a table of sector numbers, then each different sector once."""
    kind = "WUX"

    def __init__(self, path):
        self.f = open(path, "rb")
        h = self.f.read(24)
        if len(h) < 24 or struct.unpack("<II", h[:8]) != (0x30585557, 0x1099D02E):
            raise UnpackError("this is not a WUX file")
        self.ss, _, self.size = struct.unpack("<IIQ", h[8:24])
        if self.ss < 0x100 or self.ss >= 0x10000000:
            raise UnpackError("this WUX file has an odd sector size")
        n = (self.size + self.ss - 1) // self.ss
        self.idx = struct.unpack("<%dI" % n, self.f.read(4 * n))
        self.base = (24 + 4 * n + self.ss - 1) // self.ss * self.ss

    def read(self, off, n):
        out, want = [], n
        while n > 0 and off < self.size:
            i, inb = divmod(off, self.ss)
            run = 1                                           # neighbouring sectors that are stored next to each other
            while run * self.ss - inb < n and i + run < len(self.idx) and self.idx[i + run] == self.idx[i] + run:
                run += 1
            take = min(n, run * self.ss - inb, self.size - off)
            self.f.seek(self.base + self.idx[i] * self.ss + inb)
            out.append(self.f.read(take))
            off += take
            n -= take
        return b"".join(out).ljust(want, b"\0")

    def close(self):
        self.f.close()


def write_wux(disc, dst, progress, stopped, sector=0x8000):
    """A Wii U disc -> WUX: each different 32 KiB sector is stored once and a table says where every sector of the disc is."""
    n = (disc.size + sector - 1) // sector
    base = (24 + 4 * n + sector - 1) // sector * sector
    index, seen = [0] * n, {}
    with open(dst, "wb") as g:
        g.seek(base)
        step = 128
        for a in range(0, n, step):
            chunk = disc.read(a * sector, step * sector)
            for k in range(min(step, n - a)):
                s = chunk[k * sector:(k + 1) * sector]
                d = hashlib.sha1(s).digest()
                u = seen.get(d)
                if u is None:
                    u = seen[d] = len(seen)
                    g.write(s)
                index[a + k] = u
            progress(min(1.0, (a + step) / n))
            if stopped():
                raise UnpackError("stopped")
        g.seek(0)
        g.write(struct.pack("<IIIIQ", 0x30585557, 0x1099D02E, sector, 0, disc.size) + struct.pack("<%dI" % n, *index))
    progress(1.0)


class WbfsDisc(DiscImage):
    kind = "WBFS"

    def __init__(self, path):
        p = Path(path)
        self.files = [open(p, "rb")]
        for i in range(1, 10):                          # .wbf1, .wbf2 ... when the file was split for FAT32
            q = p.with_suffix(p.suffix[:-1] + str(i))
            if not q.is_file():
                break
            self.files.append(open(q, "rb"))
        self.sizes = [os.fstat(f.fileno()).st_size for f in self.files]
        f = self.files[0]
        head = f.read(12)
        if head[:4] != b"WBFS":
            raise UnpackError("this is not a WBFS file")
        n_sec, hd_shift, wb_shift = struct.unpack(">IBB", head[4:10])
        self.hd, self.wb = 1 << hd_shift, 1 << wb_shift
        if sum(self.sizes) != n_sec * self.hd or self.wb < CL:
            raise UnpackError("this WBFS file is damaged or cut short")
        per_disc = (143432 * 2 * CL + self.wb - 1) // self.wb
        f.seek(self.hd + 256)
        self.wlba = struct.unpack(">%dH" % per_disc, f.read(2 * per_disc))
        used = [i for i, v in enumerate(self.wlba) if v]
        end = (used[-1] + 1) * self.wb if used else 0
        self.size = WII_ISO_SL if end <= WII_ISO_SL else WII_ISO_DL

    def _pread(self, pos, n):
        for f, sz in zip(self.files, self.sizes):
            if pos < sz:
                f.seek(pos)
                got = f.read(min(n, sz - pos))
                return got + (self._pread(pos + len(got), n - len(got)) if len(got) < n else b"")
            pos -= sz
        return b""

    def read(self, off, n):
        out = []
        while n > 0:
            blk, inb = divmod(off, self.wb)
            take = min(n, self.wb - inb)
            lba = self.wlba[blk] if blk < len(self.wlba) else 0
            out.append(self._pread(lba * self.wb + inb, take).ljust(take, b"\0") if lba else bytes(take))
            off += take
            n -= take
        return b"".join(out)

    def close(self):
        for f in self.files:
            f.close()


class CisoDisc(DiscImage):
    kind = "CISO"

    def __init__(self, path):
        self.f = open(path, "rb")
        h = self.f.read(0x8000)
        if len(h) < 0x8000 or h[:4] != b"CISO":
            raise UnpackError("this is not a GameCube/Wii CISO file")
        self.block = struct.unpack("<I", h[4:8])[0]
        self.pos, count = [], 0
        for v in h[8:]:
            self.pos.append(count if v == 1 else None)
            count += v == 1
        last = max((i for i, v in enumerate(self.pos) if v is not None), default=-1)
        self.size = (last + 1) * self.block

    def read(self, off, n):
        out = []
        while n > 0:
            blk, inb = divmod(off, self.block)
            take = min(n, self.block - inb)
            p = self.pos[blk] if blk < len(self.pos) else None
            if p is None:
                out.append(bytes(take))
            else:
                self.f.seek(0x8000 + p * self.block + inb)
                out.append(self.f.read(take).ljust(take, b"\0"))
            off += take
            n -= take
        return b"".join(out)

    def close(self):
        self.f.close()


class GczDisc(DiscImage):
    kind = "GCZ"

    def __init__(self, path):
        self.f = open(path, "rb")
        h = self.f.read(32)
        if h[:4] != GCZ_MAGIC:
            raise UnpackError("this is not a GCZ file")
        self.sub, self.csize, self.size, self.block, n = struct.unpack("<IQQII", h[4:32])
        self.ptr = struct.unpack("<%dQ" % n, self.f.read(8 * n))
        self.hash = struct.unpack("<%dI" % n, self.f.read(4 * n))
        self.base = 32 + 12 * n
        self.last = (None, b"")

    def _block(self, i):
        if self.last[0] == i:
            return self.last[1]
        p = self.ptr[i]
        raw_flag = p >> 63
        start = p & 0x7FFFFFFFFFFFFFFF
        end = (self.ptr[i + 1] & 0x7FFFFFFFFFFFFFFF) if i + 1 < len(self.ptr) else self.csize
        self.f.seek(self.base + start)
        raw = self.f.read(end - start)
        if zlib.adler32(raw) != self.hash[i]:
            raise UnpackError(f"the GCZ file is damaged (block {i})")
        data = raw if raw_flag else zlib.decompress(raw)
        self.last = (i, data)
        return data

    def read(self, off, n):
        out, want = [], n
        while n > 0 and off < self.size:
            blk, inb = divmod(off, self.block)
            data = self._block(blk)
            take = min(n, len(data) - inb, self.size - off)
            out.append(data[inb:inb + take])
            off += take
            n -= take
        return b"".join(out).ljust(want, b"\0")

    def close(self):
        self.f.close()


def _purge_decode(data, size):
    out = bytearray(size)
    i = 0
    while i + 8 <= len(data) - 20:
        o, s = struct.unpack_from(">II", data, i)
        out[o:o + s] = data[i + 8:i + 8 + s]
        i += 8 + s
    return bytes(out)


class RvzDisc(DiscImage):
    """WIA and RVZ files."""

    def __init__(self, path):
        self.f = open(path, "rb")
        h1 = self.f.read(0x48)
        magic = h1[:4]
        if magic not in (b"WIA\x01", b"RVZ\x01"):
            raise UnpackError("this is not a WIA or RVZ file")
        self.rvz = magic == b"RVZ\x01"
        self.kind = "RVZ" if self.rvz else "WIA"
        h2size = struct.unpack(">I", h1[12:16])[0]
        self.size, fsize = struct.unpack(">QQ", h1[36:52])
        h2 = self.f.read(h2size)
        if hashlib.sha1(h1[:52]).digest() != h1[52:72] or hashlib.sha1(h2).digest() != h1[16:36]:
            raise UnpackError("the file's header checksums don't match; it is damaged")
        if fsize != os.path.getsize(path):
            raise UnpackError("the file is cut short")
        self.disc_type, self.method, self.level, self.chunk = struct.unpack(">IIiI", h2[:16])
        self.dhead = h2[16:0x90]
        if self.method > 5 or (self.rvz and self.method == 1):
            raise UnpackError(f"unsupported compression method {self.method}")
        npart, psize, poff = struct.unpack(">IIQ", h2[0x90:0xA0])
        nraw, roff, rsize, ngrp, goff, gsize = struct.unpack(">IQIIQI", h2[0xB4:0xD4])
        self.cdata = h2[0xD5:0xD5 + h2[0xD4]]
        self.f.seek(poff)
        pbytes = self.f.read(npart * psize)
        if hashlib.sha1(pbytes).digest() != h2[0xA0:0xB4]:
            raise UnpackError("the partition table is damaged")
        self.parts = []
        for i in range(npart):
            e = pbytes[i * psize:i * psize + 0x30].ljust(0x30, b"\0")
            pds = [struct.unpack(">4I", e[16 + 16 * j:32 + 16 * j]) for j in range(2)]
            self.parts.append((e[:16], pds, AesCbc(e[:16])))
        raw = self._decomp(self.method, self._pread(roff, rsize), nraw * 24)
        self.raws = [struct.unpack(">QQII", raw[i * 24:i * 24 + 24]) for i in range(nraw)]
        gs = 12 if self.rvz else 8
        graw = self._decomp(self.method, self._pread(goff, gsize), ngrp * gs)
        self.groups = [struct.unpack(">III" if self.rvz else ">II", graw[i * gs:(i + 1) * gs]) + ((0,) if not self.rvz else ())
                       for i in range(ngrp)]
        regions = []
        for i, (o, s, gi, ng) in enumerate(self.raws):
            if s:
                regions.append((o, o + s, "r", i))
        for i, (_, pds, _) in enumerate(self.parts):
            end = pds[1][0] + pds[1][1] if pds[1][1] else pds[0][0] + pds[0][1]
            if end > pds[0][0]:
                regions.append((pds[0][0] * CL, end * CL, "p", i))
        regions.sort()
        self.regions = regions
        self.starts = [r[0] for r in regions]
        self.cache = {}

    def _pread(self, pos, n):
        self.f.seek(pos)
        return self.f.read(n)

    def _decomp(self, method, data, size=0):
        if method == 0:
            return data
        if method == 1:
            return _purge_decode(data, size)
        if method == 2:
            return bz2.decompress(data)
        if method == 5:
            try:
                import zstandard
            except ImportError:
                raise UnpackError("this file uses zstd compression, which needs the 'zstandard' package") from None
            return zstandard.ZstdDecompressor().decompressobj().decompress(data)
        cd = self.cdata
        if method == 3:
            if len(cd) != 5 or cd[0] >= 225:
                raise UnpackError("unreadable LZMA settings")
            d = cd[0]
            filt = {"id": lzma.FILTER_LZMA1, "dict_size": struct.unpack("<I", cd[1:5])[0], "lc": d % 9, "lp": d // 9 % 5, "pb": d // 45}
        else:
            if len(cd) != 1 or cd[0] > 40:
                raise UnpackError("unreadable LZMA2 settings")
            p = cd[0]
            filt = {"id": lzma.FILTER_LZMA2, "dict_size": 0xFFFFFFFF if p == 40 else (2 | (p & 1)) << (p // 2 + 11)}
        return lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[filt]).decompress(data)

    def _group(self, gi, size, nlists, data_off):
        """One group's data (`size` bytes) and its lists of hash exceptions."""
        key = (gi, size)
        if key in self.cache:
            return self.cache[key]
        d4, ds, packed = self.groups[gi]
        method = self.method
        if self.rvz:
            if not ds & 0x80000000:
                method = 0
            ds &= 0x7FFFFFFF
        if ds == 0:
            res = (bytes(size), [[] for _ in range(nlists)])
        else:
            raw = self._pread(d4 * 4, ds)
            if method > 1:
                raw = self._decomp(method, raw)
            lists, pos = [], 0
            for _ in range(nlists):
                cnt = struct.unpack_from(">H", raw, pos)[0]
                lists.append([(struct.unpack_from(">H", raw, pos + 2 + 22 * k)[0], raw[pos + 4 + 22 * k:pos + 24 + 22 * k]) for k in range(cnt)])
                pos += 2 + 22 * cnt
            if method <= 1 and nlists:
                pos = (pos + 3) & ~3
            body = raw[pos:]
            if method == 1:
                body = _purge_decode(body, packed or size)
            if packed:
                body = rvz_unpack(body, data_off)
            if len(body) < size:
                raise UnpackError("a block of the file is shorter than it should be")
            res = (body[:size], lists)
        if len(self.cache) > 3:
            self.cache.pop(next(iter(self.cache)))
        self.cache[key] = res
        return res

    def _raw_part(self, ri, off, n):
        o, s, gi, ng = self.raws[ri]
        base = o - o % CL
        end = o + s
        g = (off - base) // self.chunk
        gstart = base + g * self.chunk
        gsize = min(self.chunk, end - gstart)
        body, _ = self._group(gi + g, gsize, 0, g * self.chunk)
        a = off - gstart
        return body[a:a + n]

    def _wii_group(self, pi, g):
        key, pds, aes = self.parts[pi]
        p0 = pds[0][0]
        total = (pds[1][0] + pds[1][1] - p0) if pds[1][1] else pds[0][1]
        lo_c, hi_c = g * 64, min(g * 64 + 64, total)
        data = bytearray(64 * CLD)
        exc = []
        cs = self.chunk // CL
        for first, n, gidx, ngr in pds:
            if not n:
                continue
            rel0 = first - p0
            lo, hi = max(lo_c, rel0), min(hi_c, rel0 + n)
            if lo >= hi:
                continue
            for ci in range((lo - rel0) // cs, (hi - 1 - rel0) // cs + 1):
                cstart = rel0 + ci * cs
                cn = min(cs, rel0 + n - cstart)
                nl = max(1, cs // 64)
                body, lists = self._group(gidx + ci, cn * CLD, nl, ci * cs * CLD)
                a, b = max(lo, cstart), min(hi, cstart + cn)
                data[(a - lo_c) * CLD:(b - lo_c) * CLD] = body[(a - cstart) * CLD:(b - cstart) * CLD]
                if cs >= 64:
                    lst, add = lists[(lo_c - cstart) // 64], 0
                else:
                    lst, add = lists[0], (cstart - lo_c) * 0x400
                exc += [(o + add, h) for o, h in lst]
        hb = wii_hash_group(data)
        for o, h in exc:
            if o // 0x400 < 64:
                hb[o // 0x400][o % 0x400:o % 0x400 + 20] = h
        out = []
        for b in range(hi_c - lo_c):
            eh = aes.crypt(hb[b])
            out.append(eh + aes.crypt(bytes(data[b * CLD:(b + 1) * CLD]), eh[0x3D0:0x3E0]))
        return b"".join(out)

    def read(self, off, n):
        out = []
        end = min(off + n, self.size)
        want = n
        while off < end:
            if off < 0x80:
                take = min(0x80 - off, end - off)
                out.append(self.dhead[off:off + take])
                off += take
                continue
            ri = bisect.bisect_right(self.starts, off) - 1
            if ri < 0 or off >= self.regions[ri][1]:
                nxt = self.starts[ri + 1] if ri + 1 < len(self.starts) else end
                take = min(nxt, end) - off
                out.append(bytes(take))
                off += take
                continue
            s, e, kind, idx = self.regions[ri]
            if kind == "r":
                piece = self._raw_part(idx, off, min(end, e) - off)
            else:
                g, inb = divmod(off - s, 64 * CL)
                ck = ("w", idx, g)
                if ck not in self.cache:
                    if len(self.cache) > 3:
                        self.cache.pop(next(iter(self.cache)))
                    self.cache[ck] = self._wii_group(idx, g)
                piece = self.cache[ck][inb:inb + min(end, e) - off]
            out.append(piece)
            off += len(piece)
        return b"".join(out).ljust(want, b"\0")

    def close(self):
        self.f.close()


def wii_hash_group(data):
    """The 64 hash blocks (H0, H1, H2) of one Wii group of 64 x 0x7C00 bytes of data."""
    sha1 = hashlib.sha1
    hb = [bytearray(0x400) for _ in range(64)]
    h0s = []
    for b in range(64):
        blk = data[b * CLD:(b + 1) * CLD]
        h0 = b"".join([sha1(blk[j:j + 0x400]).digest() for j in range(0, CLD, 0x400)])
        hb[b][0:620] = h0
        h0s.append(sha1(h0).digest())
    h2 = []
    for s in range(8):
        h1 = b"".join(h0s[s * 8:s * 8 + 8])
        for b in range(s * 8, s * 8 + 8):
            hb[b][0x280:0x320] = h1
        h2.append(sha1(h1).digest())
    h2 = b"".join(h2)
    for b in range(64):
        hb[b][0x340:0x3E0] = h2
    return hb


def open_disc(path):
    """Open any GameCube/Wii disc container by what is inside it."""
    with open(path, "rb") as f:
        h = f.read(8)
    if h[:4] == b"WBFS":
        return WbfsDisc(path)
    if h[:4] == b"WUX0":
        return WuxDisc(path)
    if h[:4] == b"CISO" and struct.unpack("<I", h[4:8])[0] != 24:
        return CisoDisc(path)
    if h[:4] == GCZ_MAGIC:
        return GczDisc(path)
    if h[:4] in (b"WIA\x01", b"RVZ\x01"):
        return RvzDisc(path)
    return IsoDisc(path)


def disc_kind(path):
    """'Wii' or 'GameCube' when the file is one of those discs in any of the supported containers, else ''."""
    try:
        d = open_disc(path)
    except (OSError, UnpackError, struct.error, ValueError):
        return ""
    try:
        return d.info()["platform"]
    except (UnpackError, OSError, struct.error, ValueError, zlib.error):
        return ""
    finally:
        d.close()


def disc_to_iso(disc, dst, progress, stopped, step=1 << 22):
    with open(dst, "wb") as g:
        off = 0
        while off < disc.size:
            n = min(step, disc.size - off)
            g.write(disc.read(off, n))
            off += n
            progress(off / disc.size)
            if stopped():
                raise UnpackError("stopped")


# ---- writing the containers
class KeyNeeded(UnpackError):
    """A Wii conversion that can't go on without the user's own Wii key."""


WII_KEYS = [None, None]                  # the Wii common key and the Korean one, only ever from the user's own files
WII_TOOL = [""]                          # the user's DolphinTool.exe, when there is one
WII_SCRUB = [True]
KEYS_DIR = HERE / "keys"
KEYS_README = """Put your own Wii keys in this folder. CHD Manager doesn't contain them and never downloads them.

A file here can be called anything ending in .txt, .key, .bin or .hex. It holds the key as 32 hexadecimal
characters (16 bytes), for example one line:

    common: <32 hex characters>
    korean: <32 hex characters>   (only for Korean discs)

A line with no label is taken as the common key. A file of exactly 16 raw bytes also works (a name containing
"korea" makes it the Korean key). You can instead paste the key into Settings, Formats.

The key is only needed to write RVZ, WIA or WBFS from a Wii disc. Reading every format, and everything for GameCube and
Wii U discs, works without it. If Dolphin is on this PC, its DolphinTool.exe can do the RVZ and WIA conversions instead.
"""


def parse_wii_keys(data, name=""):
    """[common key or None, Korean key or None] found in the contents of one key file."""
    out = [None, None]
    if len(data) == 16:
        out["korea" in name.lower()] = bytes(data)
        return out
    for line in data.decode("utf-8", "replace").splitlines():
        line = re.split(r"#|;|//", line)[0]
        m = re.search(r"(?<![0-9a-fA-F])(?:0x)?([0-9a-fA-F]{32})(?![0-9a-fA-F])", line) or \
            re.search(r"((?:[0-9a-fA-F]{2}[ :\-]){15}[0-9a-fA-F]{2})", line)
        if m:
            key = bytes.fromhex(re.sub(r"[^0-9a-fA-F]", "", m.group(1)))
            out["korea" in line.lower() or " new" in line.lower()] = key
    return out


def load_wii_keys(pasted_common="", pasted_korean=""):
    """Read the keys folder, then whatever was pasted into Settings; sets WII_KEYS."""
    keys = [None, None]
    try:
        for f in sorted(KEYS_DIR.iterdir()):
            if f.is_file() and f.suffix.lower() in (".txt", ".key", ".bin", ".hex") and f.name.lower() != "readme.txt" and f.stat().st_size < 4096:
                for i, k in enumerate(parse_wii_keys(f.read_bytes(), f.name)):
                    keys[i] = keys[i] or k
    except OSError:
        pass
    for i, text in enumerate((pasted_common, pasted_korean)):
        if not keys[i] and text:
            keys[i] = parse_wii_keys(text.encode("ascii", "ignore"))[0]
    WII_KEYS[:] = keys


def ensure_keys_dir():
    try:
        KEYS_DIR.mkdir(exist_ok=True)
        if not (KEYS_DIR / "README.txt").exists():
            (KEYS_DIR / "README.txt").write_text(KEYS_README, "utf-8")
    except OSError:
        pass


def find_dolphintool(configured=""):
    """The user's DolphinTool.exe: the path set in Settings, next to this program, on the PATH, or in a folder named Dolphin*
    directly inside the usual program folders. Nothing else is searched."""
    cands = [Path(configured)] if configured else []
    cands += [HERE / "DolphinTool.exe", HERE / "Dolphin" / "DolphinTool.exe", HERE / "Dolphin-x64" / "DolphinTool.exe"]
    w = shutil.which("DolphinTool.exe")
    if w:
        cands.append(Path(w))
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        root = os.environ.get(env)
        base = Path(root) / ("Programs" if env == "LOCALAPPDATA" else "") if root else None
        try:
            if base and base.is_dir():
                cands += [d / "DolphinTool.exe" for d in sorted(base.iterdir()) if d.name.lower().startswith("dolphin")]
        except OSError:
            pass
    for c in cands:
        try:
            if c.is_file():
                return str(c)
        except OSError:
            pass
    return ""


def wii_can(what):
    """Whether a conversion that needs the Wii key can be done on this PC: 'rvz' (RVZ and WIA) or 'wbfs'."""
    return bool(WII_KEYS[0] or WII_TOOL[0]) or (what == "wbfs" and not WII_SCRUB[0])


def wii_partitions(disc):
    """[(partition offset, data offset, data size, title key)] of the Wii partitions this code can handle."""
    out = []
    try:
        tbl = disc.read(0x40000, 0x20)
        for g in range(4):
            n, off = struct.unpack(">II", tbl[g * 8:g * 8 + 8])
            if not n or n > 16:
                continue
            ents = disc.read(off << 2, n * 8)
            for i in range(n):
                poff = struct.unpack(">I", ents[i * 8:i * 8 + 4])[0] << 2
                h = disc.read(poff, 0x2C0)
                if struct.unpack(">I", h[:4])[0] != 0x10001 or h[0x1F1] > 1:
                    continue
                doff, dsize = (x << 2 for x in struct.unpack(">II", h[0x2B8:0x2C0]))
                if (poff + doff) % CL or dsize < CL or poff + doff + dsize > disc.size + CL:
                    continue
                ckey = WII_KEYS[h[0x1F1]]
                if not ckey:
                    raise KeyNeeded("this Wii disc needs your Wii key: put it in the keys folder or paste it into Settings")
                key = AesCbc(ckey).crypt(h[0x1BF:0x1CF], h[0x1DC:0x1E4] + bytes(8), decrypt=True)
                first = disc.read(poff + doff, 0x420)
                plain = AesCbc(key).crypt(first[0x400:0x420], first[0x3D0:0x3E0], decrypt=True)
                if struct.unpack(">I", plain[0x18:0x1C])[0] != WII_MAGIC:
                    raise KeyNeeded("the Wii key doesn't fit this disc (is it the right key?)")
                out.append((poff, doff, min(dsize, disc.size - poff - doff), key))
    except KeyNeeded:
        raise
    except (UnpackError, struct.error, ValueError):
        return []
    out.sort()
    ok, last = [], 0
    for p in out:
        if p[0] >= last:
            ok.append(p)
            last = p[0] + p[1] + p[2]
    return ok


def wii_used_clusters(disc):
    """One byte per 0x8000 of a Wii disc: 1 when something on the disc lives there (headers, system files, game files).
    None when the disc can't be read that way."""
    parts = wii_partitions(disc)
    if not parts:
        return None
    used = bytearray((disc.size + CL - 1) // CL + 1)

    def mark(a, b):
        for s in range(a // CL, min((b - 1) // CL + 1, len(used))):
            used[s] = 1
    mark(0, 0x50000)
    try:
        for poff, doff, dsize, key in parts:
            mark(poff, poff + doff)
            aes = AesCbc(key)
            start, nclu = poff + doff, dsize // CL

            def pdata(off, n):
                out = []
                c = off // CLD
                while c * CLD < off + n and c < nclu:
                    raw = disc.read(start + c * CL, CL)
                    out.append(aes.crypt(raw[0x400:], raw[0x3D0:0x3E0], decrypt=True))
                    c += 1
                return b"".join(out)[off - off // CLD * CLD:][:n]

            def mark_data(a, b):
                if b > a:
                    mark(start + a // CLD * CL, start + ((b - 1) // CLD + 1) * CL)
            boot = pdata(0, 0x440)
            dol, fst, fsz = (x << 2 for x in struct.unpack(">III", boot[0x420:0x42C]))
            ap = pdata(0x2440, 0x20)
            ap_end = 0x2460 + struct.unpack(">II", ap[0x14:0x1C])[0] + struct.unpack(">II", ap[0x14:0x1C])[1] if len(ap) >= 0x1C else 0x2440
            dol_end = dol
            if dol:
                hd = pdata(dol, 0x100)
                if len(hd) == 0x100:
                    offs, sizes = struct.unpack(">18I", hd[:72]), struct.unpack(">18I", hd[0x90:0xD8])
                    dol_end = dol + max([o + s for o, s in zip(offs, sizes)] + [0x100])
            mark_data(0, max(fst + fsz, dol_end, ap_end))
            f = pdata(fst, fsz)
            n = struct.unpack(">I", f[8:12])[0] if len(f) >= 12 else 0
            for i in range(1, n):
                flags, off, ln = struct.unpack(">BxxxII", f[i * 12:i * 12 + 12])
                if not flags:
                    mark_data(off << 2, (off << 2) + ln)
    except KeyNeeded:
        raise
    except (UnpackError, struct.error, ValueError):
        return None
    return used


def write_wbfs(disc, dst, progress, stopped, scrub=True):
    """A Wii disc -> WBFS: the disc is cut into 2 MiB blocks and blocks nothing uses are left out (scrubbing)."""
    info = disc.info()
    if info["platform"] != "Wii":
        raise UnpackError("WBFS can only hold Wii discs")
    wb, hd = 1 << 21, 512
    per_disc = (143432 * 2 * CL + wb - 1) // wb
    used = wii_used_clusters(disc) if scrub else None
    nblocks = (disc.size + wb - 1) // wb
    keep = []
    for b in range(nblocks):
        if used is not None:
            if any(used[b * 64:b * 64 + 64]):
                keep.append(b)
        elif disc.read(b * wb, wb).strip(b"\0"):
            keep.append(b)
        if b % 64 == 0:
            progress(0.1 * b / nblocks)
            if stopped():
                raise UnpackError("stopped")
    if not keep:
        raise UnpackError("this disc has no data in it")
    total_secs = len(keep) + 1
    wlba = [0] * per_disc
    for i, b in enumerate(keep):
        wlba[b] = i + 1
    head = bytearray(hd)
    head[0:4] = b"WBFS"
    struct.pack_into(">IBB", head, 4, total_secs * wb // hd, 9, 21)
    head[12] = 1
    with open(dst, "wb") as g:
        g.write(bytes(head))
        g.write(disc.read(0, 256))
        g.write(struct.pack(">%dH" % per_disc, *wlba))
        g.write(bytes(wb - g.tell()))
        for i, b in enumerate(keep):
            g.write(disc.read(b * wb, wb).ljust(wb, b"\0"))
            if i % 8 == 0:
                progress(0.1 + 0.9 * i / len(keep))
                if stopped():
                    raise UnpackError("stopped")
    progress(1.0)


def write_ciso(disc, dst, progress, stopped, block=1 << 21):
    """A disc -> CISO (Dolphin's layout: a 0x8000-byte header with a map of the blocks that are present, then those blocks)."""
    n = (disc.size + block - 1) // block
    if n > 0x7FF8:
        raise UnpackError("this disc is too big for a CISO with 2 MiB blocks")
    present = bytearray(0x7FF8)
    with open(dst, "wb") as g:
        g.write(bytes(0x8000))
        for b in range(n):
            data = disc.read(b * block, block)
            if data.strip(b"\0") or b == n - 1:         # the last block is always kept, so the size survives
                present[b] = 1
                g.write(data)
            if b % 16 == 0:
                progress(b / n)
                if stopped():
                    raise UnpackError("stopped")
        g.seek(0)
        g.write(b"CISO" + struct.pack("<I", block) + bytes(present))
    progress(1.0)


def write_gcz(disc, dst, progress, stopped, block=None, level=9):
    """A disc -> GCZ (deflate-compressed blocks with a table of offsets and checksums)."""
    info = disc.info()
    block = block or (0x4000 if info["platform"] != "Wii" else 0x20000)
    n = (disc.size + block - 1) // block
    offsets, hashes = [], []
    with open(dst, "wb") as g:
        g.write(bytes(32 + 12 * n))
        pos = 0
        for i in range(n):
            data = disc.read(i * block, block).ljust(block, b"\0")
            c = zlib.compressobj(level)
            comp = c.compress(data) + c.flush()
            if len(comp) > block - 10:
                offsets.append(pos | 1 << 63)
                comp = data
            else:
                offsets.append(pos)
            hashes.append(zlib.adler32(comp))
            g.write(comp)
            pos += len(comp)
            if i % 256 == 0:
                progress(i / n)
                if stopped():
                    raise UnpackError("stopped")
        g.seek(0)
        g.write(GCZ_MAGIC + struct.pack("<IQQII", 1 if info["platform"] == "Wii" else 0, pos, disc.size, block, n))
        g.write(struct.pack("<%dQ" % n, *offsets) + struct.pack("<%dI" % n, *hashes))
    progress(1.0)


def rvz_pack(buf, bytes_per_chunk, nchunks, data_offset):
    """Find the pseudo-random junk in buf and write each chunk the way RVZ stores it: [(main data, packed size)]."""
    total = len(buf)
    junk = []                                              # (end, start, seed), in order
    nz = re.compile(rb"[^\x00]")
    pos, off = 0, data_offset
    while pos < total:
        m = nz.search(buf, pos)
        z = (m.start() if m else total) - pos
        pos += z
        off += z
        if pos >= total:
            break
        to_read = min(CL - off % CL, total - pos)
        dm = off % CL
        skip = -dm % 4
        seg = buf[pos + skip:pos + to_read]
        if len(seg) >= 2084:
            seed = lfg_find_seed(seg, (dm + skip) // 4)
            if seed:
                n = common_prefix(Lfg(seed, dm).get(to_read), buf[pos:pos + to_read])
                if n > 0:
                    junk.append((pos + n, pos, seed))
        pos += to_read
        off += to_read
    out = []
    ends = [j[0] for j in junk]
    for i in range(nchunks):
        cur = i * bytes_per_chunk
        end = min(cur + bytes_per_chunk, total)
        main, packed, first = bytearray(), 0, True
        while cur < end:
            js = je = end
            seed = None
            if end - cur > 68:
                k = bisect.bisect_right(ends, cur + 68)
                if k < len(junk) and junk[k][1] + 68 < end:
                    js, je, seed = max(cur, junk[k][1]), min(end, junk[k][0]), junk[k][2]
            if first:
                if js == end:
                    main += buf[cur:end]
                    break
                first = False
            if js > cur:
                main += struct.pack(">I", js - cur) + buf[cur:js]
                packed += 4 + js - cur
                cur = js
            if je > cur:
                main += struct.pack(">I", (je - cur) | 0x80000000) + seed
                packed += 4 + 68
                cur = je
        out.append((bytes(main), packed))
    return out


class _Compressor:
    def __init__(self, method, level, chunk):
        self.method = method
        self.level = level = max(1, min(9, level)) if method in (2, 3, 4) else level
        dict_size = max(1 << 16, 1 << (max(chunk, 1) - 1).bit_length())
        self.cdata = b""
        if method == 3:
            self.filt = {"id": lzma.FILTER_LZMA1, "preset": level, "dict_size": dict_size, "lc": 3, "lp": 0, "pb": 2}
            self.cdata = bytes([93]) + struct.pack("<I", dict_size)
        elif method == 4:
            p = 0
            while ((2 | (p & 1)) << (p // 2 + 11)) < dict_size:
                p += 1
            self.filt = {"id": lzma.FILTER_LZMA2, "preset": level, "dict_size": (2 | (p & 1)) << (p // 2 + 11)}
            self.cdata = bytes([p])
        elif method == 5:
            try:
                import zstandard
            except ImportError:
                raise UnpackError("zstd compression needs the 'zstandard' package") from None
            self.zc = zstandard.ZstdCompressor(level=level)

    def __call__(self, data):
        if self.method == 0:
            return data
        if self.method == 2:
            return bz2.compress(data, self.level)
        if self.method == 5:
            return self.zc.compress(data)
        return lzma.compress(data, lzma.FORMAT_RAW, filters=[self.filt])


def write_rvz(disc, dst, progress, stopped, rvz=True, method=5, level=5, chunk=0x20000):
    """A GameCube/Wii disc -> RVZ (or WIA). Wii partitions are stored decrypted and without their hashes; the pseudo-random
    padding is stored as a seed (RVZ only); every chunk is compressed on its own."""
    info = disc.info()
    if info["platform"] not in ("Wii", "GameCube"):
        raise UnpackError("this isn't a GameCube or Wii disc")
    if rvz:
        if chunk < CL or chunk > 0x200000 or chunk & (chunk - 1):
            raise UnpackError("RVZ chunk size must be a power of two from 32 KiB to 2 MiB")
    else:
        chunk = 0x200000
        method = 4 if method not in (0, 2, 3, 4) else method
    if method > (5 if rvz else 4) or (rvz and method == 1):
        raise UnpackError("unsupported compression method")
    comp = _Compressor(method, level, chunk)
    size = disc.size
    parts = wii_partitions(disc) if info["platform"] == "Wii" else []
    # ---- layout: raw regions and partition regions in disc order, each with its range of groups
    raws, pents, todo, groups = [], [], [], 0

    def add_raw(offset, n):
        nonlocal groups
        skip = min(0x80 - offset, n) if offset < 0x80 else 0
        offset, n = offset + skip, n - skip
        if n <= 0:
            return
        ng = -(-(n + offset % CL) // chunk)
        raws.append((offset, n, groups, ng))
        todo.append(("r", len(raws) - 1))
        groups += ng
    last = 0
    for poff, doff, dsize, key in parts:
        add_raw(last, poff - last)
        add_raw(poff, doff)
        fl = dsize // CL * CL
        ng = -(-fl // chunk)
        first = (poff + doff) // CL
        pents.append((key, [(first, fl // CL, groups, ng), (first + fl // CL, 0, groups + ng, 0)]))
        todo.append(("p", len(pents) - 1))
        groups += ng
        last = poff + doff + fl
    add_raw(last, size - last)
    entries = [None] * groups                                # (offset/4, size field, packed size)
    cs = chunk // CL

    def finish(gi, exc, main, packed, zero):
        nonlocal pos
        if zero and not any(exc):
            entries[gi] = (0, 0, 0)                           # all zeroes, no exceptions: a size of 0 says so
            return
        lists = b"".join(struct.pack(">H", len(x)) + b"".join(struct.pack(">H", o) + h for o, h in x) for x in exc)
        flag, payload = 0, None
        if method:
            cdat = comp(lists + main)
            if not rvz:
                payload = cdat
            elif len(cdat) < len(main) + len(lists) + -len(lists) % 4:
                payload, flag = cdat, 0x80000000
        if payload is None:                                   # not compressed: lists padded to 4 bytes, then the data
            payload = lists + bytes(-len(lists) % 4) + main
        entries[gi] = (pos // 4, len(payload) | flag, packed)
        g.write(payload)
        pos += len(payload)
        pad = -pos % 4
        if pad:
            g.write(bytes(pad))
            pos += pad
    with open(dst, "wb") as g:
        g.write(bytes(0x48 + 0xDC))
        pos = 0x48 + 0xDC
        done = 0
        for kind, idx in todo:
            if kind == "r":
                off, n, gi, ng = raws[idx]
                base = off - off % CL
                end = off + n
                for k in range(ng):
                    a = base + k * chunk
                    data = disc.read(a, min(chunk, end - a))
                    if rvz:
                        main, packed = rvz_pack(data, len(data), 1, a)[0]
                    else:
                        main, packed = data, 0
                    finish(gi + k, [], main, packed, not data.strip(b"\0"))
                    done += len(data)
                    progress(done / size)
                    if stopped():
                        raise UnpackError("stopped")
            else:
                key, pds = pents[idx]
                first, nclu, gi, ng = pds[0]
                aes = AesCbc(key)
                start = first * CL
                ngroups64 = -(-nclu // 64)
                for g64 in range(ngroups64):
                    nb = min(64, nclu - g64 * 64)
                    raw = disc.read(start + g64 * 64 * CL, nb * CL)
                    data = bytearray(64 * CLD)
                    actual = []
                    for b in range(nb):
                        c = raw[b * CL:(b + 1) * CL]
                        actual.append(aes.crypt(c[:0x400], bytes(16), decrypt=True))
                        data[b * CLD:(b + 1) * CLD] = aes.crypt(c[0x400:], c[0x3D0:0x3E0], decrypt=True)
                    hb = wii_hash_group(data)
                    bpc = min(64, cs)
                    nch = -(-nb // bpc)
                    exc = [[] for _ in range(nch)]
                    for b in range(nb):
                        ch, bi = divmod(b, bpc)
                        want, got = actual[b], bytes(hb[b])
                        if want == got:
                            continue
                        for fo, fs in ((0, 620), (620, 20), (0x280, 160), (0x320, 32), (0x340, 160), (0x3E0, 32)):
                            for l in range(0, fs, 20):
                                o = fo + min(l, fs - 20)
                                if want[o:o + 20] != got[o:o + 20]:
                                    exc[ch].append((bi * 0x400 + o, want[o:o + 20]))
                    body = bytes(data[:nb * CLD])
                    if rvz:
                        packs = rvz_pack(body, bpc * CLD, nch, g64 * 64 * CLD)
                    else:
                        packs = [(body[c * bpc * CLD:(c + 1) * bpc * CLD], 0) for c in range(nch)]
                    for c in range(nch):
                        main, packed = packs[c]
                        zero = not body[c * bpc * CLD:(c + 1) * bpc * CLD].strip(b"\0")
                        finish(gi + g64 * (64 // bpc) + c, [exc[c]], main, packed, zero)
                    done += nb * CL
                    progress(done / size)
                    if stopped():
                        raise UnpackError("stopped")
        # ---- the tables and headers
        praw = b"".join(struct.pack(">16s", k) + b"".join(struct.pack(">4I", *p) for p in pd) for k, pd in pents)
        rawt = b"".join(struct.pack(">QQII", *r) for r in raws)
        grpt = b"".join(struct.pack(">III" if rvz else ">II", *(e if rvz else e[:2])) for e in entries)
        crawt, cgrpt = comp(rawt), comp(grpt)
        poff = pos
        g.write(praw)
        roff = poff + len(praw)
        g.write(crawt)
        goff = roff + len(crawt)
        g.write(cgrpt)
        fsize = goff + len(cgrpt)
        h2 = struct.pack(">IIiI", 2 if info["platform"] == "Wii" else 1, method, level, chunk) + disc.read(0, 0x80)
        h2 += struct.pack(">IIQ", len(pents), 0x30, poff) + hashlib.sha1(praw).digest()
        h2 += struct.pack(">IQIIQI", len(raws), roff, len(crawt), len(entries), goff, len(cgrpt))
        h2 += bytes([len(comp.cdata)]) + comp.cdata.ljust(7, b"\0")
        h1 = (b"RVZ\x01" if rvz else b"WIA\x01") + struct.pack(">II", 0x01000000, 0x00030000 if rvz else 0x01000000)
        h1 += struct.pack(">I", len(h2)) + hashlib.sha1(h2).digest() + struct.pack(">QQ", size, fsize)
        h1 += hashlib.sha1(h1).digest()
        g.seek(0)
        g.write(h1 + h2)
    progress(1.0)


def ecm_probe(path):
    """The track type in the first sector of an ECM file ('MODE1/2352', 'MODE2/2352', 'MODE2/2336' or '')."""
    with open(path, "rb") as f:
        if f.read(4) != b"ECM\x00":
            raise UnpackError("this is not an ECM file")
        c = f.read(1)
        if not c:
            return ""
        c = c[0]
        typ, num, bits = c & 3, (c >> 2) & 0x1F, 5
        while c & 0x80:
            c = f.read(1)[0]
            num |= (c & 0x7F) << bits
            bits += 7
        if typ == 1:
            return "MODE1/2352"
        if typ in (2, 3):
            return "MODE2/2336"
        lit = f.read(min(num + 1, 16))
        if len(lit) >= 16 and lit[:12] == _SYNC and lit[15] in (1, 2):
            return f"MODE{lit[15]}/2352"
    return ""


def verify_disc_file(path, src, kind, progress, stopped):
    """True when the container at `path` holds the disc `src`. A WBFS is checked on the blocks it stores and a CISO up to
    the size of the disc, since those formats leave out (or pad) data that nothing uses."""
    d = open_disc(path)
    try:
        if kind == "wbfs":
            for i, v in enumerate(d.wlba):
                if v and d.read(i * d.wb, d.wb) != src.read(i * d.wb, d.wb):
                    return False
                if i % 16 == 0:
                    progress(i / len(d.wlba))
                    if stopped():
                        raise UnpackError("stopped")
            return True
        if kind == "ciso":
            off = 0
            while off < src.size:
                n = min(1 << 22, src.size - off)
                if d.read(off, n) != src.read(off, n):
                    return False
                off += n
                progress(off / src.size)
                if stopped():
                    raise UnpackError("stopped")
            return True
        return discs_equal(d, src, progress, stopped)
    finally:
        d.close()


def discs_equal(a, b, progress=lambda x: None, stopped=lambda: False, step=1 << 22):
    """True when two disc images hold exactly the same bytes."""
    if a.size != b.size:
        return False
    off = 0
    while off < a.size:
        n = min(step, a.size - off)
        if a.read(off, n) != b.read(off, n):
            return False
        off += n
        progress(off / a.size)
        if stopped():
            raise UnpackError("stopped")
    return True


# ------------------------------------------------------ CHD -> PBP (PlayStation 1), written from the layout notes above
PBP_BLOCK = 16 * 2352


def cue_layout(cue):
    """Read the .cue chdman wrote (one BIN) -> dict(bin, tracks=[(is_data, lba)], inserts=[(file sector, count)], total).
    A PREGAP (silence that isn't stored in the BIN) is put back into the stream so every track lands where it
    does on the real disc; the first track's 2-second lead-in is the standard offset and never part of the stream."""
    cue = Path(cue)
    binf, tracks, cur = None, [], None
    for line in cue.read_text("utf-8", "replace").splitlines():
        t = line.strip()
        m = re.match(r'FILE\s+(?:"([^"]+)"|(\S+))', t, re.I)
        if m:
            if binf is not None:
                raise UnpackError("this disc is stored in several files")
            binf = cue.parent / (m.group(1) or m.group(2))
            continue
        m = re.match(r"TRACK\s+\d+\s+(\S+)", t, re.I)
        if m:
            cur = dict(data=m.group(1).upper() != "AUDIO", pregap=0, idx=None)
            tracks.append(cur)
            continue
        m = re.match(r"(PREGAP|INDEX\s+01)\s+(\d+):(\d+):(\d+)", t, re.I)
        if m and cur is not None:
            frames = (int(m.group(2)) * 60 + int(m.group(3))) * 75 + int(m.group(4))
            cur["pregap" if m.group(1).upper() == "PREGAP" else "idx"] = frames
    if binf is None or not tracks or any(t["idx"] is None for t in tracks):
        raise UnpackError("can't read the track list of this disc")
    inserts, extra, out = [], 0, []
    for n, t in enumerate(tracks):
        if n and t["pregap"]:
            inserts.append((t["idx"], t["pregap"]))
            extra += t["pregap"]
        out.append((t["data"], t["idx"] + extra))
    sectors = binf.stat().st_size // 2352
    return dict(bin=binf, tracks=out, inserts=inserts, total=sectors + extra)


def iter_disc_blocks(lay):
    """The disc as consecutive 37632-byte blocks (16 sectors each); the last block is padded with zeros."""
    def pieces():
        done = 0
        with open(lay["bin"], "rb") as f:
            for at, count in lay["inserts"] + [(lay["total"], 0)]:
                left = (at - done) * 2352
                while left > 0:
                    chunk = f.read(min(left, 1 << 20))
                    if not chunk:
                        return
                    left -= len(chunk)
                    yield chunk
                done = at
                for _ in range(count):
                    yield bytes(2352)
    buf = bytearray()
    for piece in pieces():
        buf += piece
        while len(buf) >= PBP_BLOCK:
            yield bytes(buf[:PBP_BLOCK])
            del buf[:PBP_BLOCK]
    if buf:
        yield bytes(buf) + bytes(PBP_BLOCK - len(buf))


def pbp_toc_bytes(tracks, total):
    """The disc's table of contents the way PBP files store it: 10-byte entries (A0 first track, A1 last track,
    A2 lead-out, then one per track)."""
    def msf(frames):
        return bytes((_bcd(frames // 4500), _bcd(frames // 75 % 60), _bcd(frames % 75)))
    ctrl = lambda is_data: 0x41 if is_data else 0x01
    out = [bytes((ctrl(tracks[0][0]), 0, 0xA0, 0, 0, 0, 0, _bcd(1), 0x20, 0)),
           bytes((ctrl(tracks[-1][0]), 0, 0xA1, 0, 0, 0, 0, _bcd(len(tracks)), 0, 0)),
           bytes((ctrl(tracks[-1][0]), 0, 0xA2, 0, 0, 0, 0)) + msf(total + 150)]
    for n, (is_data, lba) in enumerate(tracks, 1):
        out.append(bytes((ctrl(is_data), 0, _bcd(n))) + (msf(lba) if n > 1 else bytes((0, 2, 1))) + bytes((0,)) + msf(lba + 150))
    return b"".join(out)


def build_sfo(entries):
    """A PARAM.SFO. entries: [(key, value, max_len)] sorted by key; value is an int or a string."""
    keys, data, index = b"", b"", b""
    for key, val, maxlen in entries:
        raw = key.encode() + b"\0"
        if isinstance(val, int):
            fmt, ln, body, maxlen = 0x0404, 4, struct.pack("<I", val), 4
        else:
            body = val.encode("utf-8", "replace")[:maxlen - 1] + b"\0"
            fmt, ln = 0x0204, len(body)
            body += bytes(maxlen - len(body))
        index += struct.pack("<HHIII", len(keys), fmt, ln, maxlen, len(data))
        keys += raw
        data += body
    keys += bytes((-len(keys)) % 4)
    key_off = 20 + len(index)
    return b"\0PSF" + struct.pack("<IIII", 0x101, key_off, key_off + len(keys), len(entries)) + index + keys + data


def ps1_boot_id(path, stride_off, stride=2352, ps2=False):
    """The game code ('SLUS01545') from the BOOT line of SYSTEM.CNF, read straight from the extracted BIN; '' if
    this isn't a PlayStation 1 disc."""
    try:
        r = DiscReader(path, stride, stride_off)
        pvd = r.sector(16)
        if pvd[1:6] != b"CD001":
            return ""
        rec = pvd[156:190]
        extent = struct.unpack("<I", rec[2:6])[0]
        size = struct.unpack("<I", rec[10:14])[0]
        data = b"".join(r.sector(extent + i) for i in range(min(8, (size + 2047) // 2048)))
        pos = 0
        while pos < len(data):
            ln = data[pos]
            if ln == 0:
                pos = (pos // 2048 + 1) * 2048
                continue
            name = data[pos + 33:pos + 33 + data[pos + 32]].decode("latin1").upper()
            if name.startswith("SYSTEM.CNF"):
                ext = struct.unpack("<I", data[pos + 2:pos + 6])[0]
                txt = r.sector(ext).decode("latin1", "replace")
                pat = r"BOOT2\s*=\s*cdrom0:\\?(?:.*[\\/])?([A-Z]{4})[_-]?(\d{3})\.?(\d{2})" if ps2 else \
                    r"BOOT\s*=\s*cdrom:\\?(?:.*[\\/])?([A-Z]{4})[_-]?(\d{3})\.?(\d{2})"
                m = re.search(pat, txt, re.I)
                return (m.group(1) + m.group(2) + m.group(3)).upper() if m else ""
            pos += ln
    except (OSError, struct.error, IndexError):
        pass
    return ""


def _disc_head(lay, game_id, title, multi):
    """Everything in a disc section before its block index (0x4000 bytes)."""
    head = bytearray(0x4000)
    head[0:12] = b"PSISOIMG0000"
    code = "_" + game_id[:4] + "_" + game_id[4:9]
    head[0x400:0x400 + len(code)] = code.encode("ascii", "replace")
    head[0x400 + 2046] = 0x10
    toc = pbp_toc_bytes(lay["tracks"], lay["total"])
    head[0x800:0x800 + len(toc)] = toc
    d2 = 0x1224
    head[d2 + 4:d2 + 6] = b"\xff\x07"
    t = title.encode("ascii", "replace")[:120]
    head[d2 + 8:d2 + 8 + len(t)] = t
    head[d2 + 136] = 3
    return head


ART_FILES = (("icon0", "ICON0.PNG", (144, 80)), ("icon1", "ICON1.PMF", None), ("pic0", "PIC0.PNG", (310, 180)),
             ("pic1", "PIC1.PNG", (480, 272)), ("snd0", "SND0.AT3", None))


def read_art_folder(folder):
    """Your own PBP artwork: ICON0.PNG, ICON1.PMF, PIC0.PNG, PIC1.PNG and SND0.AT3 in a folder (any may be missing).
    -> (dict of what was found, [warnings]); the dict is empty when the folder has none of them."""
    art, warn = {}, []
    folder = Path(folder)
    if not folder.is_dir():
        return art, warn
    have = {p.name.upper(): p for p in folder.iterdir() if p.is_file()}
    for key, name, size in ART_FILES:
        p = have.get(name)
        if not p:
            continue
        data = p.read_bytes()
        if size:
            if data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) < 24:
                warn.append(f"{name} isn't a PNG and was ignored")
                continue
            w, h = struct.unpack(">II", data[16:24])
            if (w, h) != size:
                warn.append(f"{name} is {w}x{h}; a PSP expects {size[0]}x{size[1]}")
        art[key] = data
    return art, warn


def extract_pbp_art(pbp, outdir):
    """Write the icon, pictures and sound stored in a PBP into outdir. -> [file names written]"""
    with open(pbp, "rb") as f:
        head = f.read(0x28)
        if len(head) < 0x28 or head[:4] != b"\x00PBP":
            raise UnpackError("this isn't a PBP file")
        o = struct.unpack("<10I", head)
        Path(outdir).mkdir(parents=True, exist_ok=True)
        made = []
        for i, (_, name, _) in enumerate(ART_FILES):
            f.seek(o[3 + i])
            data = f.read(o[4 + i] - o[3 + i])
            if data:
                (Path(outdir) / name).write_bytes(data)
                made.append(name)
    return made


# A small 5x7 bitmap font (ASCII 32..126), one byte per column, bit 0 = top row. Drawn here, so the title cards need
# no font file and nothing copyrighted.
_FONT5X7 = bytes.fromhex(
    "0000000000" "00005f0000" "0007000700" "147f147f14" "242a7f2a12" "2313086462" "3649552250" "0005030000"
    "001c224100" "0041221c00" "14083e0814" "08083e0808" "0050300000" "0808080808" "0060600000" "2010080402"
    "3e5149453e" "00427f4000" "4261514946" "2141454b31" "1814127f10" "2745454539" "3c4a494930" "0171090503"
    "3649494936" "064949291e" "0036360000" "0056360000" "0814224100" "1414141414" "0041221408" "0201510906"
    "324979413e" "7e1111117e" "7f49494936" "3e41414122" "7f4141221c" "7f49494941" "7f09090901" "3e4149497a"
    "7f0808087f" "00417f4100" "2040413f01" "7f08142241" "7f40404040" "7f020c027f" "7f0408107f" "3e4141413e"
    "7f09090906" "3e4151215e" "7f09192946" "4649494931" "01017f0101" "3f4040403f" "1f2040201f" "3f4038403f"
    "6314081463" "0708700807" "6151494543" "007f414100" "0204081020" "00417f4100" "0402010204" "4040404040"
    "0001020400" "2054545478" "7f48444438" "3844444420" "384444487f" "3854545418" "087e090102" "0c5252523e"
    "7f08040478" "00447d4000" "2040443d00" "7f10284400" "00417f4000" "7c04180478" "7c08040478"
    "3844444438" "7c14141408" "081414187c" "7c08040408" "4854545420" "043f444020" "3c4040207c" "1c2040201c"
    "3c4030403c" "4428102844" "0c5050503c" "4464544c44" "0008364100" "00007f0000" "0041360800" "1008081008")

_CARD_COLORS = ((200, 60, 60), (200, 120, 40), (180, 150, 30), (70, 160, 70), (40, 150, 150), (60, 110, 200),
                (120, 80, 200), (180, 70, 160))


def png_rgb(width, height, rows):
    """A minimal PNG writer: rows is a list of `height` byte strings of `width` RGB pixels."""
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _wrap_text(text, per_line):
    lines, cur = [], ""
    for word in text.split():
        while len(word) > per_line:                # a word longer than a line is broken
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(word[:per_line])
            word = word[per_line:]
        if not cur:
            cur = word
        elif len(cur) + 1 + len(word) <= per_line:
            cur += " " + word
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def title_card(width, height, title, code, scales):
    """A plain 'title card' PNG: a gradient in a colour taken from the game code, the title in white and the code below.
    scales = the text sizes to try for the title, biggest first (the first one that fits is used)."""
    seed = zlib.crc32((code or title).encode("ascii", "replace"))
    r, g, b = _CARD_COLORS[seed % len(_CARD_COLORS)]
    top, bot = (r // 3, g // 3, b // 3), (r // 8, g // 8, b // 8)
    rows = []
    for y in range(height):
        t = y / max(1, height - 1)
        px = bytes(int(top[i] + (bot[i] - top[i]) * t) for i in range(3))
        rows.append(bytearray(px * width))
    margin = max(4, width // 36)

    def blit(x0, y0, ch, scale, color):
        code_ = ord(ch) - 32 if 32 <= ord(ch) <= 126 else ord("?") - 32
        cols = _FONT5X7[code_ * 5:code_ * 5 + 5]
        for cx, col in enumerate(cols):
            for ry in range(7):
                if col >> ry & 1:
                    for dy in range(scale):
                        y = y0 + ry * scale + dy
                        if 0 <= y < height:
                            for dx in range(scale):
                                x = x0 + cx * scale + dx
                                if 0 <= x < width:
                                    rows[y][x * 3:x * 3 + 3] = bytes(color)

    def draw(lines, scale, top_y, color):
        step = 8 * scale + scale
        for i, line in enumerate(lines):
            w = len(line) * 6 * scale - scale
            x = (width - w) // 2
            for k, ch in enumerate(line):
                blit(x + k * 6 * scale, top_y + i * step, ch, scale, color)

    text = re.sub(r"\s+", " ", title.encode("ascii", "replace").decode("ascii")).strip().upper() or "?"
    code = (code or "").strip().upper()
    code_scale = max(1, scales[-1])
    code_h = (8 * code_scale) + margin if code else 0
    avail_h, avail_w = height - 2 * margin - code_h, width - 2 * margin
    chosen = None
    for sc in scales:
        lines = _wrap_text(text, max(1, (avail_w + sc) // (6 * sc)))
        if len(lines) * (9 * sc) - sc <= avail_h and all(len(l) * 6 * sc - sc <= avail_w for l in lines):
            chosen = (sc, lines)
            break
    if not chosen:                                   # too long even at the smallest size: cut it
        sc = scales[-1]
        per = max(1, (avail_w + sc) // (6 * sc))
        fit = max(1, (avail_h + sc) // (9 * sc))
        lines = _wrap_text(text, per)[:fit]
        lines[-1] = lines[-1][:max(0, per - 3)].rstrip() + "..."
        chosen = (sc, lines)
    sc, lines = chosen
    block = len(lines) * 9 * sc - sc
    y0 = margin + max(0, (avail_h - block) // 2)
    draw(lines, sc, y0, (255, 255, 255))
    if code:
        bar_y = height - margin - code_h + margin // 2
        for y in range(bar_y - 2, bar_y - 2 + max(1, code_scale)):          # a coloured line above the code
            if 0 <= y < height:
                rows[y][margin * 3:(width - margin) * 3] = bytes((r, g, b)) * (width - 2 * margin)
        draw([code], code_scale, bar_y + 3, (r + (255 - r) // 2, g + (255 - g) // 2, b + (255 - b) // 2))
    return png_rgb(width, height, rows)


def make_title_art(title, game_id, have=()):
    """ICON0 (144x80) and PIC1 (480x272) title cards for a PBP, for the pictures not in `have`.
    game_id like SLUS00594 is shown as SLUS-00594."""
    code = f"{game_id[:4]}-{game_id[4:]}" if re.fullmatch(r"[A-Z]{4}\d{5}", game_id or "") else (game_id or "")
    art = {}
    if "icon0" not in have:
        art["icon0"] = title_card(144, 80, title, code, (2, 1))
    if "pic1" not in have:
        art["pic1"] = title_card(480, 272, title, code, (6, 5, 4, 3, 2))
    return art


def read_base_pbp(path):
    """The parts of a working PS1 .pbp that a PSP needs and that can't be made from a disc: the loader (DATA.PSP),
    the icon and pictures, and the boot-logo block (STARTDAT) that follows the disc data."""
    with open(path, "rb") as f:
        head = f.read(0x28)
        if len(head) < 0x28 or head[:4] != b"\x00PBP":
            raise UnpackError("the base file isn't a PBP")
        o = struct.unpack("<10I", head)
        f.seek(0, 2)
        size = f.tell()
        if not all(o[i] <= o[i + 1] <= size for i in range(2, 9)) or o[9] + 16 > size:
            raise UnpackError("the base PBP's header is damaged")

        def part(i):
            f.seek(o[i])
            return f.read(o[i + 1] - o[i])
        psar = o[9]
        f.seek(psar)
        if not f.read(16).startswith((b"PSISOIMG0000", b"PSTITLEIMG000000")):
            raise UnpackError("the base PBP isn't a PlayStation 1 game")
        f.seek(psar + 0xC)
        start = psar + struct.unpack("<I", f.read(4))[0]
        f.seek(start)
        if f.read(8) != b"STARTDAT":
            raise UnpackError("the base PBP has no boot-logo block (STARTDAT)")
        f.seek(start)
        startdat = f.read()
        loader = part(8)
        if loader[:4] == b"~PSP":
            # Sony's encrypted loader only runs signed content: a PSP shuts down on an unsigned PBP. A plain (ELF)
            # DATA.PSP next to the base file takes its place, as it does for the popstation family of tools.
            side = Path(path).with_name("DATA.PSP")
            plain = side.read_bytes() if side.is_file() else b""
            if plain[:4] != b"\x7fELF":
                raise UnpackError("this base PBP carries an encrypted loader that a PSP won't run unsigned; "
                                  "use a PBP with a plain loader, or put a plain DATA.PSP next to it")
            loader = plain
        return dict(icon0=part(3), icon1=part(4), pic0=part(5), pic1=part(6), snd0=part(7), loader=loader, startdat=startdat)


# Two fixed byte runs in the header of a multi-disc PBP (offsets 0x214 and 0x28C). They are identical in every real
# multi-disc PBP examined (two different games) and a PSP shuts down if they are missing.
PBP_MULTI_BLOCK_A = bytes.fromhex("2900000023480000be18000084670000e14a00006c3d0000d62c0000ae72000052690000905f000049160000f16d0000")
PBP_MULTI_BLOCK_B = bytes.fromhex(
    "8c0eae9d398cfe246521ad2d65a961dfd44a1303b4cd325fbdb0f4f98a70be1e392c7dc0c1c66b81aa3c0653941bcee54416cfdbb1e3897ba2d2e7d6c1266b588d2ce6c31597d029c91681b6cc42ee0c2810eaf26b6f903005be4a2f4abcdce587ce19b980deb832ddad8967e0927889e8dc451d0cbe8b994d50b3d658966175")


def write_pbp(out_path, layouts, game_ids, title, progress, stopped, donor=None, level=6, art=None):
    """Write layouts (one per disc, from cue_layout) as a PBP. One disc -> PSISOIMG0000, several -> PSTITLEIMG000000.
    With donor (from read_base_pbp) the PBP also carries the loader, icon and boot logo a real PSP needs."""
    n = len(layouts)
    sfo = build_sfo([("BOOTABLE", 1, 4), ("CATEGORY", "ME", 4), ("DISC_ID", game_ids[0], 16), ("DISC_VERSION", "1.00", 8),
                     ("LICENSE", "Copyright(C) Sony Computer Entertainment America Inc.", 512), ("PARENTAL_LEVEL", 3, 4),
                     ("PSP_SYSTEM_VER", "3.01", 8), ("REGION", 0x8000, 4), ("TITLE", title, 128)])
    res = [donor[k] for k in ("icon0", "icon1", "pic0", "pic1", "snd0", "loader")] if donor else [b""] * 6
    for i, k in enumerate(("icon0", "icon1", "pic0", "pic1", "snd0")):      # your own artwork replaces the base PBP's
        if art and art.get(k):
            res[i] = art[k]
    offs, pos = [0x28], 0x28 + len(sfo)
    for r in res:
        offs.append(pos)
        pos += len(r)
    psar = max(0x10000, -(-pos // 0x10000) * 0x10000)
    header = struct.pack("<4sI8I", b"\0PBP", 0x10000, *offs, psar)
    done, all_blocks = 0, sum(-(-l["total"] // 16) for l in layouts) or 1
    with open(out_path, "wb") as g:
        g.write(header + sfo + b"".join(res))
        g.write(bytes(psar - g.tell()))
        starts = []
        if n > 1:
            mh = bytearray(0x3FC)
            mh[0:16] = b"PSTITLEIMG000000"
            struct.pack_into("<4I", mh, 0x18, 0x2CC9C5BC, 0x33B5A90F, 0x06F6B4B3, 0xB25945BA)
            mh[0x214:0x214 + len(PBP_MULTI_BLOCK_A)] = PBP_MULTI_BLOCK_A
            code = ("_" + game_ids[0][:4] + "_" + game_ids[0][4:9]).encode("ascii", "replace")
            mh[0x264:0x264 + len(code)] = code
            mh[0x28C:0x28C + len(PBP_MULTI_BLOCK_B)] = PBP_MULTI_BLOCK_B
            t = title.encode("ascii", "replace")[:120]
            mh[0x30C:0x30C + len(t)] = t
            struct.pack_into("<I", mh, 0x38C, 7)
            g.write(mh)
        for d, lay in enumerate(layouts):
            if n > 1:
                g.write(bytes((-g.tell()) % 0x8000))
            base = g.tell()
            starts.append(base - psar)
            blocks = -(-lay["total"] // 16)
            head = _disc_head(lay, game_ids[d], f"{title} [Disc{d + 1}of{n}]" if n > 1 else title, n > 1)
            struct.pack_into("<I", head, 0xC, blocks * PBP_BLOCK + 0x100000)
            g.write(head)
            index = bytearray(blocks * 32)
            g.write(index)
            g.write(bytes(base + 0x100000 - g.tell()))
            off = 0
            for b, block in enumerate(iter_disc_blocks(lay)):
                z = block
                if level > 0:
                    comp = zlib.compressobj(level, zlib.DEFLATED, -15)
                    z = comp.compress(block) + comp.flush()
                    if len(z) >= PBP_BLOCK:
                        z = block
                struct.pack_into("<IH", index, b * 32, off, len(z))
                g.write(z)
                off += len(z)
                done += 1
                if b % 32 == 0:
                    progress(done / all_blocks)
                    if stopped():
                        raise UnpackError("stopped")
            g.write(bytes((-g.tell()) % 0x10))
            end = g.tell()
            g.seek(base + 0x4000)
            g.write(index)
            if n == 1:
                g.seek(base + 0xC)
                g.write(struct.pack("<I", end - psar))
                g.seek(base + 0x1220)
                g.write(struct.pack("<I", end - psar + 0x2D31))
            g.seek(end)
        if donor:
            g.write(donor["startdat"])                   # the boot-logo block sits right after the disc data
        if n > 1:
            end = g.tell() - (len(donor["startdat"]) if donor else 0)
            g.seek(psar + 0x10)
            g.write(struct.pack("<I", end - psar))
            g.seek(psar + 0x284)
            g.write(struct.pack("<I", end - psar + 0x2D31))
            g.seek(psar + 0x200)
            g.write(struct.pack("<5I", *(starts + [0] * (5 - n))))
    progress(1.0)


def verify_pbp(path, layouts, progress, stopped):
    """Read the finished PBP back with the same code that converts PBPs to CHDs and compare every block with the
    disc it was made from."""
    discs = pbp_discs(path)
    if len(discs) != len(layouts):
        return False
    done, all_blocks = 0, sum(-(-l["total"] // 16) for l in layouts) or 1
    with open(path, "rb") as f:
        for base, lay in zip(discs, layouts):
            tracks, total = pbp_toc(f, base)
            if total != lay["total"] or [(d, l) for d, l in tracks] != lay["tracks"]:
                return False
            blocks = -(-total // 16)
            f.seek(base + 0x4000)
            index = f.read(blocks * 32)
            for b, block in enumerate(iter_disc_blocks(lay)):
                off, ln = struct.unpack("<IH", index[b * 32:b * 32 + 6])
                f.seek(base + 0x100000 + off)
                raw = f.read(ln)
                try:
                    chunk = raw if ln == PBP_BLOCK else zlib.decompress(raw, -15)
                except zlib.error:
                    return False
                if chunk != block:
                    return False
                done += 1
                if b % 64 == 0:
                    progress(done / all_blocks)
                    if stopped():
                        raise UnpackError("stopped")
    return True


# A PBP only runs on a real PSP/Vita if it carries Sony's PS1 loader, which can't be made from a disc and is not
# shipped with CHD Manager. The user can point the app at any working PS1 PBP, or let it fetch this generic one
# (pinned to one commit of the public repository and checked against a fixed checksum).
BASE_PBP_FILES = [   # (name, url, sha-256)
    ("BASE.PBP", "https://raw.githubusercontent.com/julianxhokaxhiu/iPoPS/f8afadd32e9e55e0ea9bf73cf3306890ffebe629/BASE.PBP",
     "de0d743be8e721b4167679de5655c0df99667844e0da31bcd96397b09b192614"),
    ("DATA.PSP", "https://raw.githubusercontent.com/julianxhokaxhiu/iPoPS/f8afadd32e9e55e0ea9bf73cf3306890ffebe629/DATA.PSP",
     "9a995e281433c6671e8fc9efc16a130f865e5db03e2d5e6cfbba29ab34ec2fb8"),
]


def find_base_pbp(saved=""):
    """A usable base PBP: the one chosen in Settings, else BASE.PBP next to the program. None if there isn't one."""
    for c in (saved, str(HERE / "BASE.PBP")):
        if c and Path(c).is_file():
            try:
                read_base_pbp(c)
                return Path(c)
            except (UnpackError, OSError):
                continue
    return None


PATH_SETTINGS = ("chdman", "base_pbp", "art_dir", "out_dir", "dolphintool", "last_dir")


def relocate_settings(settings, here=None):
    """The program folder was moved: saved paths that pointed inside the old folder now point inside the new one, and a
    saved file that is gone but sits next to the program under the same name (BASE.PBP, chdman.exe...) is found again.
    Changes `settings` in place; returns the names of the settings that changed."""
    here = Path(here or HERE)
    norm = lambda p: os.path.normcase(os.path.normpath(str(p)))  # noqa: E731
    old = settings.get("app_dir", "")
    changed = []
    for k in PATH_SETTINGS:
        v = settings.get(k) or ""
        if not v:
            continue
        new = None
        if old and norm(old) != norm(here):
            try:
                new = here / Path(v).relative_to(old)
            except ValueError:
                pass
        if new is None and k in ("chdman", "base_pbp", "dolphintool") and not Path(v).exists():
            new = here / Path(v).name
        if new is not None and norm(new) != norm(v) and new.exists():
            settings[k] = str(new)
            changed.append(k)
    return changed


def download_base_pbp(dest_dir):
    """Fetch BASE.PBP and DATA.PSP into dest_dir, each checked against its fixed checksum, and keep them only if
    together they make a usable base."""
    dest_dir = Path(dest_dir)
    tmp = Path(tempfile.mkdtemp(prefix="chdm_base_"))
    try:
        for name, url, want in BASE_PBP_FILES:
            h, got = hashlib.sha256(), 0
            with http_get(url, 60) as r, open(tmp / name, "wb") as f:
                while True:
                    chunk = r.read(1 << 18)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    if got > 20 << 20:
                        raise RuntimeError("the download is far larger than expected")
            if h.hexdigest() != want:
                raise RuntimeError(f"the checksum of {name} doesn't match, so the download was discarded")
        read_base_pbp(tmp / "BASE.PBP")
        for name, _url, _want in BASE_PBP_FILES:
            os.replace(tmp / name, dest_dir / name)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------- CHD -> CDI (DiscJuggler, version 2 layout)
CUE_SS = {"AUDIO": 2352, "MODE1/2352": 2352, "MODE2/2352": 2352, "MODE1/2048": 2048, "MODE2/2336": 2336}
CDI_GAP = 11250         # sectors between the two sessions of a Dreamcast disc


def cue_tracks(cue):
    """-> (bin path, [dict(type, ss, start_byte, sectors)]) for the single-BIN .cue chdman writes."""
    cue = Path(cue)
    binf, tracks = None, []
    for line in cue.read_text("utf-8", "replace").splitlines():
        t = line.strip()
        m = re.match(r'FILE\s+(?:"([^"]+)"|(\S+))', t, re.I)
        if m:
            if binf is not None:
                raise UnpackError("this disc is stored in several files")
            binf = cue.parent / (m.group(1) or m.group(2))
            continue
        m = re.match(r"TRACK\s+\d+\s+(\S+)", t, re.I)
        if m:
            kind = m.group(1).upper()
            if kind not in CUE_SS:
                raise UnpackError(f"track type {kind} isn't supported")
            tracks.append(dict(type=kind, ss=CUE_SS[kind], idx=None))
            continue
        m = re.match(r"INDEX\s+01\s+(\d+):(\d+):(\d+)", t, re.I)
        if m and tracks:
            tracks[-1]["idx"] = (int(m.group(1)) * 60 + int(m.group(2))) * 75 + int(m.group(3))
    if binf is None or not tracks or any(t["idx"] is None for t in tracks):
        raise UnpackError("can't read the track list of this disc")
    size, off = binf.stat().st_size, 0
    for i, t in enumerate(tracks):
        t["start_byte"] = off
        t["sectors"] = (tracks[i + 1]["idx"] - t["idx"]) if i + 1 < len(tracks) else (size - off) // t["ss"]
        off += t["sectors"] * t["ss"]
    if off > size or any(t["sectors"] <= 0 for t in tracks):
        raise UnpackError("the track list doesn't match the data file")
    return binf, tracks


def cdi_plan(binf, tracks):
    """Decide the CDI layout: sessions, and for every track its stored sector size, mode and start sector."""
    first_data = next((i for i, t in enumerate(tracks) if t["type"] != "AUDIO"), None)
    dc = (first_data is not None and first_data > 0 and tracks[first_data]["type"] == "MODE2/2352"
          and sniff_platform(binf, tracks[first_data]["start_byte"]) == "Dreamcast")
    gap = CDI_GAP
    if dc:
        # A Dreamcast data session was mastered to start at one particular sector: the ISO's own directory
        # addresses say which, so read it from there rather than assuming the usual gap.
        t = tracks[first_data]
        r = DiscReader(binf, 2352, 24, t["start_byte"])
        ext = struct.unpack("<I", r.sector(16)[158:162])[0]
        for rel in range(17, 200):
            sec = r.sector(rel)
            if len(sec) > 33 and sec[0] >= 0x22 and sec[25] & 2 and sec[32] == 1 and sec[33] == 0 \
                    and struct.unpack("<I", sec[2:6])[0] == ext:
                before = sum(150 + q["sectors"] for q in tracks[:first_data])
                if 0 <= ext - rel - before < 100000:
                    gap = ext - rel - before
                break
    plan, lba = [], 0
    for i, t in enumerate(tracks):
        session = 1 if dc and i >= first_data else 0
        if dc and i == first_data:
            lba += gap
        kind = t["type"]
        mode = 0 if kind == "AUDIO" else 1 if kind.startswith("MODE1") else 2
        if kind == "MODE2/2352" and dc:
            ss, code = 2336, 1                      # DiscJuggler keeps Dreamcast data sectors without sync/header
        else:
            ss, code = t["ss"], {2048: 0, 2336: 1, 2352: 2}[t["ss"]]
        plan.append(dict(t, session=session, mode=mode, out_ss=ss, code=code, start_lba=lba, total=150 + t["sectors"]))
        lba += 150 + t["sectors"]
    return plan, dc, lba


def _cdi_volume_label(binf, tracks):
    for t in tracks:
        if t["type"] != "AUDIO":
            off = 16 if t["type"] == "MODE1/2352" else 24
            ss = t["ss"]
            r = DiscReader(binf, ss, off if ss == 2352 else 0, t["start_byte"])
            pvd = r.sector(16)
            if pvd[1:6] == b"CD001":
                return pvd[40:72].decode("latin1", "replace").strip()[:30] or "DISC"
    return "DISC"


def write_cdi(out_path, cue, progress, stopped):
    """CD CHD (extracted to cue/bin) -> DiscJuggler .cdi."""
    binf, tracks = cue_tracks(cue)
    plan, dc, end_lba = cdi_plan(binf, tracks)
    name = Path(out_path).name.replace(".part", "").encode("ascii", "replace")[:60]
    capacity = 333000 if end_lba <= 333000 else 360000 if end_lba <= 360000 else 405000 if end_lba <= 405000 else end_lba
    total_sectors = sum(p["sectors"] for p in plan) or 1
    done = 0
    rec = []
    with open(binf, "rb") as f, open(out_path, "wb") as g:
        for n, p in enumerate(plan):
            if p["mode"] == 2 and p["out_ss"] == 2336:
                g.write((b"\x00\x00\x20\x00\x00\x00\x20\x00" + bytes(2328)) * 150)      # an empty Form 2 sector, as on real discs
            else:
                g.write(bytes(150 * p["out_ss"]))
            f.seek(p["start_byte"])
            left = p["sectors"]
            while left:
                c = min(left, 2048)
                raw = f.read(c * p["ss"])
                if len(raw) != c * p["ss"]:
                    raise UnpackError("the extracted disc is shorter than its track list says")
                if p["out_ss"] != p["ss"]:
                    raw = b"".join(raw[i * p["ss"] + 16:(i + 1) * p["ss"]] for i in range(c))
                g.write(raw)
                left -= c
                done += c
                progress(done / total_sectors)
                if stopped():
                    raise UnpackError("stopped")
            idx_in_session = sum(1 for q in plan[:n] if q["session"] == p["session"])
            ctrl = 0 if p["mode"] == 0 else 4
            rec.append(
                struct.pack("<I", 0) + CDI_MARK * 2 + bytes(4) + bytes((len(name),)) + name
                + bytes.fromhex("00000000000000000000000200000000000000") + struct.pack("<I", capacity) + b"\x02\x00"
                + struct.pack("<II", 150, p["sectors"]) + bytes(6) + struct.pack("<I", p["mode"])
                + struct.pack("<III", 0, p["session"], idx_in_session) + struct.pack("<II", p["start_lba"], p["total"])
                + bytes(16) + struct.pack("<I", p["code"]) + bytes((ctrl, 0, 0, 0, 0)) + struct.pack("<I", p["total"]) + bytes(20))
        sessions = [[i for i, p in enumerate(plan) if p["session"] == s] for s in range(2 if dc else 1)]
        head = struct.pack("<H", len(sessions))
        for si, members in enumerate(sessions):
            head += struct.pack("<H", len(members)) + b"".join(rec[i] for i in members)
            if si == len(sessions) - 1:
                head += struct.pack("<III", 2, 0, plan[members[-1]]["start_lba"])
            else:
                head += bytes(12)
        label = _cdi_volume_label(binf, tracks).encode("ascii", "replace")
        head += b"\x00\x00" + struct.pack("<I", end_lba) + bytes((len(label),)) + label + b"\x01\x00\x00\x00\x01\x00\x00\x00" + bytes(29)
        pos = g.tell()
        g.write(head)
        g.write(struct.pack("<II", CDI_V2, pos))
    progress(1.0)


def verify_cdi(out_path, cue, progress, stopped):
    """Read the finished CDI back and compare every track's data with the disc it was made from."""
    binf, tracks = cue_tracks(cue)
    plan, _dc, _end = cdi_plan(binf, tracks)
    back = parse_cdi(out_path)
    if len(back) != len(plan):
        return False
    done, total = 0, sum(p["sectors"] for p in plan) or 1
    with open(out_path, "rb") as f, open(binf, "rb") as b:
        for p, t in zip(plan, back):
            if (t["mode"], t["ss"], t["length"], t["pregap"], t["session"] - 1, t["start_lba"]) != (
                    p["mode"], p["out_ss"], p["sectors"], 150, p["session"], p["start_lba"]):
                return False
            f.seek(t["position"] + 150 * p["out_ss"])
            b.seek(p["start_byte"])
            left = p["sectors"]
            while left:
                c = min(left, 2048)
                a, raw = f.read(c * p["out_ss"]), b.read(c * p["ss"])
                if p["out_ss"] != p["ss"]:
                    raw = b"".join(raw[i * p["ss"] + 16:(i + 1) * p["ss"]] for i in range(c))
                if a != raw:
                    return False
                left -= c
                done += c
                progress(done / total)
                if stopped():
                    raise UnpackError("stopped")
    return True


# ------------------------------------------------------------------ CSO (compressed ISO, used for PSP games)
def read_cso_header(path):
    """-> (size of the ISO in bytes, block size, offset shift). Works for CSO (deflate) and ZSO (LZ4)."""
    with open(path, "rb") as f:
        h = f.read(24)
    if len(h) < 24 or h[:4] not in (b"CISO", b"ZISO"):
        raise UnpackError("this is not a CSO or ZSO file")
    hsize, total, block, ver, shift = struct.unpack("<IQIBB2x", h[4:])
    if h[:4] == b"CISO" and hsize != 24:
        raise UnpackError("this is a GameCube/Wii CISO, not a PSP CSO")
    if ver > 1 or block < 512 or block & (block - 1) or shift > 8 or not total:
        raise UnpackError("this CSO uses a version or layout CHD Manager can't read")
    return total, block, shift


def cso_is_zso(path):
    with open(path, "rb") as f:
        return f.read(4) == b"ZISO"


def iter_cso(path):
    """Yield (block count, the ISO bytes of each block) of a CSO or ZSO, in order."""
    total, block, shift = read_cso_header(path)
    lz = cso_is_zso(path)
    n = (total + block - 1) // block
    with open(path, "rb") as f:
        f.seek(24)
        idx = struct.unpack("<%dI" % (n + 1), f.read(4 * (n + 1)))
        for i in range(n):
            a, b = idx[i], idx[i + 1]
            start, end = (a & 0x7FFFFFFF) << shift, (b & 0x7FFFFFFF) << shift
            if f.tell() != start:
                f.seek(start)
            raw = f.read(end - start)
            need = min(block, total - i * block)
            try:
                if a & 0x80000000:
                    data = raw[:block]
                elif lz:
                    data = lz4_decompress(raw, block)
                else:
                    data = zlib.decompressobj(-15).decompress(raw)
            except zlib.error:
                data = b""
            if len(data) < need:
                raise UnpackError(f"the {'ZSO' if lz else 'CSO'} is damaged (block {i})")
            yield n, data[:need]


def cso_to_iso(src, dst, progress, stopped):
    done = 0
    with open(dst, "wb") as g:
        for n, data in iter_cso(src):
            g.write(data)
            done += 1
            if done % 2048 == 0:
                progress(done / n)
                if stopped():
                    raise UnpackError("stopped")
    progress(1.0)


def iso_to_cso(src, dst, progress, stopped, level=9, lz4=False):
    """ISO -> CSO version 1 (blocks raw-deflated) or, with lz4, ZSO (blocks LZ4-compressed): 2048-byte blocks,
    stored as they are when compressing doesn't shrink them."""
    total, block = os.path.getsize(src), 2048
    n = (total + block - 1) // block
    shift = 0
    # offsets must fit in 31 bits; an LZ4 block can't be padded safely, so a ZSO is aligned only when it has to be
    while 24 + 4 * (n + 1) + total + n * ((1 << shift) - 1) >= 1 << (31 + shift):
        shift += 1
    align = 1 << shift
    index = [0] * (n + 1)
    pos = 24 + 4 * (n + 1)
    with open(src, "rb") as f, open(dst, "wb") as g:
        g.write(struct.pack("<4sIQIBB2x", b"ZISO" if lz4 else b"CISO", 24, total, block, 1, shift) + b"\0" * (4 * (n + 1)))
        for i in range(n + 1):
            pad = -pos % align
            if pad:
                g.write(b"\0" * pad)
                pos += pad
            if i == n:
                index[i] = pos >> shift
                break
            data = f.read(block).ljust(block, b"\0")
            if lz4:
                comp = lz4_compress(data)
            else:
                c = zlib.compressobj(level, zlib.DEFLATED, -15)
                comp = c.compress(data) + c.flush()
            if len(comp) >= block:
                index[i] = (pos >> shift) | 0x80000000
                g.write(data)
                pos += block
            else:
                index[i] = pos >> shift
                g.write(comp)
                pos += len(comp)
            if i % 2048 == 0:
                progress(i / n)
                if stopped():
                    raise UnpackError("stopped")
        g.seek(24)
        g.write(struct.pack("<%dI" % (n + 1), *index))
    progress(1.0)


def verify_cso(cso, iso, progress, stopped):
    """True when the CSO unpacks to exactly this ISO."""
    done = 0
    with open(iso, "rb") as f:
        for n, data in iter_cso(cso):
            if f.read(len(data)) != data:
                return False
            done += 1
            if done % 2048 == 0:
                progress(done / n)
                if stopped():
                    raise UnpackError("stopped")
        return f.read(1) == b""


def cso_platform(path):
    """Platform of the disc inside a CSO, found by unpacking its first 2 MB."""
    try:
        tmp = Path(tempfile.mkdtemp(prefix="chdm_sniff_"))
        try:
            head = tmp / "head.iso"
            with open(head, "wb") as g:
                for i, (_, data) in enumerate(iter_cso(path)):
                    g.write(data)
                    if i >= 1000:
                        break
            return sniff_platform(head)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    except (UnpackError, OSError):
        return ""


# ------------------------------------------------------ game IDs and titles (for naming output files)
GAMEDB_FILE = ("gameInfo.db", "https://raw.githubusercontent.com/RupertAvery/PSXPackager/453c0e7b11fa46d78fa1939473923699894fde03/"
               "Popstation.Database/Resources/gameInfo.db", "fdea8792f8f173e434fd094038e81c14b77db71c00d1c64a11f6ce5b49c4cce9")
_GAMEDB = {}


def find_gamedb():
    p = HERE / GAMEDB_FILE[0]
    return p if p.is_file() else None


def load_gamedb(path):
    """gameInfo.db (from the PSXPackager project): game ID -> title, title of the whole game, region."""
    key = str(path)
    if key not in _GAMEDB:
        db = {}
        try:
            for line in Path(path).read_text("utf-8", "replace").splitlines():
                p = line.split(";")
                if len(p) >= 6:
                    db[p[5].strip().upper()] = dict(main_id=p[1], main_title=p[2], title=p[3], region=p[4])
        except OSError:
            pass
        _GAMEDB[key] = db
    return _GAMEDB[key]


def download_gamedb(dest_dir):
    name, url, want = GAMEDB_FILE
    tmp = Path(tempfile.mkdtemp(prefix="chdm_db_"))
    try:
        h, got = hashlib.sha256(), 0
        with http_get(url, 60) as r, open(tmp / name, "wb") as f:
            while True:
                chunk = r.read(1 << 18)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                got += len(chunk)
                if got > 10 << 20:
                    raise RuntimeError("the download is far larger than expected")
        if h.hexdigest() != want:
            raise RuntimeError("the checksum doesn't match, so the download was discarded")
        os.replace(tmp / name, Path(dest_dir) / name)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def sfo_get(sfo, key):
    """One text value of a PARAM.SFO ('' if missing)."""
    if sfo[:4] != b"\0PSF":
        return ""
    kofs, dofs, n = struct.unpack("<III", sfo[8:20])
    for i in range(n):
        ko, fmt, ln, _mx, do = struct.unpack("<HHIII", sfo[20 + 16 * i:36 + 16 * i])
        if sfo[kofs + ko:sfo.index(b"\0", kofs + ko)].decode("latin1") == key:
            return sfo[dofs + do:dofs + do + ln].split(b"\0")[0].decode("utf-8", "replace")
    return ""


def pbp_game_id(path):
    """DISC_ID of a PBP ('SLUS01545'), '' if it has none."""
    try:
        with open(path, "rb") as f:
            o = struct.unpack("<10I", f.read(0x28))
            f.seek(o[2])
            return sfo_get(f.read(o[3] - o[2]), "DISC_ID").upper()
    except (OSError, struct.error, ValueError):
        return ""


def target_label(key):
    """Label of a target key, including the per-disc ones ('chd_d2' = CHD of disc 2 only)."""
    base, _, d = key.partition("_d")
    return TARGET_LABELS.get(base, "") + (f", disc {d} only" if d else "") if key not in TARGET_LABELS else TARGET_LABELS[key]


# What a row's Action drop-down can offer. 'auto' is for CHDs that hold something else (hard disk, laserdisc, raw).
TARGET_LABELS = {"chd": "→ CHD", "cue": "→ CUE/BIN", "cue_split": "→ CUE/BIN, one BIN per track",
                 "cue_single": "→ CUE/BIN, one single BIN", "iso": "→ ISO", "gdi": "→ GDI",
                 "pbp": "→ PlayStation PBP", "cdi": "→ DiscJuggler CDI (experimental)", "cso": "→ CSO (compressed ISO)",
                 "verify": "Verify only", "recompress": "Recompress", "auto": "Extract", "art": "→ Artwork images (PNG)",
                 "cue_fix": "→ CUE/BIN, error correction rebuilt", "zso": "→ ZSO (LZ4 compressed ISO)",
                 "ecm": "→ ECM (error codes removed)", "wbfs": "→ WBFS", "ciso": "→ CISO", "gcz": "→ GCZ",
                 "rvz": "→ RVZ (Dolphin, compressed)", "wia": "→ WIA", "wud": "→ WUD (raw Wii U disc)",
                 "wux": "→ WUX (compressed Wii U disc)"}
# how the CHD extraction code names each target
TARGET_FMT = {"cue": "auto", "cue_split": "split", "cue_single": "single", "iso": "iso", "gdi": "auto", "pbp": "pbp",
              "cdi": "cdi", "cso": "cso", "auto": "auto", "cue_fix": "fix", "zso": "zso", "ecm": "ecm"}
CUE_TYPE = {"AUDIO": "AUDIO", "MODE1/2048": "MODE1", "MODE1/2352": "MODE1_RAW", "MODE2/2352": "MODE2_RAW"}


def collections_counter(jobs):
    c = {}
    for j in jobs:
        c[j.kind] = c.get(j.kind, 0) + 1
    return c


def parse_cue_tracks(p):
    """[(bin file, track type)] for each TRACK in a .cue"""
    out, cur = [], None
    try:
        for line in p.read_text("utf-8", "replace").splitlines():
            t = line.strip()
            m = re.match(r'FILE\s+(?:"([^"]+)"|(\S+))', t, re.I)
            if m:
                cur = p.parent / (m.group(1) or m.group(2))
                continue
            m = re.match(r"TRACK\s+\d+\s+(\S+)", t, re.I)
            if m and cur is not None:
                out.append((cur, m.group(1).upper()))
    except OSError:
        pass
    return out


# ------------------------------------------------------------------------- Job
# ---- Disk space: a conversion that clearly cannot fit is refused before anything is written
SPACE_MARGIN = 64 * 1024 ** 2        # file system overhead and a little slack
# What a target's file is, against the size of the unpacked disc. Compressed targets use a typical figure, not the
# best case, so a job is only refused when it clearly won't fit. Anything not listed is taken at the full size.
SPACE_RATIO = {"chd": 0.7, "cso": 0.95, "zso": 0.95, "ecm": 0.95, "pbp": 0.8, "rvz": 0.8, "wia": 0.8, "gcz": 0.8,
               "wbfs": 0.95, "art": 0.0}
SPACE_EXT = {"chd": ".chd", "recompress": ".chd", "cue": ".cue", "cue_split": ".cue", "cue_single": ".cue", "iso": ".iso",
             "gdi": ".gdi", "pbp": ".pbp", "cdi": ".cdi", "cso": ".cso", "zso": ".zso", "ecm": ".ecm", "rvz": ".rvz",
             "wia": ".wia", "gcz": ".gcz", "ciso": ".ciso", "wbfs": ".wbfs", "wux": ".wux", "wud": ".wud"}


def chd_logical_size(path):
    """The unpacked size of a CHD in bytes, read from its header (None when it can't be read)."""
    try:
        with open(path, "rb") as f:
            h = f.read(40)
    except OSError:
        return None
    if len(h) < 40 or h[:8] != b"MComprHD":
        return None
    ver = struct.unpack(">I", h[12:16])[0]
    if ver == 5:
        return struct.unpack(">Q", h[32:40])[0]
    if ver in (3, 4):
        return struct.unpack(">Q", h[28:36])[0]
    return None


def free_space(path):
    """(the nearest folder that exists, bytes free on its drive)."""
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    return p, shutil.disk_usage(p).free


class Job:
    def __init__(self, path, root=None):
        self.path = Path(path)
        self.root = Path(root) if root else None
        self.kind = "convert"          # convert | extract | verify | recompress
        self.home = self.path          # the file the user added; outputs are named after it even while a temp copy is worked on
        self.target = None             # what this row becomes: a key of TARGET_LABELS (chosen in the Action drop-down)
        self.types = []                # track types in CHD style: AUDIO, MODE1, MODE1_RAW, MODE2_RAW (empty = unknown)
        self.action_text = ""
        self.override = "Auto"         # Auto | CD | DVD
        self.auto_type, self.reason = "CD", ""
        self.platform = ""
        self.disc = ""                 # for CHDs: CD / GD-ROM / DVD / HD / ...
        self.tracks = 0                # for CD CHDs: number of tracks
        self.container = None          # "pbp" | "cdi" | "image": unpacked to CUE/BIN before chdman sees it
        self.disc_count = 1            # discs inside a PBP
        self.m3u_chds = []             # for a playlist: its CHD files, one per disc
        self.size = 0
        self._raw = None
        self.src_files = []
        self.missing = []
        self.lone_bin = False
        self.game_id, self.name_warned, self.disc_pick = "", False, None
        self.archive, self.extract_dir = None, None   # set when the file was unpacked from an archive
        self.wrap_cue = None           # a raw image without a CUE: the track type found in its first sector ('MODE2/2352')
        self.disc_info = {}            # GameCube/Wii discs: id, title and the container format
        self.status = "Queued"
        self.result = ""
        self.out_path = None
        self.out_size = 0

    @property
    def type(self):
        return self.override if self.override != "Auto" else self.auto_type

    def raw_size(self):
        """About how many bytes the disc holds unpacked: a CHD, CSO, PBP or Wii disc file is smaller than what is in it."""
        if self._raw is None:
            self._raw = self._raw_size()
        return self._raw

    def _raw_size(self):
        p, ext = self.path, self.path.suffix.lower()
        try:
            if ext == ".chd":
                return chd_logical_size(p) or self.size * 3
            if ext == ".m3u":
                return sum(chd_logical_size(c) or c.stat().st_size * 3 for c in self.m3u_chds)
            if self.container == "cso":
                return read_cso_header(p)[0]
            if self.container == "disc":
                d = open_disc(p)
                try:
                    return d.size
                finally:
                    d.close()
            if self.container == "ecm":
                return int(self.size * 1.2)
            if self.container == "pbp":
                n = int(self.size * 1.6)
                return n // self.disc_count if self.disc_pick and self.disc_count > 1 else n
        except (OSError, UnpackError, ValueError, struct.error):
            pass
        return self.size

    def set_target(self, target):
        self.target = target
        base, _, d = target.partition("_d")
        self.disc_pick = int(d) if d else None          # 'chd_d2': only disc 2 of a multi-disc PBP
        self.kind = {"chd": "convert", "verify": "verify", "recompress": "recompress"}.get(base, "extract")

    def default_target(self):
        t = [k for k, _ in self.available_targets()]
        if self.path.suffix.lower() == ".chd":
            return next((k for k in ("cue_split", "cue", "gdi", "iso", "auto") if k in t), "verify")
        return t[0] if t else "chd"

    def available_targets(self):
        """[(key, label)] of everything this item can safely become. Nothing is offered that is known not to work."""
        if self.container == "disc":                # GameCube / Wii: Dolphin's formats (a CHD of these runs nowhere)
            own = self.disc_info.get("format", "ISO").lower()
            if self.platform == "Wii U":
                return [(k, target_label(k)) for k in ("wux", "wud") if k != own]
            keys = [k for k in ["rvz", "iso", "gcz", "wia", "ciso"] + (["wbfs"] if self.platform == "Wii" else []) if k != own]
            if own != "iso":
                keys.insert(0, keys.pop(keys.index("iso")))
            if self.platform == "Wii":                  # RVZ, WIA and WBFS from a Wii disc need the user's key (or their Dolphin)
                keys = [k for k in keys if not (k in ("rvz", "wia") and not wii_can("rvz")) and not (k == "wbfs" and not wii_can("wbfs"))]
            return [(k, target_label(k)) for k in keys]
        ext, ty = self.path.suffix.lower(), self.types
        data = [x for x in ty if x != "AUDIO"]
        single = len(ty) == 1 and ty[0].startswith("MODE")
        raw = bool(data) and all(x.endswith("_RAW") for x in data)
        keys = []
        if ext == ".chd":
            if self.disc == "CD":
                keys += ["cue"] if len(ty) <= 1 else ["cue_split", "cue_single"]
                if single:
                    keys += ["iso", "cso", "zso"]
                if raw and data[0].startswith("MODE2"):
                    keys.append("pbp")
                if raw:
                    keys.append("cdi")
                if raw:
                    keys.append("cue_fix")
                if raw and len(ty) == 1:
                    keys.append("ecm")
            elif self.disc == "GD-ROM":
                keys.append("gdi")
            elif self.disc == "DVD":
                keys += ["iso", "cso", "zso"]
            else:
                keys.append("auto")
            keys.append("verify")
            if self.disc in ("CD", "GD-ROM", "DVD"):
                keys.append("recompress")
        elif ext == ".m3u":
            keys.append("pbp")
        else:
            keys.append("chd")
            if ty and self.container in ("pbp", "cdi", "image", "ecm"):
                keys.append("cue")
            if self.container == "pbp":
                keys.append("art")
            if ty and self.disc_count == 1:
                if single:
                    keys += [k for k, skip in (("iso", ".iso"), ("cso", ".cso"), ("zso", ".zso")) if ext != skip or self.wrap_cue]
                if raw and self.container != "cso":
                    if self.platform == "PS1" and ext != ".pbp":
                        keys.append("pbp")
                    if ext != ".cdi":
                        keys.append("cdi")
                    keys.append("cue_fix")
                    if len(ty) == 1 and ext != ".ecm":
                        keys.append("ecm")
                elif ext in (".iso", ".cso") and self.platform == "PS1":
                    keys.append("pbp")            # a plain PS1 ISO: raw sectors are rebuilt for the PBP
        if self.container == "pbp" and self.disc_count > 1:
            keys += [f"{b}_d{n}" for b in ("chd", "cue") for n in range(1, self.disc_count + 1)]
        return [(k, target_label(k)) for k in dict.fromkeys(keys)]

    @property
    def can_pick(self):
        return len(self.available_targets()) > 1

    def analyze(self, _=None):
        self._analyze()
        if self.target not in [k for k, _ in self.available_targets()]:
            self.set_target(self.default_target())
        self.game_id = self.guess_game_id()

    def guess_game_id(self):
        """The PlayStation game code ('SLUS01545') when it can be read straight from the file, else ''."""
        ext = self.path.suffix.lower()
        if self.container == "disc":
            return self.disc_info.get("id", "")
        try:
            if ext == ".pbp":
                return pbp_game_id(self.path)
            if self.platform not in ("PS1", "PS2"):
                return ""
            if ext == ".cue":
                c = cue_data_start(self.path)
                files = [c[0]] if c and c[0].exists() else []
            else:
                files = [self.path] if ext in (".iso", ".bin") else []
            for f in files:
                for stride, off in ((2352, 24), (2352, 16), (2048, 0)):
                    gid = ps1_boot_id(f, off, stride, ps2=self.platform == "PS2")
                    if gid:
                        return gid
        except (OSError, ValueError, struct.error):
            pass
        return ""

    def _analyze(self):
        p, ext = self.path, self.path.suffix.lower()
        if ext == ".chd":
            self.size = p.stat().st_size
            self.disc, self.types = chd_probe(p) if CHDMAN else ("?", [])
            self.tracks = len(self.types)
            self.src_files = [p]
            return
        kind = disc_kind(p) if ext in (".iso", ".gcm", ".gcz", ".rvz", ".wia", ".wbfs", ".ciso", ".wux", ".wud") else ""
        if kind:
            d = open_disc(p)
            try:
                self.disc_info = dict(d.info(), format="WUD" if kind == "Wii U" and d.kind == "ISO" else d.kind)
                self.src_files = [Path(f.name) for f in getattr(d, "files", [])] or [p]
            finally:
                d.close()
            self.container, self.platform, self.disc = "disc", kind, f"{kind} disc"
            self.size = sum(f.stat().st_size for f in self.src_files)
            self.auto_type, self.reason = "DVD", f"{kind} disc image"
            return
        if ext in (".gcm", ".gcz", ".rvz", ".wia", ".wbfs", ".wux", ".wud"):
            raise ValueError("this doesn't look like a readable GameCube/Wii/Wii U disc image (damaged, or not a disc)")
        if ext == ".ecm":
            t = ecm_probe(p)
            self.container, self.size, self.src_files = "ecm", p.stat().st_size, [p]
            self.types = [CUE_TYPE[t]] if t in CUE_TYPE else []
            self.auto_type, self.reason = "CD", f"ECM file ({t or 'sector type unknown'})"
            return
        if ext in (".cso", ".zso", ".ciso"):
            total = read_cso_header(p)[0]
            self.size, self.src_files, self.container, self.types = p.stat().st_size, [p], "cso", ["MODE1"]
            self.platform = cso_platform(p)
            self.auto_type, self.reason = decide_type(".iso", total, "")
            return
        if ext == ".m3u":
            lines = [ln.strip() for ln in p.read_text("utf-8-sig", "replace").splitlines() if ln.strip() and not ln.startswith("#")]
            self.m3u_chds = [p.parent / ln for ln in lines]
            bad = [c.name for c in self.m3u_chds if c.suffix.lower() != ".chd" or not c.is_file()]
            if not self.m3u_chds or bad or len(self.m3u_chds) > 5:
                raise ValueError("a playlist must list 1-5 existing .chd files" + (f" (problem: {', '.join(bad[:2])})" if bad else ""))
            self.disc, self.platform, self.disc_count = "CD", "PS1", len(self.m3u_chds)
            self.src_files = [p] + self.m3u_chds
            self.size = sum(f.stat().st_size for f in self.src_files)
            return
        if ext in IMAGE_EXTS:
            tracks = read_image(p)
            self.container = "image"
            self.types = ["AUDIO" if t["kind"] == "AUDIO" else "MODE1" if t["kind"] == "MODE1" and t["stride"] == 2048 else
                          f"{t['kind']}_RAW" for t in tracks]
            self.src_files = image_sources(p, tracks)
            self.size = sum(f.stat().st_size for f in self.src_files)
            self.platform = image_platform(tracks)
            self.auto_type, self.reason = decide_type(ext, sum(t["count"] * t["ss"] for t in tracks), self.platform)
            return
        if ext in (".pbp", ".cdi"):
            self.container = ext[1:]
            self.size = p.stat().st_size
            self.src_files = [p]
            if ext == ".pbp":
                self.disc_count = len(pbp_discs(p))
                self.platform = "PS1"
                self.types = ["MODE2_RAW", "AUDIO"]       # a PBP may hold audio tracks, so no ISO/CSO is offered for it
            else:
                cdi = parse_cdi(p)
                self.types = ["AUDIO" if t["mode"] == 0 else f"MODE{t['mode']}_RAW" for t in cdi]
                if any(t["session"] > 1 and t["mode"] == 2 for t in cdi):
                    self.platform = "Dreamcast"
            self.auto_type, self.reason = decide_type(ext, self.size, self.platform)
            return
        if ext == ".cue":
            refs = parse_cue(p)
        elif ext == ".gdi":
            refs = parse_gdi(p)
        elif ext == ".toc":
            refs = parse_toc(p)
        else:
            refs = [p]
            self.lone_bin = ext == ".bin"
        self.src_files = [p] + [r for r in refs if r != p]
        self.missing = [r.name for r in refs if not r.exists()]
        self.size = sum(f.stat().st_size for f in self.src_files if f.exists())
        data_size = sum(r.stat().st_size for r in refs if r.exists()) or self.size
        first = next((r for r in refs if r.exists()), None)
        if ext == ".gdi":
            self.platform = "Dreamcast"
        elif ext == ".cue" and cue_data_start(p) and cue_data_start(p)[0].exists():
            f0, base = cue_data_start(p)
            self.platform = sniff_platform(f0, base) or (sniff_platform(first) if first else "")
        elif first:
            self.platform = sniff_platform(first)
        self.auto_type, self.reason = decide_type(ext, data_size, self.platform)
        if ext in (".iso", ".bin") and not self.missing:
            self.wrap_cue = sniff_raw_image(p) or None
            if self.wrap_cue:
                self.types = [CUE_TYPE[self.wrap_cue]]
                self.auto_type, self.reason = "CD", f"raw {self.wrap_cue.split('/')[0]} sectors without a CUE"
            elif ext == ".iso":
                self.types = ["MODE1"]
        elif ext == ".cue":
            ty = [CUE_TYPE.get(t) for _, t in parse_cue_tracks(p)]
            self.types = ty if ty and None not in ty else []
        if self.lone_bin:
            self.reason += " (BIN without a CUE)"
        if self.missing:
            self.status = "Missing: " + ", ".join(self.missing[:2])

    def action_label(self):
        text = self.action_text or target_label(self.target or "")
        return text + "  ▾" if self.can_pick and not self.status.startswith("Done") else text

    def type_label(self):
        if self.kind == "convert":
            return self.type + ("*" if self.override != "Auto" else "")
        return self.disc or self.type

    def row(self):
        return (self.path.name, self.action_label(), self.platform, self.type_label(),
                fmt_size(self.size), self.status, self.result, str(self.path.parent))


# --------------------------------------------------------------------------- App
DEFAULTS = {
    "chdman": "", "compression": "Default", "custom_comp": "", "hunk": "", "threads": 0,
    "exists": "Skip", "verify_after": True, "delete_src": False, "delete_forever": False, "auto_cue": True,
    "extract_subfolder": False,
    "out_mode": "same", "out_dir": "", "keep_structure": True, "last_dir": "", "geometry": "",
    "base_pbp": "", "base_pbp_ask": True, "pbp_level": 6, "rvz_method": "zstd", "rvz_level": 5, "rvz_chunk": 131072, "wbfs_scrub": True, "dolphintool": "", "wii_common_key": "", "wii_korean_key": "", "wii_warn_off": False, "art_dir": "", "title_cards": True, "name_format": "%FILENAME%", "auto_log": False, "auto_update_check": True, "theme": "dark", "auto_suggest": True, "low_priority": False, "skip_version": "", "auto_app_check": True,
}


class App:
    def __init__(self, root, silent=False, initial_paths=None):
        global CHDMAN
        self.root = root
        self.silent = silent
        self.echo = None                # the command-line mode prints log lines through this
        self.no_save = False
        self.jobs = []
        self.q = queue.Queue()
        self.running = False
        self.stop_flag = False
        self.proc = None
        self.worker = None
        self.tree_ids = {}
        self.arc_dirs = {}              # unpacked archive folder -> how many jobs still use it
        self.settings = dict(DEFAULTS)
        try:
            self.settings.update(json.loads(SETTINGS_FILE.read_text("utf-8")))
        except Exception:  # noqa: BLE001
            pass
        moved = relocate_settings(self.settings)
        CHDMAN = find_chdman(self.settings.get("chdman", ""))
        restored = None
        if not CHDMAN:
            restored = restore_bundled_chdman()
            CHDMAN = find_chdman("")
        self.chd_version, self.has_dvd = chdman_info()
        self.build_ui()
        if moved:
            self.log(f"The program folder has moved, so {len(moved)} saved path(s) were updated to the new location.", "info")
        if restored:
            self.log(f"chdman.exe was missing, so the spare copy built into this program was restored: {restored}", "warn")
        if CHDMAN:
            self.log(f"Using {CHDMAN}  (chdman {self.chd_version})", "info")
            if not self.has_dvd:
                self.log("This chdman is too old to have 'createdvd'. DVD images will fall back to "
                         "'createcd'. Get a newer chdman (MAME 0.255 or later).", "warn")
        else:
            self.log("chdman.exe not found. Put it next to this script or set it in Settings, Output page.", "err")
        self.latest = None
        self.refresh_suggestions()
        ensure_keys_dir()
        self.refresh_wii()
        if initial_paths:
            self.root.after(400, lambda: self.add_paths([str(x) for x in initial_paths]))
        self.root.after(80, self.pump)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        if self.v["auto_update_check"].get() and not silent:
            self.root.after(1500, lambda: self.check_update_async(manual=False))
        if self.v["auto_app_check"].get() and not silent:
            self.root.after(2500, lambda: self.check_app_update_async(manual=False))
        if not silent:
            self.root.after(1200, self.wii_warning)

    # ------------------------------------------------------------------ UI
    def build_ui(self):
        root = self.root
        root.title(f"{APP_NAME} {APP_VER}")
        root.geometry(self.settings.get("geometry") or "1100x720")
        root.minsize(940, 580)
        # variables
        self.v = {}
        for k, d in DEFAULTS.items():
            if k in ("last_dir", "geometry", "chdman"):
                continue
            val = self.settings.get(k, d)
            var = (tk.BooleanVar if isinstance(d, bool) else tk.IntVar if isinstance(d, int) else tk.StringVar)(value=val)
            self.v[k] = var
        if self.v["theme"].get() not in THEMES:
            self.v["theme"].set("dark")
        self.t = THEMES[self.v["theme"].get()]
        self.apply_ttk_theme()
        self.make_icon()
        self.build_header()

        # ---- toolbar: only the everyday actions. Everything else lives in the "More" menu or the settings window.
        bar = ttk.Frame(root, padding=(18, 14, 18, 8))
        bar.pack(fill="x")
        self.toolbar = bar

        def btn(text, cmd):
            b = ttk.Button(bar, text=text, command=cmd)
            b.pack(side="left", padx=(0, 8))
            return b

        btn("+  Add Files", self.add_files_dialog)
        btn("+  Add Folder", self.add_folder_dialog)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=(4, 12))
        btn("Remove", self.remove_selected)
        btn("Clear all", self.clear_all)
        self.more_btn = ttk.Button(bar, text="More  ▾", command=self.show_more_menu)
        self.more_btn.pack(side="right")
        self.more_menu = tk.Menu(root, tearoff=0)
        self.more_menu.add_command(label="Settings…", command=lambda: self.open_settings("Convert"))
        self.more_menu.add_separator()
        self.more_menu.add_command(label="Info about the selected disc…", command=self.info_selected)
        self.more_menu.add_command(label="Verify the selected CHD files", command=self.verify_selected)
        self.more_menu.add_separator()
        self.more_menu.add_command(label=self.theme_menu_label(), command=self.toggle_theme)
        self.theme_menu_index = self.more_menu.index("end")
        self.more_menu.add_separator()
        self.more_menu.add_command(label="About CHD Manager…", command=lambda: self.open_settings("About"))

        # ---- bottom, packed first so it always keeps its space: footer, then action bar, then the settings line
        foot = ttk.Frame(root, padding=(18, 0, 18, 10))
        foot.pack(fill="x", side="bottom")
        self._footer_note = {"chdman": "", "app": ""}
        self.chdman_btn = ttk.Button(foot, style="Subtle.TButton", command=self.update_clicked)
        self.chdman_btn.pack(side="left")
        self.upd_btn = self.chdman_btn                      # (state changes during a chdman update use this name)
        self.app_btn = ttk.Button(foot, style="Subtle.TButton", command=self.app_footer_clicked)
        self.app_btn.pack(side="right")
        self.set_footer("chdman")
        self.set_footer("app")
        _Tip(self.chdman_btn, lambda: "Click to install the chdman update" if self.update_available()
             else "Click to check for a newer chdman")
        _Tip(self.app_btn, lambda: "Click to install the update" if self.app_update_available()
             else "Click to check for a newer CHD Manager")

        act = ttk.Frame(root, padding=(18, 8, 18, 8))
        act.pack(fill="x", side="bottom")
        self.start_btn = ttk.Button(act, text="▶  Start", style="Accent.TButton", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(act, text="■  Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        self.log_btn = ttk.Button(act, text="Show log", command=self.toggle_log)
        self.log_btn.pack(side="right")
        ttk.Button(act, text="Open output folder", command=self.open_output_folder).pack(side="right", padx=(0, 8))
        pf = ttk.Frame(act)
        pf.pack(side="left", fill="x", expand=True, padx=(18, 18))
        self.status = ttk.Label(pf, text="Ready")
        self.status.pack(anchor="w")
        self.cur_bar = ttk.Progressbar(pf, maximum=100, style="Accent.Horizontal.TProgressbar")
        self.cur_bar.pack(fill="x", pady=(4, 2))
        self.all_bar = ttk.Progressbar(pf, maximum=100, style="Slim.Horizontal.TProgressbar")
        self.all_bar.pack(fill="x")

        strip = ttk.Frame(root, padding=(18, 2, 18, 2))
        strip.pack(fill="x", side="bottom")
        self.summary_label = ttk.Label(strip, style="Muted.TLabel")
        self.summary_label.pack(side="left")
        ttk.Button(strip, text="Change settings…", style="Subtle.TButton",
                   command=lambda: self.open_settings("Convert")).pack(side="right")

        # ---- main area: the list of discs. The log joins it only when asked for.
        pw = ttk.PanedWindow(root, orient="vertical")
        pw.pack(fill="both", expand=True, padx=18, pady=(2, 4))
        self.pw = pw

        top = ttk.Frame(pw)
        pw.add(top, weight=1)
        cols = ("file", "action", "platform", "type", "size", "status", "result", "folder")
        self.tree = ttk.Treeview(top, columns=cols, displaycolumns=("file", "action", "platform", "type", "size", "status", "result"),
                                 show="headings", selectmode="extended")
        for c, t, w, a in (("file", "File", 240, "w"), ("action", "Action  ▾", 150, "w"), ("platform", "Platform", 100, "w"),
                           ("type", "Disc type", 90, "w"), ("size", "Size", 90, "e"), ("status", "Status", 150, "w"),
                           ("result", "Result", 230, "w"), ("folder", "Folder", 300, "w")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=a, stretch=(c in ("file", "status", "result", "folder")))
        ys = ttk.Scrollbar(top, orient="vertical", command=self.tree.yview)
        xs = ttk.Scrollbar(top, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        top.rowconfigure(0, weight=1)
        top.columnconfigure(0, weight=1)
        self.empty = ttk.Label(top, justify="center", style="Empty.TLabel", font=("Segoe UI", 12),
                               text="Drag files or folders here, or use “Add Files” / “Add Folder”\n\n"
                                    "Then choose what each one becomes in its Action column.\n"
                                    "CHD, CUE/BIN, GDI, ISO, CSO, PBP and CDI, plus MDS, NRG and CCD/IMG as sources")
        self.empty.place(relx=0.5, rely=0.45, anchor="center")
        self.picker, self.manual_cols, self.auto_w, self.tip, self.tip_after, self.tip_cell = None, set(), {}, None, None, None
        self.tree.bind("<Double-1>", self.on_tree_double)
        self.tree.bind("<ButtonPress-1>", self.on_tree_press, add="+")
        self.tree.bind("<ButtonRelease-1>", self.on_tree_release, add="+")
        self.tree.bind("<MouseWheel>", lambda e: self.close_picker(), add="+")
        self.tree.bind("<Motion>", self.on_tree_motion)
        self.tree.bind("<Leave>", lambda e: self.hide_tip())
        self.tree.bind("<Configure>", lambda e: self.root.after_idle(self.autofit_columns), add="+")
        self.tree.bind("<Delete>", lambda e: self.remove_selected())
        self.tree.bind("<Button-3>", self.on_right_click)
        root.bind("<Control-o>", lambda e: self.add_files_dialog())

        self.menu = tk.Menu(root, tearoff=0)
        self.menu.add_command(label="Disc type: Auto-detect", command=lambda: self.set_override("Auto"))
        self.menu.add_command(label="Disc type: force CD", command=lambda: self.set_override("CD"))
        self.menu.add_command(label="Disc type: force DVD", command=lambda: self.set_override("DVD"))
        self.menu.add_separator()
        self.menu.add_command(label="Info…", command=self.info_selected)
        self.menu.add_separator()
        self.menu.add_command(label="Open folder", command=self.open_selected_folder)
        self.menu.add_separator()
        self.menu.add_command(label="Remove from list", command=self.remove_selected)

        # the log (hidden until "Show log", or until something fails)
        self.logf = ttk.Frame(pw)
        self.logf.rowconfigure(0, weight=1)
        self.logf.columnconfigure(0, weight=1)
        self.logbox = tk.Text(self.logf, height=9, wrap="word", font=("Consolas", 9), state="disabled")
        ls = ttk.Scrollbar(self.logf, command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=ls.set)
        self.logbox.grid(row=0, column=0, sticky="nsew")
        ls.grid(row=0, column=1, sticky="ns")
        lb = ttk.Frame(self.logf)
        lb.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(lb, text="Copy log", style="Subtle.TButton", command=self.copy_log).pack(side="left", padx=(0, 6))
        ttk.Button(lb, text="Save log…", style="Subtle.TButton", command=self.save_log).pack(side="left", padx=(0, 6))
        ttk.Button(lb, text="Clear log", style="Subtle.TButton", command=self.clear_log).pack(side="left")

        # ---- settings live in their own window, not on the main screen
        sw = tk.Toplevel(root)
        sw.withdraw()
        sw.title("CHD Manager settings")
        sw.transient(root)
        sw.minsize(700, 520)
        sw.protocol("WM_DELETE_WINDOW", sw.withdraw)
        self.settings_win = sw
        nb = ttk.Notebook(sw)
        nb.pack(fill="both", expand=True, padx=16, pady=(16, 8))
        self.nb = nb
        self.build_options(nb)
        bf = ttk.Frame(sw)
        bf.pack(fill="x", padx=16, pady=(0, 14))
        ttk.Button(bf, text="Done", style="Accent.TButton", command=sw.withdraw).pack(side="right")

        for key in ("compression", "custom_comp", "hunk", "threads", "verify_after", "low_priority", "delete_src", "delete_forever",
                    "exists", "out_mode", "out_dir", "auto_suggest"):
            self.v[key].trace_add("write", lambda *a: self.refresh_summary())
        for key in ("dolphintool", "wii_common_key", "wii_korean_key", "wbfs_scrub"):
            self.v[key].trace_add("write", lambda *a: self.refresh_wii())
        self.style_widgets()
        self.refresh_summary()
        self.refresh_chd_controls()
        self.root.after(120, self.fit_toolbar)
        self.enable_file_drop()

    def build_options(self, nb):
        v = self.v

        def tab(title):
            f = ttk.Frame(nb, padding=10)
            nb.add(f, text=title)
            return f

        def row(f, r, label, widget):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=3, padx=(0, 8))
            widget.grid(row=r, column=1, sticky="w", pady=3)

        # --- Suggested
        f = tab("Suggested")
        sf = ttk.Frame(f)
        sf.pack(fill="both", expand=True)
        self.sugg_text = tk.Text(sf, width=58, height=14, wrap="word", state="disabled", cursor="arrow")
        sg = ttk.Scrollbar(sf, command=self.sugg_text.yview)
        self.sugg_text.configure(yscrollcommand=sg.set)
        sg.pack(side="right", fill="y")
        self.sugg_text.pack(side="left", fill="both", expand=True)
        sb = ttk.Frame(f)
        sb.pack(fill="x", pady=(8, 0))
        ttk.Button(sb, text="Apply suggestions", command=lambda: self.apply_suggestions(False)).pack(side="left")
        ttk.Checkbutton(sb, text="Apply automatically when files are added", variable=v["auto_suggest"]).pack(
            side="left", padx=12)
        self.sugg = None

        # --- Convert
        f = tab("Convert")
        cf = ttk.Frame(f)
        cb = ttk.Combobox(cf, textvariable=v["compression"], width=11, state="readonly",
                          values=["Default", "None", "Fast", "Smallest", "Custom"])
        cb.pack(side="left")
        ttk.Entry(cf, textvariable=v["custom_comp"], width=18).pack(side="left", padx=6)
        ttk.Label(cf, text="(custom: e.g. cdlz,cdzl,cdfl)", style="Muted.TLabel").pack(side="left")
        row(f, 0, "Compression", cf)
        hf = ttk.Frame(f)
        ttk.Entry(hf, textvariable=v["hunk"], width=10).pack(side="left")
        ttk.Label(hf, text="bytes (blank = default)", style="Muted.TLabel").pack(side="left", padx=6)
        row(f, 1, "Hunk size", hf)
        tf = ttk.Frame(f)
        ttk.Spinbox(tf, from_=0, to=64, textvariable=v["threads"], width=5).pack(side="left")
        ttk.Label(tf, text=f"0 = use all ({os.cpu_count()} available)", style="Muted.TLabel").pack(side="left", padx=6)
        row(f, 2, "Threads", tf)
        row(f, 3, "If output exists", ttk.Combobox(f, textvariable=v["exists"], width=11, state="readonly",
                                                    values=["Skip", "Overwrite", "Rename"]))
        ttk.Checkbutton(f, text="Run chdman at low priority (keeps Windows responsive)", variable=v["low_priority"]).grid(
            row=8, column=0, columnspan=2, sticky="w")
        ttk.Label(f, style="Muted.TLabel", wraplength=560, justify="left",
                  text="Tip: 'Fast' uses zlib only, the safest pick if an emulator rejects a CHD "
                       "(NetherSX2/AetherSX2 are reported to need zlib). 'None' makes full-size files, and chdman can't verify an uncompressed CHD.").grid(
            row=9, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(f, text="Verify each CHD after creating it", variable=v["verify_after"]).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(f, text="Send source files to the Recycle Bin after a VERIFIED CHD",
                        variable=v["delete_src"], command=self.on_delete_toggle).grid(
            row=5, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(f, text="Delete them permanently instead (frees the space at once; cannot be undone)",
                        variable=v["delete_forever"], command=self.on_forever_toggle).grid(
            row=6, column=0, columnspan=2, sticky="w", padx=(22, 0))
        ttk.Checkbutton(f, text="Auto-create a CUE for a lone .bin (assumes MODE2/2352, typical PS1)",
                        variable=v["auto_cue"]).grid(row=7, column=0, columnspan=2, sticky="w")

        # --- Formats (what each file becomes is chosen in its Action column; this tab only holds the extras)
        f = tab("Formats")
        ttk.Label(f, style="Muted.TLabel", wraplength=600, justify="left",
                  text="What each file becomes is chosen in the Action column of the list; only choices that can work for that file are "
                       "offered. The options below are the extras for those formats.").grid(row=0, column=0, sticky="w", pady=(0, 8))

        g1 = ttk.LabelFrame(f, text=" Output file names ", padding=10)
        g1.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ttk.Checkbutton(g1, text="Always put converted discs in a subfolder (discs with several files get one automatically)",
                        variable=v["extract_subfolder"]).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(g1, text="Name pattern (%FILENAME% %GAMEID% %TITLE% %REGION%):").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(g1, textvariable=v["name_format"], width=30).grid(row=1, column=1, padx=6, pady=(8, 0))
        ttk.Button(g1, text="Get game titles…", command=self.download_gamedb_ui).grid(row=1, column=2, pady=(8, 0))

        g2 = ttk.LabelFrame(f, text=" PlayStation 1 (PBP) ", padding=10)
        g2.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(g2, text="Base PBP for a real PSP/Vita (optional):").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Entry(g2, textvariable=v["base_pbp"], width=44).grid(row=1, column=0)
        ttk.Button(g2, text="Browse…", command=self.pick_base_pbp).grid(row=1, column=1, padx=4)
        ttk.Button(g2, text="Download…", command=self.download_base_pbp_ui).grid(row=1, column=2)
        ttk.Label(g2, text="Artwork folder (optional; one subfolder per game):").grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Entry(g2, textvariable=v["art_dir"], width=44).grid(row=3, column=0)
        ttk.Button(g2, text="Browse…", command=self.pick_art_dir).grid(row=3, column=1, padx=4)
        ttk.Checkbutton(g2, text="Draw a title card (game name and code) for PBPs that have no artwork of your own",
                        variable=v["title_cards"]).grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))
        pf = ttk.Frame(g2)
        pf.grid(row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(pf, text="Compression (0 = none, 9 = smallest):").grid(row=0, column=0, sticky="w")
        ttk.Combobox(pf, textvariable=v["pbp_level"], width=4, state="readonly", values=list(range(10))).grid(row=0, column=1, padx=6)

        g3 = ttk.LabelFrame(f, text=" Wii and GameCube (RVZ, WIA, WBFS) ", padding=10)
        g3.grid(row=3, column=0, sticky="ew")
        ttk.Label(g3, text="RVZ / WIA compression:").grid(row=0, column=0, sticky="w")
        ttk.Combobox(g3, textvariable=v["rvz_method"], width=7, state="readonly",
                     values=["zstd", "lzma", "lzma2", "bzip2", "none"]).grid(row=0, column=1, padx=(8, 4))
        ttk.Label(g3, text="level").grid(row=0, column=2)
        ttk.Combobox(g3, textvariable=v["rvz_level"], width=4, state="readonly", values=list(range(1, 10))).grid(row=0, column=3, padx=4)
        ttk.Label(g3, text="block").grid(row=0, column=4)
        ttk.Combobox(g3, textvariable=v["rvz_chunk"], width=8, state="readonly",
                     values=[32768, 65536, 131072, 262144, 524288, 1048576, 2097152]).grid(row=0, column=5, padx=4)
        ttk.Checkbutton(g3, text="WBFS: leave out data nothing uses (scrub)", variable=v["wbfs_scrub"]).grid(
            row=1, column=0, columnspan=6, sticky="w", pady=(6, 0))
        ttk.Button(g3, text="Wii setup (Dolphin or your key)…", command=self.show_wii_setup).grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.wii_status = ttk.Label(g3, text="", style="Muted.TLabel", wraplength=560, justify="left")
        self.wii_status.grid(row=3, column=0, columnspan=6, sticky="w", pady=(4, 0))

        # --- Output
        f = tab("Output")
        ttk.Radiobutton(f, text="Next to the source file", variable=v["out_mode"], value="same").grid(
            row=0, column=0, columnspan=3, sticky="w")
        ttk.Radiobutton(f, text="This folder:", variable=v["out_mode"], value="custom").grid(row=1, column=0, sticky="w")
        ttk.Entry(f, textvariable=v["out_dir"], width=34).grid(row=1, column=1, padx=6)
        ttk.Button(f, text="Browse…", command=self.pick_out_dir).grid(row=1, column=2)
        ttk.Checkbutton(f, text="Keep subfolder structure (for folders you added)", variable=v["keep_structure"]).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(4, 10))
        ttk.Label(f, text="chdman.exe:").grid(row=3, column=0, sticky="w")
        self.chd_path_var = tk.StringVar(value=CHDMAN or "")
        ttk.Entry(f, textvariable=self.chd_path_var, width=34).grid(row=3, column=1, padx=6)
        ttk.Button(f, text="Browse…", command=self.pick_chdman).grid(row=3, column=2)
        ttk.Checkbutton(f, text="Check for a newer chdman at startup (only asks GitHub; never downloads by itself)",
                        variable=v["auto_update_check"]).grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Checkbutton(f, text="Also write every run's log to chd_manager_log.txt next to the program",
                        variable=v["auto_log"]).grid(row=5, column=0, columnspan=3, sticky="w")

        # --- About
        f = tab("About")
        ttk.Label(f, text=f"CHD Manager {APP_VER}", font=("Segoe UI Semibold", 14)).grid(row=0, column=0, columnspan=4, sticky="w")
        lf = ttk.Frame(f)
        lf.grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))
        for i, (text, url) in enumerate((("GitHub page and downloads", f"https://github.com/{APP_REPO}"),
                                         ("Report a problem", f"https://github.com/{APP_REPO}/issues"),
                                         ("chdman (MAME project)", "https://github.com/mamedev/mame"))):
            lab = ttk.Label(lf, text=text, style="Link.TLabel", cursor="hand2")
            lab.grid(row=0, column=i, padx=(0, 18))
            lab.bind("<Button-1>", lambda e, u=url: webbrowser.open(u))
        ttk.Label(f, style="Muted.TLabel", wraplength=600, justify="left",
                  text="Free and open source (GNU GPL v3 or later). chdman and the other bundled parts keep their own licenses, "
                       "listed in THIRD_PARTY_NOTICES.txt. Not affiliated with MAME. No games or BIOS files are included; "
                       "use it only with discs you own.").grid(row=2, column=0, columnspan=4, sticky="w", pady=(8, 6))
        ttk.Separator(f).grid(row=3, column=0, columnspan=4, sticky="ew", pady=4)
        self.app_upd_btn = ttk.Button(f, text="Check for CHD Manager update", command=lambda: self.check_app_update_async(True))
        self.app_upd_btn.grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.app_upd_label = ttk.Label(f, text=f"You have {APP_VER}.", style="Muted.TLabel")
        self.app_upd_label.grid(row=4, column=1, columnspan=3, sticky="w", padx=(10, 0), pady=(6, 0))
        ttk.Checkbutton(f, text="Check for CHD Manager updates at startup (never installs by itself)",
                        variable=v["auto_app_check"]).grid(row=5, column=0, columnspan=4, sticky="w", pady=(6, 0))
        bf = ttk.Frame(f)
        bf.grid(row=6, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Button(bf, text="Copy diagnostics", command=self.copy_diagnostics).pack(side="left")
        ttk.Button(bf, text="Open program folder", command=lambda: os.startfile(str(HERE))).pack(side="left", padx=8)


    # ------------------------------------------------------------------ look & feel
    def apply_ttk_theme(self):
        t = self.t
        st = ttk.Style()
        st.theme_use("clam")
        self.root.configure(bg=t["bg"])
        self.root.option_add("*TCombobox*Listbox.background", t["field"])
        self.root.option_add("*TCombobox*Listbox.foreground", t["fg"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", t["accent"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        st.configure(".", background=t["bg"], foreground=t["fg"], fieldbackground=t["field"], bordercolor=t["border"],
                     lightcolor=t["border"], darkcolor=t["border"], troughcolor=t["trough"], focuscolor=t["bg"],
                     font=("Segoe UI", 10))
        st.configure("TLabel", background=t["bg"], foreground=t["fg"])
        st.configure("Muted.TLabel", foreground=t["muted"])
        st.configure("Warn.TLabel", foreground=t["warn"])
        st.configure("Link.TLabel", foreground=t["g2"] if self.v["theme"].get() == "dark" else t["accent"],
                     font=("Segoe UI", 10, "underline"))
        st.configure("Empty.TLabel", foreground=t["muted"], background=t["panel"])
        st.configure("TButton", background=t["btn"], foreground=t["fg"], borderwidth=0, padding=(10, 5), width=0,
                     focusthickness=0, relief="flat")
        st.map("TButton", background=[("disabled", t["bg"]), ("pressed", t["accent"]), ("active", t["btn_hover"])],
               foreground=[("disabled", t["muted"])])
        st.configure("Accent.TButton", background=t["accent"], foreground="#ffffff", font=("Segoe UI Semibold", 12),
                     padding=(26, 10))
        st.map("Accent.TButton", background=[("disabled", t["btn"]), ("pressed", t["accent2"]), ("active", t["accent2"])],
               foreground=[("disabled", t["muted"])])
        st.configure("Subtle.TButton", background=t["bg"], foreground=t["muted"], padding=(8, 3), font=("Segoe UI", 9))
        st.map("Subtle.TButton", background=[("active", t["btn"])], foreground=[("active", t["fg"]), ("disabled", t["muted"])])
        st.configure("SubtleWarn.TButton", background=t["bg"], foreground=t["warn"], padding=(8, 3), font=("Segoe UI Semibold", 9))
        st.map("SubtleWarn.TButton", background=[("active", t["btn"])], foreground=[("disabled", t["muted"])])
        st.configure("Treeview", background=t["panel"], fieldbackground=t["panel"], foreground=t["fg"], rowheight=31,
                     borderwidth=0, relief="flat")
        st.map("Treeview", background=[("selected", t["sel"])], foreground=[("selected", t["fg"])])
        st.configure("Treeview.Heading", background=t["head"], foreground=t["muted"], relief="groove", borderwidth=1,
                     bordercolor=t["border"], lightcolor=t["border"], darkcolor=t["border"],
                     font=("Segoe UI Semibold", 9), padding=(10, 8))
        st.map("Treeview.Heading", background=[("active", t["btn_hover"])])
        st.configure("TNotebook", background=t["bg"], borderwidth=0, tabmargins=(0, 0, 0, 0))
        st.configure("TNotebook.Tab", background=t["btn"], foreground=t["muted"], padding=(16, 7), borderwidth=0,
                     font=("Segoe UI Semibold", 9))
        st.map("TNotebook.Tab", background=[("selected", t["panel2"])], foreground=[("selected", t["fg"])])
        for w in ("TCheckbutton", "TRadiobutton"):
            st.configure(w, background=t["bg"], foreground=t["fg"], indicatorbackground=t["field"],
                         indicatorforeground="#ffffff")
            st.map(w, background=[("active", t["bg"])], indicatorbackground=[("selected", t["accent"]), ("active", t["btn_hover"])])
        st.configure("TCombobox", fieldbackground=t["field"], background=t["btn"], foreground=t["fg"], arrowcolor=t["fg"],
                     bordercolor=t["border"], selectbackground=t["field"], selectforeground=t["fg"])
        st.map("TCombobox", fieldbackground=[("readonly", t["field"])], foreground=[("readonly", t["fg"])],
               background=[("active", t["btn_hover"])])
        st.configure("TEntry", fieldbackground=t["field"], foreground=t["fg"], insertcolor=t["fg"], bordercolor=t["border"])
        st.configure("TSpinbox", fieldbackground=t["field"], foreground=t["fg"], arrowcolor=t["fg"], background=t["btn"],
                     insertcolor=t["fg"])
        st.configure("TScrollbar", background=t["btn"], troughcolor=t["panel"], arrowcolor=t["muted"], bordercolor=t["panel"],
                     relief="flat")
        st.map("TScrollbar", background=[("active", t["btn_hover"])])
        st.configure("TSeparator", background=t["border"])
        st.configure("TPanedwindow", background=t["bg"])
        st.configure("Accent.Horizontal.TProgressbar", background=t["accent"], troughcolor=t["trough"], thickness=12,
                     borderwidth=0, lightcolor=t["accent"], darkcolor=t["accent"])
        st.configure("Slim.Horizontal.TProgressbar", background=t["g2"], troughcolor=t["trough"], thickness=5,
                     borderwidth=0, lightcolor=t["g2"], darkcolor=t["g2"])

    def style_widgets(self):
        """Colors for the plain-tk widgets (they don't follow ttk styles)."""
        t = self.t
        self.root.configure(bg=t["bg"])
        self.logbox.configure(bg=t["panel"], fg=t["fg"], insertbackground=t["fg"], relief="flat", bd=0, padx=10, pady=8,
                              highlightthickness=1, highlightbackground=t["border"], highlightcolor=t["border"],
                              selectbackground=t["sel"])
        for tag in ("info", "cmd", "ok", "err", "warn"):
            self.logbox.tag_configure(tag, foreground=t[tag])
        for tag, key in (("done", "ok"), ("fail", "err"), ("skip", "skip"), ("work", "work")):
            self.tree.tag_configure(tag, foreground=t[key])
        self.tree.tag_configure("even", background=t["panel"])
        self.tree.tag_configure("odd", background=t["panel2"])
        self.sugg_text.configure(bg=t["panel2"], fg=t["fg"], relief="flat", bd=0, padx=12, pady=10, highlightthickness=0,
                                 selectbackground=t["sel"], font=("Segoe UI", 9))
        for tag, col, font in (("muted", t["muted"], None), ("key", t["fg"], ("Segoe UI Semibold", 10)),
                               ("val", t["g2"] if self.v["theme"].get() == "dark" else t["accent"], ("Segoe UI Semibold", 10)),
                               ("warn", t["warn"], ("Segoe UI Semibold", 9)), ("note", t["fg"], None),
                               ("ok", t["ok"], ("Segoe UI Semibold", 9))):
            self.sugg_text.tag_configure(tag, foreground=col)
            if font:
                self.sugg_text.tag_configure(tag, font=font)
        self.settings_win.configure(bg=t["bg"])
        self.more_menu.configure(bg=t["panel"], fg=t["fg"], activebackground=t["accent"], activeforeground="#ffffff",
                                 bd=0, relief="flat")
        self.menu.configure(bg=t["panel"], fg=t["fg"], activebackground=t["accent"], activeforeground="#ffffff",
                            bd=0, relief="flat")
        self.draw_header()

    def make_icon(self):
        img = tk.PhotoImage(width=32, height=32)
        for y in range(32):
            for x in range(32):
                d = ((x - 15.5) ** 2 + (y - 15.5) ** 2) ** 0.5
                if d <= 15:
                    col = "#10121a" if d <= 3.5 else "#ffffff" if d <= 5 else lerp_hex("#7c5cff", "#22d3ee", (x + y) / 62)
                    img.put(col, (x, y))
        self.root.iconphoto(True, img)
        self._icon = img

    def build_header(self):
        self.hdr = tk.Canvas(self.root, height=62, highlightthickness=0, bd=0)
        self.hdr.pack(fill="x")
        self.hdr_state, self.spin_angle, self.arcs = "idle", 0, []
        self.hdr.bind("<Configure>", lambda e: self.draw_header())

    def draw_header(self):
        c, t = self.hdr, self.t
        w, h = max(c.winfo_width(), 400), 62
        c.configure(bg=t["bg"])
        c.delete("all")
        n = max(1, w // 5)
        for i in range(n):
            c.create_rectangle(int(i * w / n), 0, int((i + 1) * w / n) + 1, h,
                               fill=lerp_hex(t["g1"], t["g2"], i / n), outline="")
        cx, cy, r = 40, 31, 20
        base = lerp_hex(t["g1"], t["g2"], cx / w)
        c.create_oval(cx - r - 3, cy - r - 3, cx + r + 3, cy + r + 3, fill=lerp_hex(base, "#000000", 0.25), outline="")
        c.create_oval(cx - r, cy - r, cx + r, cy + r, fill="#f4f6ff", outline="")
        self.arcs = [c.create_arc(cx - r + 3, cy - r + 3, cx + r - 3, cy + r - 3, start=self.spin_angle + k * 90,
                                  extent=42, style="pieslice", fill=lerp_hex("#f4f6ff", base, 0.45), outline="")
                     for k in range(4)]
        c.create_oval(cx - 7, cy - 7, cx + 7, cy + 7, fill=base, outline="#ffffff", width=2)
        c.create_text(76, 23, text="CHD Manager", anchor="w", fill="#ffffff", font=("Segoe UI Semibold", 17))
        c.create_text(78, 47, text="Convert game disc images between formats  ·  CD / DVD detected for you", anchor="w",
                      fill="#eef0ff", font=("Segoe UI", 9))
        label, col = {"idle": ("●  Ready", "#ffffff"), "run": ("●  Working…", "#fff1b8"),
                      "done": ("✔  Finished", "#c9ffe4"), "fail": ("●  Finished with errors", "#ffd4d4")}[self.hdr_state]
        c.create_text(w - 20, 24, text=label, anchor="e", fill=col, font=("Segoe UI Semibold", 11))
        if self.app_update_available():
            nv = ".".join(str(x) for x in ver_tuple(self.app_latest["tag"]))
            c.create_text(w - 20, 46, text=f"▲  Update {nv} available - click", anchor="e", fill="#fff1b8",
                          font=("Segoe UI Semibold", 10), tags="updbadge")
            c.tag_bind("updbadge", "<Button-1>", lambda e: self.confirm_app_update())
            c.tag_bind("updbadge", "<Enter>", lambda e: c.configure(cursor="hand2"))
            c.tag_bind("updbadge", "<Leave>", lambda e: c.configure(cursor=""))

    def set_state(self, state):
        self.hdr_state = state
        self.draw_header()
        if state == "run":
            self.spin_tick()
        elif state in ("done", "fail"):
            self.root.after(9000, lambda: self.set_state("idle") if self.hdr_state in ("done", "fail") else None)

    def spin_tick(self):
        if self.hdr_state != "run":
            return
        self.spin_angle = (self.spin_angle + 16) % 360
        try:
            for k, item in enumerate(self.arcs):
                self.hdr.itemconfigure(item, start=self.spin_angle + k * 90)
            self.root.after(45, self.spin_tick)
        except tk.TclError:
            pass

    def toggle_theme(self):
        new = "light" if self.v["theme"].get() == "dark" else "dark"
        self.v["theme"].set(new)
        self.t = THEMES[new]
        self.more_menu.entryconfigure(self.theme_menu_index, label=self.theme_menu_label())
        self.apply_ttk_theme()
        self.style_widgets()
        self.restripe()

    def tags_for(self, iid, job):
        tag = ("done" if job.status.startswith("Done") else
               "fail" if job.status.startswith(("Failed", "Error", "Missing", "Verify failed", "No CUE")) else
               "skip" if job.status.startswith(("Skipped", "Stopped")) else
               "work" if job.status not in ("Queued",) else "")
        stripe = "odd" if self.tree.index(iid) % 2 else "even"
        return (tag, stripe) if tag else (stripe,)

    def restripe(self):
        for iid, job in self.tree_ids.items():
            self.tree.item(iid, tags=self.tags_for(iid, job))

    # ------------------------------------------------------------------ smart suggestions
    def refresh_suggestions(self, first_add=False):
        o = self.options()
        hint = None
        if o["out_mode"] == "custom" and o["out_dir"]:
            hint = Path(o["out_dir"])
            while not hint.exists() and hint != hint.parent:
                hint = hint.parent
        self.sugg = build_suggestions(self.jobs, hint)
        self.render_suggestions()
        if self.v["auto_suggest"].get():
            has_convert = any(j.kind == "convert" for j in self.jobs)
            self.apply_suggestions(quiet=True, hardware_only=not has_convert)   # PC-specific settings apply at startup
            if first_add and has_convert:
                self.refresh_summary()

    def render_suggestions(self):
        s, tx = self.sugg, self.sugg_text
        tx.configure(state="normal")
        tx.delete("1.0", "end")
        tx.insert("end", s["summary"] + "\n\n", "muted")
        for key, val, why in s["rows"]:
            tx.insert("end", f"{key}:  ", "key")
            tx.insert("end", val + "\n", "val")
            tx.insert("end", why + "\n\n", "muted")
        for w in s["warnings"]:
            tx.insert("end", "⚠  " + w + "\n\n", "warn")
        for n in s["notes"]:
            tx.insert("end", "•  " + n + "\n", "note")
        auto = self.v["auto_suggest"].get() and any(j.kind == "convert" for j in self.jobs)
        if auto:
            tx.insert("end", "\n✔ These settings were applied (see the settings line under the list).", "ok")
        tx.configure(state="disabled")

    def apply_suggestions(self, quiet=False, hardware_only=False):
        if not self.sugg:
            self.refresh_suggestions()
        for k, val in self.sugg["values"].items():
            if hardware_only and k not in ("threads", "low_priority"):
                continue
            self.v[k].set(val)
        if not quiet:
            self.status.config(text="Suggested settings applied.")
            self.log("Suggested settings applied: compression Default, hunk default, "
                     f"threads {self.sugg['values']['threads'] or 'all'}, low priority {'on' if self.sugg['values']['low_priority'] else 'off'}, skip existing, verify on.", "info")
        self.render_suggestions()

    # ------------------------------------------------------------------ drag and drop (native Windows)
    def enable_file_drop(self):
        """Let files/folders be dragged from Explorer onto the window. Uses the Windows shell API directly."""
        if os.name != "nt":
            return
        try:
            self.root.update_idletasks()
            user32, shell32 = ctypes.windll.user32, ctypes.windll.shell32
            hwnd = user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            WM_DROPFILES, GWLP_WNDPROC = 0x0233, -4
            WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
            user32.GetWindowLongPtrW.restype = ctypes.c_void_p
            user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.SetWindowLongPtrW.restype = ctypes.c_void_p
            user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
            user32.CallWindowProcW.restype = ctypes.c_ssize_t
            user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
            shell32.DragQueryFileW.restype = wintypes.UINT
            shell32.DragFinish.argtypes = [wintypes.HANDLE]
            old_proc = user32.GetWindowLongPtrW(hwnd, GWLP_WNDPROC)

            def proc(h, msg, wparam, lparam):
                if msg == WM_DROPFILES:
                    paths = []
                    try:
                        count = shell32.DragQueryFileW(wparam, 0xFFFFFFFF, None, 0)
                        for i in range(count):
                            n = shell32.DragQueryFileW(wparam, i, None, 0)
                            buf = ctypes.create_unicode_buffer(n + 1)
                            shell32.DragQueryFileW(wparam, i, buf, n + 1)
                            paths.append(buf.value)
                    finally:
                        shell32.DragFinish(wparam)
                    if paths:
                        self.root.after(10, lambda: self.on_files_dropped(paths))
                    return 0
                return user32.CallWindowProcW(old_proc, h, msg, wparam, lparam)

            self._drop_proc = WNDPROC(proc)            # keep a reference or Python frees the callback
            user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, ctypes.cast(self._drop_proc, ctypes.c_void_p).value)
            shell32.DragAcceptFiles(hwnd, True)
            for msg in (WM_DROPFILES, 0x0049, 0x004A):   # also accept drops when the sender runs at another privilege level
                user32.ChangeWindowMessageFilterEx(hwnd, msg, 1, None)
            self.drop_hwnd = hwnd
        except Exception as e:  # noqa: BLE001
            self.log(f"Drag and drop is unavailable: {e}", "warn")

    def on_files_dropped(self, paths):
        self.log(f"Dropped {len(paths)} item(s).", "info")
        self.add_paths(paths)

    # ------------------------------------------------------------------ layout helpers (menu, settings window, log, summary)
    def footer_base(self, which):
        if which == "chdman":
            return f"chdman {self.chd_version}" if CHDMAN else "chdman not found"
        return f"CHD Manager {APP_VER}"

    def set_footer(self, which, note="", tone="muted", revert_after=0):
        """Each footer button shows its component and version; a short note (checking, up to date, update available)
        is appended and can clear itself after a few seconds."""
        btn = self.chdman_btn if which == "chdman" else self.app_btn
        self._footer_note[which] = note
        btn.config(text=self.footer_base(which) + (f"  ·  {note}" if note else ""),
                   style="SubtleWarn.TButton" if tone == "warn" else "Subtle.TButton")
        self.root.after(50, self.fit_toolbar)
        if revert_after:
            self.root.after(revert_after, lambda: self.set_footer(which) if self._footer_note.get(which) == note else None)

    def app_footer_clicked(self):
        if self.running:
            self.status.config(text="Wait for the current jobs to finish before updating CHD Manager.")
            return
        if self.app_update_available():
            self.confirm_app_update()
        else:
            self.check_app_update_async(True)

    # ------------------------------------------------------------------ what each file becomes (its Action drop-down)
    def refresh_chd_controls(self):
        self.root.after(50, self.fit_toolbar)

    def fmt_for(self, job, o=None):
        """How the CHD extraction code names this row's target."""
        return TARGET_FMT.get((job.target or "").partition("_d")[0], "auto")

    def action_choices(self, job):
        """[(label, target key)] offered in the Action drop-down of one row: only what can work for that item."""
        return [(label, key) for key, label in job.available_targets()]

    def pick_action(self, job, choice):
        job.set_target(choice[1])
        self.close_picker()
        self.update_action_labels()
        self.update_status_idle()

    GENERIC_TARGETS = (("chd", "→ CHD"), ("cue", "→ CUE/BIN"), ("iso", "→ ISO"), ("pbp", "→ PlayStation PBP"),
                       ("cdi", "→ DiscJuggler CDI"), ("cso", "→ CSO"), ("zso", "→ ZSO"), ("ecm", "→ ECM"),
                       ("rvz", "→ RVZ (Wii / GameCube)"), ("wbfs", "→ WBFS (Wii)"), ("gcz", "→ GCZ"), ("wux", "→ WUX (Wii U)"), ("wud", "→ WUD (Wii U)"), ("verify", "Verify (CHD files)"),
                       ("recompress", "Recompress (CHD files)"))

    def set_all_targets(self, key, label):
        """From the Action heading: give every file that can do it the same target; the rest keep their own."""
        done = 0
        for j in self.jobs:
            have = [k for k, _ in j.available_targets()]
            pick = next((k for k in (("cue", "cue_split") if key == "cue" else (key,)) if k in have), None)
            if pick and not j.status.startswith("Done"):
                j.set_target(pick)
                done += 1
        self.update_action_labels()
        self.update_status_idle()
        self.status.config(text=f"{done} of {len(self.jobs)} item(s) set to “{label}”; the others can't do that and keep their own choice.")

    def close_picker(self):
        pk = getattr(self, "picker", None)
        self.picker = None
        if pk is not None:
            try:
                pk.destroy()
            except tk.TclError:
                pass

    # ---- column widths: sized to their text, until the user drags a divider (double-click a divider to fit it again)
    def tree_font(self):
        from tkinter import font as tkfont
        if getattr(self, "_tf", None) is None:
            self._tf = tkfont.Font(font=ttk.Style().lookup("Treeview", "font") or "TkDefaultFont")
        return self._tf

    def autofit_columns(self, only=None):
        cols = self.tree["displaycolumns"]
        avail = self.tree.winfo_width()
        if avail < 200 or self.running and only is None:
            return
        f, pad = self.tree_font(), 26
        caps = {"file": 520, "result": 520, "status": 260, "action": 330, "platform": 140, "type": 150, "size": 110}
        floor = {"file": 150, "result": 120, "status": 110, "action": 110, "platform": 80, "type": 70, "size": 70}
        want = {}
        for c in cols:
            texts = [self.tree.heading(c, "text")] + [self.tree.set(i, c) for i in self.tree.get_children()]
            want[c] = min(caps[c], max(f.measure(t) for t in texts) + pad)
        free = [c for c in ("file", "result") if c not in self.manual_cols]
        over = sum(want[c] if c not in self.manual_cols else self.tree.column(c, "width") for c in cols) - avail
        for c in sorted(free, key=lambda c: -want[c]):        # too wide for the window: trim the two long columns first
            if over > 0:
                cut = min(over, want[c] - floor[c])
                want[c] -= cut
                over -= cut
        for c in cols:
            if c in self.manual_cols and only != c:
                continue
            self.manual_cols.discard(c)
            self.auto_w[c] = want[c]
            self.tree.column(c, width=want[c])

    def on_tree_press(self, e):
        self.close_picker()
        region = self.tree.identify_region(e.x, e.y)
        if region == "heading" and self.tree.identify_column(e.x) == "#2" and self.jobs and not self.running:
            m = tk.Menu(self.root, tearoff=0)
            m.add_command(label="Set every file that can, to:", state="disabled")
            for key, label in self.GENERIC_TARGETS:
                m.add_command(label=label, command=lambda k=key, lab=label: self.set_all_targets(k, lab))
            m.tk_popup(e.x_root, e.y_root)
        self.sep_drag = region == "separator"
        self.press_w = {c: self.tree.column(c, "width") for c in self.tree["displaycolumns"]}

    def on_tree_double(self, e):
        region = self.tree.identify_region(e.x, e.y)
        if region == "separator":
            col = self.tree.identify_column(e.x)
            self.manual_cols.discard(self.tree["displaycolumns"][int(col[1:]) - 1])
            self.autofit_columns(only=self.tree["displaycolumns"][int(col[1:]) - 1])
        elif not (region == "cell" and self.tree.identify_column(e.x) == "#2"):
            self.info_selected()

    def note_manual_resize(self):
        """After a divider drag, remember which columns the user sized themselves."""
        for c in self.tree["displaycolumns"]:
            if self.tree.column(c, "width") != self.press_w.get(c):
                self.manual_cols.add(c)

    def hide_tip(self):
        if self.tip_after:
            self.root.after_cancel(self.tip_after)
            self.tip_after = None
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None
        self.tip_cell = None

    def on_tree_motion(self, e):
        cell = (self.tree.identify_row(e.y), self.tree.identify_column(e.x)) if self.tree.identify_region(e.x, e.y) == "cell" else None
        if cell == self.tip_cell:
            return
        self.hide_tip()
        self.tip_cell = cell
        if cell and cell[0]:
            self.tip_after = self.root.after(500, lambda: self.show_tip(cell, e.x_root, e.y_root))

    def show_tip(self, cell, x, y):
        iid, col = cell
        name = self.tree["displaycolumns"][int(col[1:]) - 1]
        text = self.tree.set(iid, name)
        if not text or self.tree_font().measure(text) + 14 <= self.tree.column(name, "width"):
            return
        if name == "file":
            job = self.tree_ids.get(iid)
            text = str(job.path) if job else text
        self.tip = tk.Toplevel(self.root)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x + 14}+{y + 18}")
        tk.Label(self.tip, text=text, bg=self.t["field"], fg=self.t["fg"], relief="solid", borderwidth=1, padx=6, pady=3,
                 wraplength=700, justify="left", font=("Segoe UI", 9)).pack()

    def on_tree_release(self, e):
        if getattr(self, "sep_drag", False):
            self.sep_drag = False
            self.note_manual_resize()
            return
        """A click on a row's Action cell opens a drop-down with every choice that fits that item."""
        if self.running or self.tree.identify_region(e.x, e.y) != "cell" or self.tree.identify_column(e.x) != "#2" or e.state & 0x5:
            return
        iid = self.tree.identify_row(e.y)
        job = self.tree_ids.get(iid)
        if job is None or not job.can_pick or job.status.startswith("Done"):
            return
        box = self.tree.bbox(iid, "action")
        if not box:
            return
        self.close_picker()
        choices = self.action_choices(job)
        cb = ttk.Combobox(self.tree, state="readonly", values=[c[0] for c in choices])
        cb.place(x=box[0], y=box[1], width=max(box[2], 290), height=box[3])
        cb.bind("<<ComboboxSelected>>", lambda ev: self.pick_action(job, choices[cb.current()]))
        cb.set(job.action_label().replace("  ▾", ""))
        cb.bind("<Escape>", lambda ev: self.close_picker())
        self.picker = cb
        cb.focus_set()
        try:
            cb.tk.call("ttk::combobox::Post", str(cb))
        except tk.TclError:
            pass

    def wants_split(self, job, o=None):
        """One BIN per track: chosen in the Action drop-down."""
        fmt = self.fmt_for(job, o)
        return fmt == "split" or (fmt == "auto" and job.tracks > 1 and job.target != "cue_single")

    def action_text_for(self, job):
        if job.target == "chd" and job.disc_count > 1:
            return f"→ {job.disc_count} CHDs"
        return target_label(job.target or "") or "Extract"

    def update_action_labels(self):
        for j in self.jobs:
            j.action_text = self.action_text_for(j)
            self.refresh_job(j)
        self.refresh_chd_controls()
        self.root.after_idle(self.autofit_columns)

    def plan_text(self):
        c = {}
        for j in self.jobs:
            lab = target_label(j.target or "")
            c[lab] = c.get(lab, 0) + 1
        return "  ·  ".join(f"{n} {lab}" for lab, n in c.items())

    def theme_menu_label(self):
        return "Switch to the light theme" if self.v["theme"].get() == "dark" else "Switch to the dark theme"

    def show_more_menu(self):
        self.more_menu.update_idletasks()
        x = self.more_btn.winfo_rootx() + self.more_btn.winfo_width() - self.more_menu.winfo_reqwidth()
        self.more_menu.tk_popup(max(x, 0), self.more_btn.winfo_rooty() + self.more_btn.winfo_height())

    def open_settings(self, tab="Convert"):
        w = self.settings_win
        if not getattr(self, "_settings_placed", False):
            w.update_idletasks()        # on a scaled display the pages are bigger than 780x700: fit the tallest one
            ww = min(max(780, w.winfo_reqwidth() + 8), w.winfo_screenwidth() - 60)
            hh = min(max(700, w.winfo_reqheight() + 8), w.winfo_screenheight() - 90)
            w.geometry(f"{ww}x{hh}+{max(self.root.winfo_rootx() + 80, 0)}+{max(self.root.winfo_rooty() + 40, 0)}")
            self._settings_placed = True
        w.deiconify()
        w.lift()
        names = {self.nb.tab(i, "text"): i for i in range(self.nb.index("end"))}
        if tab in names:
            self.nb.select(names[tab])

    def show_log(self, show):
        shown = str(self.logf) in self.pw.panes()
        if show and not shown:
            self.pw.add(self.logf, weight=1)
        elif not show and shown:
            self.pw.forget(self.logf)
        self.log_btn.config(text="Hide log" if show else "Show log")

    def toggle_log(self):
        self.show_log(str(self.logf) not in self.pw.panes())

    def summary_text(self):
        o = self.options()
        comp = o["compression"] if o["compression"] != "Custom" else f"custom ({o['custom_comp'].strip() or 'default'})"
        parts = [f"{comp.lower()} compression" if o["compression"] != "Default" else "default compression",
                 f"{o['threads']} threads" if o["threads"] else "all threads"]
        if str(o["hunk"]).strip():
            parts.append(f"hunk {str(o['hunk']).strip()}")
        parts.append("verify after" if o["verify_after"] else "no verify")
        parts.append({"Skip": "skip existing", "Overwrite": "overwrite existing", "Rename": "rename if exists"}.get(o["exists"], "skip existing"))
        parts.append("saved next to the originals" if o["out_mode"] == "same" or not o["out_dir"]
                     else f"saved to {Path(o['out_dir']).name or o['out_dir']}")
        if o["low_priority"]:
            parts.append("low priority")
        if o["delete_src"]:
            parts.append("delete originals for good after verify" if o["delete_forever"] else "recycle originals after verify")
        lead = "Suggested settings:  " if o["auto_suggest"] else "Settings:  "
        return lead + "  ·  ".join(parts)

    def refresh_summary(self):
        try:
            self.summary_label.config(text=self.summary_text())
        except (tk.TclError, AttributeError):
            pass

    def fit_toolbar(self):
        """Keep the window wide enough that no toolbar button is squeezed (the label text changes at runtime)."""
        try:
            self.root.update_idletasks()
            need = self.toolbar.winfo_reqwidth() + 24
            self.root.minsize(max(900, need), 580)
            if self.root.winfo_width() < need:
                self.root.geometry(f"{need}x{self.root.winfo_height()}")
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ settings
    def save_settings(self):
        if self.no_save:
            return
        d = {k: var.get() for k, var in self.v.items()}
        d["chdman"] = self.chd_path_var.get() if hasattr(self, "chd_path_var") else ""
        d["last_dir"] = self.settings.get("last_dir", "")
        d["app_dir"] = str(HERE)                  # lets the next start notice that the program folder was moved
        try:
            d["geometry"] = self.root.geometry()
        except tk.TclError:
            d["geometry"] = ""
        try:
            SETTINGS_FILE.write_text(json.dumps(d, indent=2), "utf-8")
        except OSError:
            pass

    def on_close(self):
        if self.running:
            if not self.ask("A job is running. Stop it and quit?"):
                return
            self.stop()
        self.save_settings()
        self.drop_all_arcs()
        self.root.destroy()

    def ask(self, msg):
        return getattr(self, "silent_answer", True) if self.silent else messagebox.askyesno(APP_NAME, msg)

    def on_delete_toggle(self):
        if self.v["delete_src"].get() and not self.silent:
            messagebox.showinfo(APP_NAME, "Source files will go to the Recycle Bin (so you can restore them), "
                                          "and only after the new CHD has been created AND verified. "
                                          "Verification is turned on automatically.")
            self.v["verify_after"].set(True)

    def on_forever_toggle(self):
        """Ticking 'delete permanently' asks once; it also switches on the verified-delete option it belongs to."""
        if not self.v["delete_forever"].get():
            return
        if not self.silent and not messagebox.askyesno(
                APP_NAME, "Permanently delete the source files after each CHD is created and verified?\n\n"
                          "They will NOT go to the Recycle Bin and cannot be restored. A source is only deleted once its "
                          "new CHD has passed verification; a CHD that can't be verified never causes a delete.",
                icon="warning", default="no"):
            self.v["delete_forever"].set(False)
            return
        self.v["delete_src"].set(True)
        self.v["verify_after"].set(True)

    def pick_out_dir(self):
        d = filedialog.askdirectory(title="Output folder")
        if d:
            self.v["out_dir"].set(d)
            self.v["out_mode"].set("custom")

    def pick_chdman(self):
        global CHDMAN
        p = filedialog.askopenfilename(title="Select chdman.exe", filetypes=[("chdman", "chdman.exe"), ("All", "*.*")])
        if p:
            CHDMAN = p
            self.chd_path_var.set(p)
            self.chd_version, self.has_dvd = chdman_info()
            self.set_footer("chdman")
            self.root.after(50, self.fit_toolbar)
            self.log(f"Using {p} (chdman {self.chd_version})", "info")

    # ------------------------------------------------------------------ queue mgmt
    def add_files_dialog(self):
        files = filedialog.askopenfilenames(
            title="Select disc images or CHD files", initialdir=self.settings.get("last_dir") or None,
            filetypes=[("Disc images / CHD", "*.cue *.gdi *.iso *.toc *.bin *.pbp *.cdi *.mds *.nrg *.ccd *.img *.m3u *.chd"), ("All files", "*.*")])
        if files:
            self.settings["last_dir"] = str(Path(files[0]).parent)
            self.add_paths(files)

    def add_folder_dialog(self):
        d = filedialog.askdirectory(title="Add a folder (searched recursively)",
                                    initialdir=self.settings.get("last_dir") or None)
        if d:
            self.settings["last_dir"] = d
            self.add_paths([d])

    def add_paths(self, paths):
        found = []   # (path, root)
        for p in map(Path, paths):
            if p.is_dir():
                files, ignored = self.scan_folder(p)
                found += [(f, p) for f in files]
                if ignored:
                    self.log(f"Ignored {sum(ignored.values())} file(s) ({', '.join(sorted(ignored))}): keep an .mdf next to its .mds.", "warn")
            elif p.suffix.lower() in ARCHIVE_EXTS:
                found.append((p, None))
            elif p.suffix.lower() in SRC_EXTS | {".chd"}:
                found.append((image_primary(p), None))
            else:
                self.log(f"Skipped (unsupported type): {p.name}", "warn")
        jobs_from = []   # (file, root, archive, folder it was unpacked into)
        known = {str(j.archive).lower() for j in self.jobs if j.archive}
        for f, root in found:
            if f.suffix.lower() in ARCHIVE_EXTS:
                if str(f).lower() in known:
                    continue
                files, tmp = self.open_archive(f)
                jobs_from += [(x, None, f, tmp) for x in files]
            else:
                jobs_from.append((f, root, None, None))
        have = {str(j.path).lower() for j in self.jobs}
        was_empty = not self.jobs
        added = 0
        for f, root, arc, tmp in jobs_from:
            if str(f).lower() in have:
                continue
            try:
                job = Job(f, root)
                job.analyze()
            except Exception as e:  # noqa: BLE001
                self.log(f"Could not read {f.name}: {e}", "err")
                continue
            if arc:
                job.archive, job.extract_dir, job.home, job.src_files = arc, tmp, arc.parent / f.name, [arc]
                self.arc_dirs[tmp] += 1
            have.add(str(f).lower())
            self.jobs.append(job)
            iid = self.tree.insert("", "end", values=job.row(), tags=("odd" if len(self.tree_ids) % 2 else "even",))
            self.tree_ids[iid] = job
            added += 1
            if job.missing:
                self.log(f"{job.path.name}: missing referenced file(s): {', '.join(job.missing)}", "warn")
        self.refresh_empty()
        self.log(f"Added {added} item(s).", "info")
        self.update_action_labels()
        self.update_status_idle()
        if added:
            self.log("Click the Action cell of a file to choose what it becomes; click the Action heading to set them all.", "info")
        if added:
            self.refresh_suggestions(first_add=was_empty)

    def refresh_empty(self):
        if self.jobs:
            self.empty.place_forget()
        else:
            self.empty.place(relx=0.5, rely=0.45, anchor="center")

    def selected_jobs(self):
        return [self.tree_ids[i] for i in self.tree.selection() if i in self.tree_ids]

    def remove_selected(self):
        if self.running:
            return
        for iid in self.tree.selection():
            job = self.tree_ids.pop(iid, None)
            if job in self.jobs:
                self.jobs.remove(job)
                self.drop_arc(job)
            self.tree.delete(iid)
        self.restripe()
        self.refresh_empty()
        self.update_status_idle()
        self.refresh_suggestions()
        self.refresh_chd_controls()
        self.root.after_idle(self.autofit_columns)

    def clear_all(self):
        if self.running:
            return
        self.tree.delete(*self.tree.get_children())
        self.tree_ids.clear()
        self.jobs.clear()
        self.drop_all_arcs()
        self.refresh_empty()
        self.update_status_idle()
        self.refresh_suggestions()
        self.refresh_chd_controls()

    def update_status_idle(self):
        if not self.running:
            n = len(self.jobs)
            total = sum(j.size for j in self.jobs)
            plan = self.plan_text()
            self.status.config(text=(f"{n} item(s), {fmt_size(total)}" + (f"   ·   {plan}" if plan else "")) if n else "Ready")

    def set_override(self, val):
        for j in self.selected_jobs():
            if j.kind == "convert":
                j.override = val
                self.refresh_job(j)

    def on_right_click(self, e):
        iid = self.tree.identify_row(e.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        if self.tree.selection():
            self.menu.tk_popup(e.x_root, e.y_root)

    def open_selected_folder(self):
        for j in self.selected_jobs()[:1]:
            target = j.out_path or j.path
            subprocess.Popen(["explorer", "/select,", str(target)])

    def open_output_folder(self):
        o = self.options()
        if o["out_mode"] == "custom" and o["out_dir"] and Path(o["out_dir"]).is_dir():
            os.startfile(o["out_dir"])
        else:
            for j in (self.selected_jobs() or self.jobs)[:1]:
                os.startfile(str((j.out_path or j.path).parent))

    def refresh_job(self, job):
        for iid, j in self.tree_ids.items():
            if j is job:
                self.tree.item(iid, values=job.row(), tags=self.tags_for(iid, job))
                break

    # ------------------------------------------------------------------ log
    def log(self, text, tag="info"):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", text.rstrip("\n") + "\n", tag)
        self.logbox.see("end")
        self.logbox.configure(state="disabled")
        if self.echo:
            self.echo(text.rstrip("\n"), tag)

    def copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.logbox.get("1.0", "end"))

    def save_log(self):
        p = filedialog.asksaveasfilename(defaultextension=".txt", initialfile="chd_manager_log.txt")
        if p:
            Path(p).write_text(self.logbox.get("1.0", "end"), "utf-8")

    def clear_log(self):
        self.logbox.configure(state="normal")
        self.logbox.delete("1.0", "end")
        self.logbox.configure(state="disabled")

    # ------------------------------------------------------------------ run control
    def options(self):
        o = {k: var.get() for k, var in self.v.items()}
        try:
            o["threads"] = int(o["threads"])
        except (ValueError, tk.TclError):
            o["threads"] = 0
        o["base_pbp_file"] = str(find_base_pbp(o.get("base_pbp", "")) or "")
        try:
            o["pbp_level"] = min(9, max(0, int(o["pbp_level"])))
        except (ValueError, tk.TclError):
            o["pbp_level"] = 6
        for k, d in (("rvz_level", 5), ("rvz_chunk", 131072)):
            try:
                o[k] = int(o[k])
            except (ValueError, tk.TclError):
                o[k] = d
        return o

    BASE_PBP_TEXT = ("A PBP needs a small loader program from Sony to run on a real PSP or Vita. CHD Manager doesn't include it, "
                     "but can download a generic PBP that has one from the public GitHub repository julianxhokaxhiu/iPoPS "
                     "(two files, about 1.4 MB, each checked against a fixed checksum) and keep it next to CHD Manager.exe as BASE.PBP and DATA.PSP. "
                     "It is only used to copy the loader, icon and boot logo into the PBPs you make. "
                     "You can also choose any working PlayStation 1 .pbp of your own in Settings, Formats.")

    def pbp_jobs(self, todo):
        return any(j.target == "pbp" for j in todo)

    def offer_base_pbp(self):
        """Before making PBPs without a base: offer the download once. False = don't start."""
        if self.silent or not self.v["base_pbp_ask"].get() or find_base_pbp(self.v["base_pbp"].get()):
            return True
        ans = messagebox.askyesnocancel(
            APP_NAME, self.BASE_PBP_TEXT + "\n\nYes: download it now.\nNo: make PBPs without it (they work in emulators and "
            "frontends, not on a PSP/Vita) and don't ask again.\nCancel: don't start yet.")
        if ans is None:
            return False
        if ans:
            self.download_base_pbp_ui(confirm=False)
        else:
            self.v["base_pbp_ask"].set(False)
        return True

    def download_base_pbp_ui(self, confirm=True):
        if self.silent or (confirm and not messagebox.askyesno(APP_NAME, self.BASE_PBP_TEXT + "\n\nDownload it now?")):
            return False
        self.status.config(text="Downloading the base PBP…")
        self.root.update_idletasks()
        try:
            download_base_pbp(HERE)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"Could not get the base PBP: {e}\n\nYou can still pick a PBP of your own in Settings.")
            self.update_status_idle()
            return False
        self.log(f"Saved BASE.PBP and DATA.PSP next to the program ({HERE}).", "ok")
        if (HERE / "BASE.PBP").is_file():
            self.v["base_pbp"].set(str(HERE / "BASE.PBP"))          # select it, so nothing has to be browsed for
            self.save_settings()
        self.update_status_idle()
        return True

    # ---- archives: unpacked into a hidden folder when added, and removed again when the file is done with
    def scan_folder(self, p, archives=True):
        """The disc files in a folder tree -> ([paths], {ignored extension: count})."""
        found, ignored = [], {}
        allf = [Path(dp) / fn for dp, _, fns in os.walk(p) for fn in fns]
        referenced = set()
        for f in allf:
            e = f.suffix.lower()
            refs = parse_cue(f) if e == ".cue" else parse_gdi(f) if e == ".gdi" else parse_toc(f) if e == ".toc" else []
            referenced.update(str(r).lower() for r in refs)
        for f in sorted(allf):
            e = f.suffix.lower()
            if e == ".chd" or e in (".cue", ".gdi", ".toc", ".iso", ".pbp", ".cdi", ".mds", ".nrg", ".ccd", ".cso", ".zso", ".ecm",
                                    ".gcm", ".gcz", ".rvz", ".wia", ".wbfs", ".ciso", ".wux", ".wud"):
                found.append(f)
            elif e == ".bin" and str(f).lower() not in referenced:
                found.append(f)
            elif e == ".img" and not f.with_suffix(".ccd").exists() and not f.with_suffix(".mds").exists():
                found.append(f)
            elif e == ".mdf" and not f.with_suffix(".mds").exists():
                ignored[e] = ignored.get(e, 0) + 1
            elif e in ARCHIVE_EXTS and archives:
                found.append(f)
        return found, ignored

    def pumped(self, fn):
        """Run fn in a thread while the window keeps answering; returns the exception it raised, or None."""
        box = {}

        def work():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                box["err"] = e
        t = threading.Thread(target=work, daemon=True)
        t.start()
        while t.is_alive():
            self.root.update()
            time.sleep(0.03)
        return box.get("err")

    def open_archive(self, arc):
        """Unpack an archive next to itself in a hidden folder -> ([the disc files inside], that folder or None)."""
        import gzip
        import tarfile
        import zipfile
        name, ext = arc.name.lower(), arc.suffix.lower()
        try:
            tmp = Path(tempfile.mkdtemp(prefix=".chdm_arc_", dir=str(arc.parent)))
        except OSError:
            tmp = Path(tempfile.mkdtemp(prefix="chdm_arc_"))
        self.status.config(text=f"Unpacking {arc.name}…")
        self.root.update_idletasks()

        def run():
            if ext == ".zip":
                with zipfile.ZipFile(arc) as z:
                    z.extractall(tmp)
            elif ext in (".tar", ".tgz") or name.endswith((".tar.gz", ".tar.bz2", ".tar.xz")):
                with tarfile.open(arc) as t:
                    t.extractall(tmp, filter="data")
            elif ext == ".gz":
                with gzip.open(arc) as src, open(tmp / arc.stem, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
            else:
                z = find_7z()
                if not z:
                    raise RuntimeError("7-Zip is needed for this kind of archive (Settings, Output page, or install it from 7-zip.org)")
                r = subprocess.run([z, "x", "-y", f"-o{tmp}", str(arc), "-bso0", "-bsp0"], capture_output=True,
                                   creationflags=CREATE_NO_WINDOW)
                if r.returncode:
                    raise RuntimeError((r.stderr or r.stdout).decode("utf-8", "replace").strip()[-300:] or f"7-Zip exited with {r.returncode}")
        if ext in (".7z", ".rar") and not find_7z() and not self.silent and not self.ensure_7zip():
            shutil.rmtree(tmp, ignore_errors=True)
            return [], None
        err = self.pumped(run)
        if err:
            self.log(f"Could not unpack {arc.name}: {err}", "err")
            shutil.rmtree(tmp, ignore_errors=True)
            return [], None
        files, _ = self.scan_folder(tmp, archives=False)
        if not files:
            self.log(f"{arc.name}: no disc images found inside.", "warn")
            shutil.rmtree(tmp, ignore_errors=True)
            return [], None
        self.arc_dirs[tmp] = 0
        return files, tmp

    def drop_arc(self, job):
        """A job that came out of an archive is finished with: delete the unpacked folder once no other job uses it."""
        d = getattr(job, "extract_dir", None)
        if d is None or d not in self.arc_dirs:
            return
        job.extract_dir = None
        self.arc_dirs[d] -= 1
        if self.arc_dirs[d] <= 0:
            del self.arc_dirs[d]
            shutil.rmtree(d, ignore_errors=True)

    def drop_all_arcs(self):
        for d in list(self.arc_dirs):
            shutil.rmtree(d, ignore_errors=True)
        self.arc_dirs.clear()

    def download_gamedb_ui(self):
        if self.silent or not messagebox.askyesno(APP_NAME, "To name files after the game's title, region or disc code, CHD Manager "
                "needs a list of PlayStation game codes and titles: gameInfo.db (about 1 MB) from the public PSXPackager project on "
                "GitHub (MIT license). It is checked against a fixed checksum and kept next to CHD Manager.\n\nDownload it now?"):
            return
        self.status.config(text="Downloading the game list…")
        self.root.update_idletasks()
        try:
            download_gamedb(HERE)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"Could not get the game list: {e}")
        else:
            self.log(f"Saved gameInfo.db next to the program ({HERE}).", "ok")
        self.update_status_idle()

    def pick_art_dir(self):
        d = filedialog.askdirectory(title="Folder with one subfolder of artwork per game")
        if d:
            self.v["art_dir"].set(d)

    def pick_base_pbp(self):
        p = filedialog.askopenfilename(title="Choose a working PlayStation 1 .pbp",
                                       filetypes=[("PBP files", "*.pbp"), ("All files", "*.*")])
        if not p:
            return
        try:
            read_base_pbp(p)
        except (UnpackError, OSError) as e:
            messagebox.showerror(APP_NAME, f"That file can't be used as a base PBP: {e}")
            return
        self.v["base_pbp"].set(p)

    def start(self, only=None):
        if self.running:
            return
        if not CHDMAN:
            if not self.silent:
                messagebox.showerror(APP_NAME, "chdman.exe was not found. Set it in Settings, Output page.")
            return
        todo = [j for j in (only if only is not None else self.jobs) if not j.status.startswith("Done")]
        if not todo:
            self.status.config(text="Nothing to do - add files first.")
            return
        if self.pbp_jobs(todo) and not self.offer_base_pbp():
            return
        for j in todo:
            j.status, j.result = "Queued", ""
            self.refresh_job(j)
        self.save_settings()
        self.log_mark = self.logbox.index("end-1c")
        self.running, self.stop_flag = True, False
        self.set_state("run")
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.all_bar["value"] = self.cur_bar["value"] = 0
        o = self.options()
        self.worker = threading.Thread(target=self.run_all, args=(todo, o), daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_flag = True
        p = self.proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass

    def run_blocking(self, jobs=None, timeout=600):
        """Used by the self-test: start, pump the UI until finished."""
        self.start(jobs)
        end = time.time() + timeout
        while self.running and time.time() < end:
            self.root.update()
            time.sleep(0.02)
        self.root.update()
        return not self.running

    # ------------------------------------------------------------------ worker thread
    def post(self, *a):
        self.q.put(a)

    def run_all(self, todo, o):
        self.cur_opts = o
        t0 = time.time()
        stats = {"ok": 0, "fail": 0, "skip": 0, "src": 0, "out": 0}
        total = len(todo)
        for i, job in enumerate(todo):
            if self.stop_flag:
                job.status = "Stopped"
                self.post("job", job)
                continue
            self.post("overall", i, total)
            self.post("label", f"[{i + 1}/{total}] {job.path.name}")
            try:
                why = self.space_check(job, o)
            except Exception:  # noqa: BLE001  (a fault in the estimate must never block a conversion)
                why = None
            if why:
                job.status, job.result = "Failed (disk space)", why
                self.post("log", f"{job.path.name}: {why}", "err")
                self.post("job", job)
                stats["fail"] += 1
                continue
            job.status = "Working…"
            self.post("job", job)
            started = time.time()
            try:
                {"convert": self.do_convert, "extract": self.do_extract, "verify": self.do_verify,
                 "recompress": self.do_recompress}[job.kind](job, o)
            except Exception:  # noqa: BLE001
                job.status = "Error"
                self.post("log", traceback.format_exc(), "err")
            took = fmt_time(time.time() - started)
            if job.status.startswith("Done"):
                stats["ok"] += 1
                if job.kind in ("convert", "recompress"):
                    stats["src"] += job.size
                    stats["out"] += job.out_size
                    if job.size:
                        pct = 100 * job.out_size / job.size
                        job.result = f"{fmt_size(job.size)} → {fmt_size(job.out_size)} ({pct:.0f}%) · {took}"
                else:
                    job.result = (job.result + " · " if job.result else "") + took
            elif job.status.startswith(("Skipped", "Stopped")):
                stats["skip"] += 1
            else:
                stats["fail"] += 1
            self.post("job", job)
        self.post("overall", total, total)
        saved = stats["src"] - stats["out"]
        msg = (f"Finished in {fmt_time(time.time() - t0)}: {stats['ok']} done, {stats['skip']} skipped, "
               f"{stats['fail']} failed.")
        if stats["src"]:
            msg += f"  Converted {fmt_size(stats['src'])} → {fmt_size(stats['out'])}, saved {fmt_size(saved)} ({100 * saved / stats['src']:.0f}%)."
        self.post("finish", msg, stats["fail"] == 0)

    def out_stem(self, job, o):
        """The output file's name: the source's, or built from the game ID and title when Settings, Formats says so."""
        fmt = (o.get("name_format") or "%FILENAME%").strip()
        stem = job.home.stem
        if fmt == "%FILENAME%":
            return stem
        gid = job.game_id
        dbf = find_gamedb()
        ent = load_gamedb(dbf).get(gid) if gid and dbf else None
        if ent is None and job.container == "disc" and gid:        # GameCube / Wii: the name stored on the disc
            di = job.disc_info
            ent = dict(title=di["title"], main_title=di["title"], main_id=gid,
                       region={"E": "USA", "P": "Europe", "J": "Japan", "K": "Korea"}.get(gid[3:4], ""))
        vals = {"%FILENAME%": stem, "%GAMEID%": gid, "%TITLE%": ent["title"] if ent else "",
                "%MAINTITLE%": ent["main_title"] if ent else "", "%MAINGAMEID%": ent["main_id"] if ent else "",
                "%REGION%": ent["region"] if ent else ""}
        if any(k in fmt and not v for k, v in vals.items()):
            if not job.name_warned:
                job.name_warned = True
                why = "no game ID could be read from it" if not gid else "gameInfo.db has no entry for it (Settings, Formats, Get game titles)"
                self.post("log", f"{job.path.name}: kept the original file name, since {why}.", "warn")
            return stem
        for k, v in vals.items():
            fmt = fmt.replace(k, v)
        return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", fmt).strip(" .") or stem

    def output_path(self, job, o, ext, multi=False, make=True):
        stem = self.out_stem(job, o)
        if o["out_mode"] == "custom" and o["out_dir"]:
            base = Path(o["out_dir"])
            if o["keep_structure"] and job.root:
                try:
                    base = base / job.home.parent.relative_to(job.root)
                except ValueError:
                    pass
        else:
            base = job.home.parent
        if job.kind == "extract" and (multi or o["extract_subfolder"]):
            base = base / stem
        if make:
            base.mkdir(parents=True, exist_ok=True)
        return base / (stem + ext)

    def space_needs(self, job, o):
        """[(folder, bytes)] this job will occupy at its peak: the finished file plus any temporary files, which are
        always written next to it. The ECM check also unpacks to the Windows temp folder."""
        t = (job.target or "").partition("_d")[0]
        if t == "verify":
            return []
        raw = job.raw_size()
        ext = job.path.suffix.lower()
        final = job.size if t == "recompress" else int(raw * SPACE_RATIO.get(t, 1.0))
        extra = 0
        if job.container == "disc":
            if t == "wbfs" and job.platform == "Wii" and o["wbfs_scrub"] and not WII_KEYS[0]:
                extra = raw                                  # the scrubbed ISO Dolphin makes first
        elif ext == ".chd":
            if t in ("cso", "zso", "ecm", "pbp", "cdi", "cue_fix") or (t == "iso" and job.disc == "CD"):
                extra = raw                                  # extracted to a temporary folder first
        elif job.kind == "convert":
            extra = raw if job.container else 0              # PBP / CDI / images are unpacked first
        else:
            direct = (t == "cue" and job.container in ("pbp", "cdi", "image", "ecm")) or \
                     (t in ("cso", "zso") and (job.container == "cso" or (ext == ".iso" and not job.wrap_cue))) or \
                     (t == "iso" and job.container == "cso") or t in ("cue_fix", "art")
            if not direct:                                   # through a temporary uncompressed CHD
                extra = raw + (raw if job.container else 0)
        out = self.output_path(job, o, SPACE_EXT.get(t, ".x"), make=False).parent
        needs = [(out, final + extra)]
        if t == "ecm" and o["verify_after"]:
            needs.append((Path(tempfile.gettempdir()), raw))
        return needs

    def space_check(self, job, o):
        """None when the job fits; otherwise a plain sentence saying how much is needed and how much is free."""
        needs = self.space_needs(job, o)
        t = (job.target or "").partition("_d")[0]
        ext = SPACE_EXT.get(t)
        if ext and o["exists"] == "Skip" and self.output_path(job, o, ext, make=False).exists():
            return None                                      # it will be skipped, so it needs no room
        drives = {}
        for folder, n in needs:
            near, free = free_space(folder)
            d = drives.setdefault(os.stat(near).st_dev, [near, free, 0])
            d[2] += n
        for near, free, n in drives.values():
            if n > 0 and free < n * 1.03 + SPACE_MARGIN:
                bin_note = " Files in the Recycle Bin still use space until it is emptied." if o["delete_src"] and not o["delete_forever"] else ""
                return (f"Not enough free space on {near.anchor or near}: about {fmt_size(n)} is needed there, "
                        f"but only {fmt_size(free)} is free. Free up some room or choose another Output folder." + bin_note)
        return None

    def handle_exists(self, job, out, o):
        """-> (out, force, skip)"""
        if not out.exists():
            return out, False, False
        mode = o["exists"]
        if mode == "Skip":
            job.status = "Skipped (exists)"
            job.result = f"{out.name} already exists (see 'If output exists' in Settings)"
            self.post("log", f"Skipped {job.path.name}: {out.name} already exists.", "warn")
            return out, False, True
        if mode == "Rename":
            n = 1
            while True:
                cand = out.with_name(f"{out.stem} ({n}){out.suffix}")
                if not cand.exists():
                    return cand, False, False
                n += 1
        return out, True, False

    def comp_args(self, o, typ):
        c = o["compression"]
        if c == "None":
            return ["-c", "none"]
        if c == "Fast":
            return ["-c", "cdzl" if typ == "CD" else "zlib"]
        if c == "Smallest":
            return ["-c", "cdlz" if typ == "CD" else "lzma"]
        if c == "Custom" and o["custom_comp"].strip():
            return ["-c", o["custom_comp"].strip()]
        return []

    def common_args(self, o, typ):
        a = self.comp_args(o, typ)
        h = str(o["hunk"]).strip()
        if h:
            if h.isdigit():
                a += ["-hs", h]
            else:
                self.post("log", f"Ignoring hunk size '{h}' (must be a number).", "warn")
        if o["threads"] > 0:
            a += ["-np", str(o["threads"])]
        return a

    def unpack_job(self, job, base, outdir, tag, prog, iso_ok=False):
        """One disc of a .pbp / .cdi / disc image -> CUE/BIN files in outdir. Returns the .cue (or .iso) path."""
        stopped = lambda: self.stop_flag  # noqa: E731
        if job.container == "pbp":
            return unpack_pbp_disc(job.path, base, outdir, tag, prog, stopped)
        if job.container == "image":
            return unpack_tracks(read_image(job.path), outdir, tag, prog, stopped, iso_ok=iso_ok)
        if job.container == "cso":
            iso = Path(outdir) / f"{tag}.iso"
            cso_to_iso(job.path, iso, prog, stopped)
            return iso
        if job.container == "ecm":
            out_bin = Path(outdir) / f"{tag}.bin"
            ttype = ecm_decode(job.path, out_bin, prog, stopped) or "MODE1/2352"
            cue = Path(outdir) / f"{tag}.cue"
            cue.write_text(f'FILE "{out_bin.name}" BINARY\n  TRACK 01 {ttype}\n    INDEX 01 00:00:00\n', "utf-8")
            return cue
        return unpack_cdi(job.path, outdir, tag, prog, stopped)

    def do_unpack(self, job, o):
        """.pbp / .cdi / .mds / .nrg / .ccd / .img -> CUE/BIN files in their own folder, no CHD."""
        n = job.disc_count
        stem = self.output_path(job, o, ".cue").stem
        out = self.output_path(job, o, ".cue").parent / stem
        if out.exists() and any(out.iterdir()):
            if o["exists"] == "Skip":
                job.status, job.result = "Skipped (exists)", f"{out.name} already exists (see 'If output exists' in Settings)"
                self.post("log", f"Skipped {job.path.name}: folder {out.name} already exists.", "warn")
                return
            if o["exists"] == "Rename":
                k = 1
                while out.with_name(f"{stem} ({k})").exists():
                    k += 1
                out = out.with_name(f"{stem} ({k})")
        created = not out.exists()
        out.mkdir(parents=True, exist_ok=True)
        bases = pbp_discs(job.path) if job.container == "pbp" else [None]
        cues = []
        try:
            for i in ([job.disc_pick - 1] if job.disc_pick else range(n)):
                label = f" disc {i + 1} of {n}" if n > 1 else ""
                last = [-1]

                def prog(x, label=label):
                    pct = int(100 * x)
                    if pct != last[0]:
                        last[0] = pct
                        job.status = f"Unpacking{label} {pct}%"
                        self.post("job", job)
                        self.post("progress", pct)

                self.post("log", f"\n{job.path.name}{label}  [unpacking {job.path.suffix[1:].upper()}]", "info")
                cues.append(self.unpack_job(job, bases[i], out, f"{stem} (Disc {i + 1})" if n > 1 else stem, prog))
        except UnpackError as e:
            if self.stop_flag:
                job.status = "Stopped"
            else:
                job.status, job.result = "Failed (can't unpack)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
        except OSError as e:
            job.status, job.result = "Failed (disk)", str(e)
            self.post("log", f"{job.path.name}: {e}", "err")
        else:
            job.out_path = cues[0]
            job.out_size = sum(f.stat().st_size for f in out.iterdir() if f.is_file())
            job.status, job.result = "Done", f"→ {out.name}\\"
            self.post("log", f"OK  {out}", "ok")
        finally:
            if created and not job.status.startswith("Done"):
                shutil.rmtree(out, ignore_errors=True)

    def do_convert_container(self, job, o):
        """.pbp / .cdi -> unpack to CUE/BIN in a temporary folder, then chdman createcd. A multi-disc PBP
        becomes one CHD per disc plus an .m3u playlist."""
        n = job.disc_count
        first = self.output_path(job, o, ".chd")
        stem = first.stem
        discs = [job.disc_pick - 1] if job.disc_pick else list(range(n))
        outs = [first] if n == 1 else [first.with_name(f"{stem} (Disc {i + 1}).chd") for i in discs]
        tmp = first.parent / f".{stem[:40]}.chdm_tmp"
        made, skipped, unverified = [], 0, False
        try:
            bases = pbp_discs(job.path) if job.container == "pbp" else [None]
            for i, out in zip(discs, outs):
                label = f" disc {i + 1} of {n}" if n > 1 else ""
                out, force, skip = self.handle_exists(job, out, o)
                if skip:
                    skipped += 1
                    continue
                shutil.rmtree(tmp, ignore_errors=True)
                tmp.mkdir(parents=True, exist_ok=True)
                last = [-1]

                def prog(x, label=label):
                    pct = int(100 * x)
                    if pct != last[0]:
                        last[0] = pct
                        job.status = f"Unpacking{label} {pct}%"
                        self.post("job", job)
                        self.post("progress", pct)

                tag = f"{stem} (Disc {i + 1})" if n > 1 else stem
                self.post("log", f"\n{job.path.name}{label}  [unpacking {job.path.suffix[1:].upper()}]", "info")
                try:
                    cue = self.unpack_job(job, bases[i], tmp, tag, prog, iso_ok=job.type == "DVD" and self.has_dvd)
                except UnpackError as e:
                    if self.stop_flag:
                        job.status = "Stopped"
                    else:
                        job.status, job.result = "Failed (can't unpack)", str(e)
                        self.post("log", f"{job.path.name}: {e}", "err")
                    return
                except OSError as e:
                    job.status, job.result = "Failed (disk)", str(e)
                    self.post("log", f"{job.path.name}: {e}", "err")
                    return
                iso = cue.suffix.lower() == ".iso"
                cmd = [CHDMAN, "createdvd" if iso else "createcd", "-i", str(cue), "-o", str(out)] + (["-f"] if force else []) \
                    + self.common_args(o, "DVD" if iso else "CD")
                job.out_path = out
                self.post("log", "$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), "cmd")
                rc = self.run_proc(cmd, job, "Compressing")
                if self.stop_flag or rc != 0:
                    job.status = "Stopped" if self.stop_flag else f"Failed (code {rc})"
                    if out.exists():
                        try:
                            out.unlink()
                        except OSError:
                            pass
                    return
                if o["verify_after"] or o["delete_src"]:
                    self.post("log", "Verifying…", "info")
                    _, ok = self.verify_file(out, job, "Verifying")
                    if ok is False:
                        job.status = "Verify failed"
                        self.post("log", f"VERIFY FAILED for {out.name} - the source file was NOT touched.", "err")
                        return
                    unverified = unverified or ok is None
                made.append(out)
                self.post("log", f"OK  {out.name}", "ok")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if not made:
            return                                  # everything was skipped; handle_exists already said why
        if n > 1 and len(made) == n and not job.disc_pick:
            first.with_name(stem + ".m3u").write_text("".join(p.name + "\n" for p in made), "utf-8")
        job.out_size = sum(p.stat().st_size for p in made)
        job.status = "Done"
        job.result = f"→ {made[0].name}" if len(made) == 1 else f"→ {len(made)} CHDs" + (" + .m3u" if len(made) == n else "")
        if skipped:
            job.result += f" ({skipped} already existed)"
        self.post("log", f"OK  {job.path.name}  ({fmt_size(job.size)} → {fmt_size(job.out_size)})", "ok")
        if o["delete_src"] and not skipped and not unverified:
            self.remove_sources(job, o)
        elif o["delete_src"]:
            self.post("log", "The source file was kept (not everything could be verified, or part of it already existed).", "warn")

    def do_convert(self, job, o):
        if job.container:
            return self.do_convert_container(job, o)
        if job.missing:
            job.status = "Failed (missing files)"
            self.post("log", f"{job.path.name}: missing {', '.join(job.missing)}", "err")
            return
        typ = job.type
        src, temp_cue = job.path, None
        if job.lone_bin or job.wrap_cue:
            if not job.wrap_cue and not o["auto_cue"]:
                job.status = "No CUE"
                self.post("log", f"{job.path.name}: .bin without a CUE (enable auto-create in Settings, Convert page).", "err")
                return
            mode = job.wrap_cue or "MODE2/2352"
            temp_cue = job.path.with_name(job.path.stem + ".__chdm_auto__.cue")
            temp_cue.write_text(f'FILE "{job.path.name}" BINARY\n  TRACK 01 {mode}\n    INDEX 01 00:00:00\n', "utf-8")
            src = temp_cue
            typ = "CD"
            self.post("log", f"{job.path.name}: created a temporary CUE ({mode}).", "warn" if job.lone_bin and not job.wrap_cue else "info")
        try:
            out = self.output_path(job, o, ".chd")
            out, force, skip = self.handle_exists(job, out, o)
            if skip:
                return
            verb = "createcd"
            if typ == "DVD":
                if self.has_dvd:
                    verb = "createdvd"
                else:
                    self.post("log", "chdman has no createdvd - using createcd instead.", "warn")
            cmd = [CHDMAN, verb, "-i", str(src), "-o", str(out)] + (["-f"] if force else []) + self.common_args(o, typ)
            job.out_path = out
            self.post("log", f"\n{job.path.name}  [{typ}{' ' + job.platform if job.platform else ''}]", "info")
            self.post("log", "$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), "cmd")
            rc = self.run_proc(cmd, job, "Compressing")
        finally:
            if temp_cue and temp_cue.exists():
                try:
                    temp_cue.unlink()
                except OSError:
                    pass
        if self.stop_flag or rc != 0:
            job.status = "Stopped" if self.stop_flag else f"Failed (code {rc})"
            if out.exists():
                try:
                    out.unlink()
                except OSError:
                    pass
            return
        job.out_size = out.stat().st_size
        unverified = False
        if o["verify_after"] or o["delete_src"]:
            self.post("log", "Verifying…", "info")
            vrc, ok = self.verify_file(out, job, "Verifying")
            if ok is False:
                job.status = "Verify failed"
                self.post("log", f"VERIFY FAILED for {out.name} - source files were NOT touched.", "err")
                return
            unverified = ok is None
        job.status = "Done"
        self.post("log", f"OK  {out.name}  ({fmt_size(job.size)} → {fmt_size(job.out_size)})", "ok")
        if o["delete_src"] and unverified:
            self.post("log", "Source files were kept: an uncompressed CHD can't be verified, so nothing was recycled.", "warn")
        elif o["delete_src"]:
            self.remove_sources(job, o)

    def remove_sources(self, job, o):
        """After a verified CHD: send the source files to the Recycle Bin, or delete them for good when the user chose that."""
        if o.get("delete_forever"):
            if delete_forever(job.src_files):
                self.post("log", f"Deleted {len(job.src_files)} source file(s) permanently; the space is free again.", "ok")
            else:
                self.post("log", "Could not delete every source file.", "warn")
        elif recycle(job.src_files):
            self.post("log", f"Sent {len(job.src_files)} source file(s) to the Recycle Bin.", "ok")
        else:
            self.post("log", "Could not recycle the source files.", "warn")

    def verify_file(self, path, job, label):
        lines = []
        rc = self.run_proc([CHDMAN, "verify", "-i", str(path)], job, label, capture=lines)
        text = "\n".join(lines).lower()
        if "uncompressed" in text and "no verification" in text:
            self.post("log", "This CHD is uncompressed, so chdman has no checksum to verify it against.", "warn")
            return rc, None
        ok = rc == 0 and "verification successful" in text and "does not match" not in text
        if ok:
            self.post("log", "Verification successful (SHA-1 matches).", "ok")
        return rc, ok

    def do_verify(self, job, o):
        self.post("log", f"\nVerify {job.path.name}", "info")
        _, ok = self.verify_file(job.path, job, "Verifying")
        job.status = "Done" if ok is not False else "Verify failed"
        job.result = "uncompressed: nothing to verify" if ok is None else "SHA-1 OK" if ok else "CORRUPT or unreadable"
        if ok is False:
            self.post("log", f"{job.path.name} FAILED verification.", "err")

    def do_extract(self, job, o):
        if job.path.suffix.lower() not in (".chd", ".m3u"):
            return self.export_other(job, o)
        if job.disc in ("", "?"):
            job.disc, job.tracks = chd_info(job.path)
        kind = job.disc
        ext, verb, extra = {"CD": (".cue", "extractcd", True), "GD-ROM": (".gdi", "extractcd", True),
                            "DVD": (".iso", "extractdvd", False), "HD": (".img", "extracthd", False),
                            "LD": (".avi", "extractld", False), "Raw": (".bin", "extractraw", False)}.get(
            kind, (None, None, False))
        if not verb:
            job.status = "Failed (unknown CHD)"
            self.post("log", f"{job.path.name}: cannot read this CHD.", "err")
            return
        fmt = self.fmt_for(job, o)
        if fmt in ("cso", "zso"):
            return self.extract_to_cso(job, o, lz4=fmt == "zso")
        if fmt == "ecm":
            return self.extract_to_ecm(job, o)
        if fmt == "fix":
            return self.do_fix(job, o)
        if fmt == "pbp" or job.path.suffix.lower() == ".m3u":
            if fmt != "pbp":
                job.status, job.result = "Failed (choose PBP)", "A playlist can only be combined into a PBP: choose PlayStation PBP under 'CHD files'."
                self.post("log", f"{job.path.name}: {job.result}", "err")
                return
            if kind != "CD":
                job.status, job.result = "Failed (not possible)", f"PBP is only possible for PlayStation 1 CDs; this CHD holds a {kind} disc."
                self.post("log", f"{job.path.name}: {job.result}", "err")
                return
            return self.extract_to_pbp(job, o)
        if fmt == "cdi" and kind == "CD":
            return self.extract_to_cdi(job, o)
        if fmt == "cdi":
            job.status, job.result = "Failed (not possible)", f"CDI is only possible for CD discs; this CHD holds a {kind} disc."
            self.post("log", f"{job.path.name}: {job.result}", "err")
            return
        if fmt == "iso" and kind == "CD":
            return self.extract_to_iso(job, o)
        if fmt == "iso" and kind != "DVD":
            job.status = "Failed (not possible)"
            job.result = f"ISO is only possible for CD and DVD discs; this CHD holds a {kind} disc."
            self.post("log", f"{job.path.name}: {job.result}", "err")
            return
        split = verb == "extractcd" and kind == "CD" and self.wants_split(job, o)
        # a disc that comes out as several files (a .cue with several BINs, or a .gdi with its tracks) gets its own folder
        out = self.output_path(job, o, ext, multi=split or kind == "GD-ROM")
        out, force, skip = self.handle_exists(job, out, o)
        if skip:
            return
        cmd = [CHDMAN, verb, "-i", str(job.path), "-o", str(out)] + (["-f"] if force else [])
        if split:
            cmd += ["-sb"]
            self.post("log", f"{job.tracks} tracks: writing one BIN per track.", "info")
        job.out_path = out
        self.post("log", f"\n{job.path.name}  [{kind}]", "info")
        self.post("log", "$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), "cmd")
        rc = self.run_proc(cmd, job, "Extracting")
        if self.stop_flag or rc != 0:
            job.status = "Stopped" if self.stop_flag else f"Failed (code {rc})"
            return
        job.status = "Done"
        job.result = f"→ {out.name}"
        self.post("log", f"OK  {out}", "ok")

    # ---- sources other than a CHD that become something other than a CHD
    def export_other(self, job, o):
        """A disc image / PBP / CDI / CSO whose target isn't a CHD. Where a direct route exists it is used; every other
        combination goes through a temporary uncompressed CHD, so each target is written by the same code whatever the source."""
        t = (job.target or "").partition("_d")[0]
        if t == "art":
            return self.do_extract_art(job, o)
        if t == "cue_fix":
            return self.do_fix(job, o)
        if job.container == "disc":
            return self.do_disc(job, o)
        if t == "cue" and job.container in ("pbp", "cdi", "image", "ecm"):
            return self.do_unpack(job, o)
        if t in ("cso", "zso") and job.path.suffix.lower() == ".iso" and not job.wrap_cue:
            return self.do_iso_to_cso(job, o, lz4=t == "zso")
        if t in ("cso", "zso") and job.container == "cso":
            return self.do_cso_convert(job, o, lz4=t == "zso")
        if t == "iso" and job.container == "cso":
            return self.do_cso_to_iso(job, o)
        return self.export_via_chd(job, o)

    def export_via_chd(self, job, o):
        tmp = self.output_path(job, o, ".x").parent / f".{job.home.stem[:40]}.chdm_hub"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        saved = (job.path, job.disc, job.tracks)
        try:
            tchd = tmp / (job.home.stem + ".chd")
            if not self.chd_from_source(job, o, tchd, tmp):
                return
            job.path = tchd
            job.disc, job.tracks = chd_info(tchd)
            self.do_extract(job, o)
        finally:
            job.path, job.disc, job.tracks = saved
            shutil.rmtree(tmp, ignore_errors=True)

    def chd_from_source(self, job, o, tchd, tmp):
        """Any non-CHD source -> an uncompressed CHD at tchd. False (with the job's status set) when that fails."""
        src, typ = job.path, job.type
        if job.missing:
            job.status = "Failed (missing files)"
            self.post("log", f"{job.path.name}: missing {', '.join(job.missing)}", "err")
            return False
        if job.container:
            last = [-1]

            def prog(x):
                pct = int(100 * x)
                if pct != last[0]:
                    last[0] = pct
                    job.status = f"Unpacking {pct}%"
                    self.post("job", job)
                    self.post("progress", pct)

            unp = tmp / "unp"
            unp.mkdir()
            self.post("log", f"\n{job.path.name}  [unpacking {job.path.suffix[1:].upper()}]", "info")
            try:
                base = pbp_discs(job.path)[0] if job.container == "pbp" else None
                src = self.unpack_job(job, base, unp, job.home.stem, prog, iso_ok=True)
            except UnpackError as e:
                if self.stop_flag:
                    job.status = "Stopped"
                else:
                    job.status, job.result = "Failed (can't unpack)", str(e)
                    self.post("log", f"{job.path.name}: {e}", "err")
                return False
            except OSError as e:
                job.status, job.result = "Failed (disk)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
                return False
            typ = "DVD" if src.suffix.lower() == ".iso" else "CD"
        temp_cue = None
        if job.wrap_cue:
            src = temp_cue = job.path.with_name(job.path.stem + ".__chdm_auto__.cue")
            temp_cue.write_text(f'FILE "{job.path.name}" BINARY\n  TRACK 01 {job.wrap_cue}\n    INDEX 01 00:00:00\n', "utf-8")
            typ = "CD"
        elif job.target == "pbp" and src.suffix.lower() == ".iso":
            last = [-1]

            def cprog(x):
                pct = int(100 * x)
                if pct != last[0]:
                    last[0] = pct
                    job.status = f"Rebuilding sectors {pct}%"
                    self.post("job", job)
                    self.post("progress", pct)

            self.post("log", f"{job.path.name}: rebuilding raw CD sectors from the plain ISO (a PBP needs them).", "info")
            raw = tmp / "raw.bin"
            try:
                cook_to_raw(src, raw, cprog, lambda: self.stop_flag)
            except UnpackError:
                job.status = "Stopped"
                return False
            except OSError as e:
                job.status, job.result = "Failed (disk)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
                return False
            src = tmp / "raw.cue"
            src.write_text('FILE "raw.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n', "utf-8")
            typ = "CD"
        cmd = [CHDMAN, "createdvd" if typ == "DVD" and self.has_dvd else "createcd", "-i", str(src), "-o", str(tchd), "-c", "none"]
        self.post("log", "$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), "cmd")
        try:
            rc = self.run_proc(cmd, job, "Preparing")
        finally:
            if temp_cue:
                temp_cue.unlink(missing_ok=True)
        if self.stop_flag or rc != 0:
            job.status = "Stopped" if self.stop_flag else f"Failed (code {rc})"
            return False
        return True

    def cso_progress(self, job, label):
        last = [-1]

        def prog(x):
            pct = int(100 * x)
            if pct != last[0]:
                last[0] = pct
                job.status = f"{label} {pct}%"
                self.post("job", job)
                self.post("progress", pct)
        return prog

    def write_cso_file(self, job, o, iso, part, out, lz4=False):
        """iso -> part (a CSO, or a ZSO), compared against the ISO when verifying is on, then moved to out. True on success."""
        try:
            iso_to_cso(iso, part, self.cso_progress(job, "Compressing"), lambda: self.stop_flag, lz4=lz4)
            if o["verify_after"] and not verify_cso(part, iso, self.cso_progress(job, "Verifying"), lambda: self.stop_flag):
                job.status, job.result = "Verify failed", "the finished CSO didn't match the ISO it was made from; it was discarded."
                self.post("log", f"{job.path.name}: {job.result}", "err")
                return False
        except UnpackError as e:
            if self.stop_flag:
                job.status = "Stopped"
            else:
                job.status, job.result = "Failed (can't write)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
            return False
        except OSError as e:
            job.status, job.result = "Failed (disk)", str(e)
            self.post("log", f"{job.path.name}: {e}", "err")
            return False
        os.replace(part, out)
        job.out_path, job.out_size = out, out.stat().st_size
        job.status, job.result = "Done", f"→ {out.name}"
        self.post("log", f"OK  {out}  ({fmt_size(job.size)} → {fmt_size(job.out_size)})", "ok")
        return True

    def do_iso_to_cso(self, job, o, lz4=False):
        out, _, skip = self.handle_exists(job, self.output_path(job, o, ".zso" if lz4 else ".cso"), o)
        if skip:
            return
        part = out.with_name(out.name + ".part")
        self.post("log", f"\n{job.path.name}  [ISO → {'ZSO' if lz4 else 'CSO'}]", "info")
        try:
            self.write_cso_file(job, o, job.path, part, out, lz4)
        finally:
            part.unlink(missing_ok=True)

    def do_cso_convert(self, job, o, lz4=False):
        """CSO <-> ZSO, through a temporary ISO."""
        out, _, skip = self.handle_exists(job, self.output_path(job, o, ".zso" if lz4 else ".cso"), o)
        if skip:
            return
        tmp = out.parent / f".{out.stem[:40]}.chdm_tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        part = out.with_name(out.name + ".part")
        self.post("log", f"\n{job.path.name}  [{'CSO → ZSO' if lz4 else 'ZSO → CSO'}]", "info")
        try:
            iso = tmp / "t.iso"
            cso_to_iso(job.path, iso, self.cso_progress(job, "Unpacking"), lambda: self.stop_flag)
            self.write_cso_file(job, o, iso, part, out, lz4)
        except UnpackError as e:
            if self.stop_flag:
                job.status = "Stopped"
            else:
                job.status, job.result = "Failed (can't unpack)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
        except OSError as e:
            job.status, job.result = "Failed (disk)", str(e)
            self.post("log", f"{job.path.name}: {e}", "err")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            part.unlink(missing_ok=True)

    def do_cso_to_iso(self, job, o):
        out, _, skip = self.handle_exists(job, self.output_path(job, o, ".iso"), o)
        if skip:
            return
        part = out.with_name(out.name + ".part")
        self.post("log", f"\n{job.path.name}  [CSO → ISO]", "info")
        try:
            cso_to_iso(job.path, part, self.cso_progress(job, "Unpacking"), lambda: self.stop_flag)
            if o["verify_after"] and not verify_cso(job.path, part, self.cso_progress(job, "Verifying"), lambda: self.stop_flag):
                job.status, job.result = "Verify failed", "the unpacked ISO didn't match the CSO; it was discarded."
                self.post("log", f"{job.path.name}: {job.result}", "err")
                return
            os.replace(part, out)
        except UnpackError as e:
            if self.stop_flag:
                job.status = "Stopped"
            else:
                job.status, job.result = "Failed (can't unpack)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
            return
        except OSError as e:
            job.status, job.result = "Failed (disk)", str(e)
            self.post("log", f"{job.path.name}: {e}", "err")
            return
        finally:
            part.unlink(missing_ok=True)
        job.out_path, job.out_size = out, out.stat().st_size
        job.status, job.result = "Done", f"→ {out.name}"
        self.post("log", f"OK  {out}", "ok")

    def extract_to_cso(self, job, o, lz4=False):
        """CHD -> CSO (or ZSO): a DVD CHD or a single-data-track CD CHD, through an ISO."""
        if job.disc != "DVD":
            return self.extract_to_iso(job, o, to_cso=True, lz4=lz4)
        out, _, skip = self.handle_exists(job, self.output_path(job, o, ".zso" if lz4 else ".cso"), o)
        if skip:
            return
        tmp = out.parent / f".{out.stem[:40]}.chdm_tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            iso = tmp / "t.iso"
            cmd = [CHDMAN, "extractdvd", "-i", str(job.path), "-o", str(iso), "-f"]
            self.post("log", f"\n{job.path.name}  [DVD → {'ZSO' if lz4 else 'CSO'}]", "info")
            self.post("log", "$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), "cmd")
            rc = self.run_proc(cmd, job, "Extracting")
            if self.stop_flag or rc != 0:
                job.status = "Stopped" if self.stop_flag else f"Failed (code {rc})"
                return
            self.write_cso_file(job, o, iso, out.with_name(out.name + ".part"), out, lz4)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            out.with_name(out.name + ".part").unlink(missing_ok=True)

    # ---- Wii keys and Dolphin
    def refresh_wii(self):
        load_wii_keys(self.v["wii_common_key"].get(), self.v["wii_korean_key"].get())
        WII_TOOL[0] = find_dolphintool(self.v["dolphintool"].get().strip())
        WII_SCRUB[0] = bool(self.v["wbfs_scrub"].get())
        if hasattr(self, "wii_status"):
            self.wii_status.config(text=self.wii_status_text())
        for j in getattr(self, "jobs", []):
            if j.container == "disc" and not j.status.startswith("Done") and j.target not in [k for k, _ in j.available_targets()]:
                j.set_target(j.default_target())
        if hasattr(self, "tree"):
            self.update_action_labels()

    def wii_status_text(self):
        if not (WII_KEYS[0] or WII_TOOL[0]):
            return "Not set up yet: needs Dolphin, or your Wii key (use the Wii setup button)"
        return "Ready: " + " and ".join(x for x in ("Dolphin found" if WII_TOOL[0] else "", "Wii key found" if WII_KEYS[0] else "") if x)

    def open_keys_folder(self):
        ensure_keys_dir()
        try:
            os.startfile(KEYS_DIR)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Couldn't open {KEYS_DIR}: {e}")

    def themed_dialog(self, title):
        w = tk.Toplevel(self.root)
        w.title(title)
        w.configure(bg=self.t["bg"])
        w.transient(self.root)
        w.resizable(False, False)
        fr = ttk.Frame(w, padding=18)
        fr.pack(fill="both", expand=True)
        return w, fr

    def place_dialog(self, w):
        w.update_idletasks()
        x = self.root.winfo_rootx() + max(20, (self.root.winfo_width() - w.winfo_reqwidth()) // 2)
        y = self.root.winfo_rooty() + max(20, (self.root.winfo_height() - w.winfo_reqheight()) // 3)
        w.geometry(f"+{x}+{y}")

    def show_wii_setup(self):
        """One window for everything Wii: show where Dolphin is, or give your key (pick the file, or paste it)."""
        old = getattr(self, "wii_win", None)
        try:
            if old is not None and old.winfo_exists():
                old.lift()
                return
        except tk.TclError:
            pass
        w, fr = self.themed_dialog("Wii setup")
        self.wii_win = w
        ttk.Label(fr, justify="left", wraplength=470, font=("Segoe UI Semibold", 10),
                  text="To turn a Wii disc into RVZ, WIA or WBFS, CHD Manager needs ONE of these two things:").grid(
            row=0, column=0, sticky="w")
        o1 = ttk.LabelFrame(fr, text=" Option 1: Dolphin (the easy way) ", padding=10)
        o1.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(o1, justify="left", wraplength=450, text="If Dolphin is on this PC, just show CHD Manager where it is. "
                  "That is all it needs; no key is required.").grid(row=0, column=0, columnspan=2, sticky="w")
        d_stat = ttk.Label(o1, text="", wraplength=300, justify="left")
        d_stat.grid(row=1, column=1, sticky="w", padx=(10, 0), pady=(8, 0))
        o2 = ttk.LabelFrame(fr, text=" Option 2: your own Wii key ", padding=10)
        o2.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(o2, justify="left", wraplength=450, text="Choose the file that holds your key, or paste the key "
                  "(32 letters and numbers). CHD Manager contains no keys and never downloads them.").grid(row=0, column=0, columnspan=3, sticky="w")
        k_stat = ttk.Label(o2, text="", wraplength=300, justify="left")
        k_stat.grid(row=1, column=1, columnspan=2, sticky="w", padx=(10, 0), pady=(8, 0))
        pasted = tk.StringVar()
        ttk.Label(o2, text="Or paste the key here:").grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Entry(o2, textvariable=pasted, width=38).grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(fr, justify="left", wraplength=470, style="Muted.TLabel", text="Without either, a Wii disc can still become an ISO, "
                  "GCZ or CISO. GameCube and Wii U discs and every other format never need any of this.").grid(
            row=3, column=0, sticky="w", pady=(10, 0))

        def refresh():
            d_stat.config(text=f"✓ Found: {WII_TOOL[0]}" if WII_TOOL[0] else "Not set yet")
            k_stat.config(text="✓ A key is set" if WII_KEYS[0] else "Not set yet")
            self.wii_status.config(text=self.wii_status_text())

        def find_dolphin():
            p = filedialog.askopenfilename(parent=w, title="Find Dolphin: choose Dolphin.exe (or DolphinTool.exe)",
                                           filetypes=[("Dolphin", "Dolphin.exe DolphinTool.exe"), ("All programs", "*.exe")])
            if not p:
                return
            p = Path(p)
            if p.name.lower() == "dolphin.exe":
                p = p.with_name("DolphinTool.exe")
            if p.name.lower() != "dolphintool.exe" or not p.is_file():
                messagebox.showerror(APP_NAME, "That isn't Dolphin, or this Dolphin has no DolphinTool.exe next to it. "
                                     "Pick Dolphin.exe from the Dolphin folder (a recent Dolphin download).", parent=w)
                return
            self.v["dolphintool"].set(str(p))
            self.save_settings()
            refresh()

        def take_key(data, name=""):
            keys = parse_wii_keys(data, name)
            if not keys[0] and not keys[1]:
                messagebox.showerror(APP_NAME, "No key found there. A key is 32 letters and numbers (0-9 and a-f).", parent=w)
                return
            if keys[0]:
                self.v["wii_common_key"].set(keys[0].hex())
            if keys[1]:
                self.v["wii_korean_key"].set(keys[1].hex())
            self.save_settings()
            pasted.set("")
            refresh()

        def choose_key():
            p = filedialog.askopenfilename(parent=w, title="Choose the file that holds your Wii key",
                                           filetypes=[("Key files", "*.txt *.key *.bin *.hex"), ("All files", "*.*")])
            if not p:
                return
            try:
                if Path(p).stat().st_size > 4096:
                    raise OSError("that file is too big to be a key file")
                take_key(Path(p).read_bytes(), Path(p).name)
            except OSError as e:
                messagebox.showerror(APP_NAME, f"Couldn't read that file: {e}", parent=w)

        ttk.Button(o1, text="Find Dolphin…", style="Accent.TButton", command=find_dolphin).grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Button(o2, text="Choose key file…", style="Accent.TButton", command=choose_key).grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Button(o2, text="Use this key", command=lambda: take_key(pasted.get().strip().encode("ascii", "ignore"))).grid(
            row=3, column=2, padx=(8, 0), pady=(4, 0))
        bf = ttk.Frame(fr)
        bf.grid(row=4, column=0, sticky="e", pady=(14, 0))
        ttk.Button(bf, text="Open the keys folder", command=self.open_keys_folder).pack(side="left", padx=(0, 16))
        ttk.Button(bf, text="Done", command=w.destroy).pack(side="left")
        refresh()
        self.place_dialog(w)

    def wii_warning(self):
        """At start: say that Wii to RVZ/WIA/WBFS is switched off until Dolphin or a key is set up."""
        if WII_KEYS[0] or WII_TOOL[0] or self.v["wii_warn_off"].get():
            return
        w, fr = self.themed_dialog("Wii keys needed")
        ttk.Label(fr, justify="left", wraplength=430, text=(
            "Turning a Wii disc into RVZ, WIA or WBFS needs Dolphin on this PC, or your own Wii key. Neither was found, "
            "so those conversions are switched off for now.\n\n"
            "Everything else works without it: GameCube and Wii U discs and every other format, and Wii discs can still "
            "become ISO, GCZ or CISO.\n\nSet it up now? It takes a moment: just show it where Dolphin is, or give it your key.")
                  ).grid(row=0, column=0, columnspan=2, sticky="w")
        dont = tk.BooleanVar(value=False)
        ttk.Checkbutton(fr, text="Don't show this again", variable=dont).grid(row=1, column=0, sticky="w", pady=(14, 0))

        def close(setup):
            if dont.get():
                self.v["wii_warn_off"].set(True)
                self.save_settings()
            w.destroy()
            if setup:
                self.show_wii_setup()
        bf = ttk.Frame(fr)
        bf.grid(row=1, column=1, sticky="e", pady=(14, 0))
        ttk.Button(bf, text="Not now", command=lambda: close(False)).pack(side="left", padx=4)
        ttk.Button(bf, text="Set up…", style="Accent.TButton", command=lambda: close(True)).pack(side="left")
        w.protocol("WM_DELETE_WINDOW", lambda: close(False))
        self.place_dialog(w)

    def dolphin_convert(self, job, o, t, part, method, scrub=False):
        """Wii disc -> RVZ/WIA (or a scrubbed ISO) with the user's own DolphinTool.exe, used when there is no Wii key. True on success."""
        tool = find_dolphintool(o.get("dolphintool", ""))
        if not tool:
            job.status, job.result = "Failed (setup needed)", "this needs Dolphin or your Wii key (Settings, Formats, Set up…)"
            self.post("log", f"{job.path.name}: {job.result}", "err")
            return False
        udir = Path(tempfile.mkdtemp(prefix="chdm_dolphin_"))
        try:
            cmd = [tool, "convert", "-i", str(job.path), "-o", str(part), "-f", t, "-u", str(udir)]
            if t == "iso":
                cmd += ["-s"] if scrub else []
            else:
                name = {v: k for k, v in self.RVZ_METHODS.items()}[method]
                cmd += ["-b", str(o["rvz_chunk"] if t == "rvz" else 0x200000), "-c", name, "-l", str(o["rvz_level"])]
            self.post("log", "No Wii key, so Dolphin's own tool does the work:", "info")
            self.post("log", "$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), "cmd")
            rc = self.run_proc(cmd, job, "Converting with Dolphin")
        finally:
            shutil.rmtree(udir, ignore_errors=True)
        if self.stop_flag or rc != 0 or not part.exists():
            job.status = "Stopped" if self.stop_flag else f"Failed (code {rc})"
            return False
        return True

    # ---- GameCube / Wii discs (Dolphin's formats)
    DISC_EXT = {"iso": ".iso", "rvz": ".rvz", "wia": ".wia", "gcz": ".gcz", "ciso": ".ciso", "wbfs": ".wbfs", "wud": ".wud",
                "wux": ".wux"}
    RVZ_METHODS = {"none": 0, "bzip2": 2, "lzma": 3, "lzma2": 4, "zstd": 5}

    def do_disc(self, job, o):
        t = (job.target or "").partition("_d")[0]
        out, _, skip = self.handle_exists(job, self.output_path(job, o, self.DISC_EXT[t]), o)
        if skip:
            return
        part = out.with_name(out.name + ".part")
        job.out_path = out
        fmt = job.disc_info.get("format", "ISO")
        self.post("log", f"\n{job.path.name}  [{job.platform}: {fmt} → {t.upper()}]", "info")
        prog = self.cso_progress(job, "Converting")
        stop = lambda: self.stop_flag  # noqa: E731
        src = vsrc = tmpiso = None
        try:
            src = open_disc(job.path)
            if t in ("iso", "wud"):
                disc_to_iso(src, part, prog, stop)
            elif t == "wux":
                write_wux(src, part, prog, stop)
            elif t == "gcz":
                write_gcz(src, part, prog, stop)
            elif t == "ciso":
                write_ciso(src, part, prog, stop)
            elif t == "wbfs" and job.platform == "Wii" and o["wbfs_scrub"] and not WII_KEYS[0]:
                tmpiso = out.with_name(f".{out.stem[:40]}.chdm_scrub.iso")
                if not self.dolphin_convert(job, o, "iso", tmpiso, 0, scrub=True):
                    return
                vsrc = open_disc(tmpiso)
                write_wbfs(vsrc, part, prog, stop, scrub=False)
            elif t == "wbfs":
                write_wbfs(src, part, prog, stop, scrub=bool(o["wbfs_scrub"]))
            else:
                method = self.RVZ_METHODS.get(str(o["rvz_method"]).lower(), 5)
                if t == "wia" and method == 5:
                    self.post("log", "WIA can't use zstd; using LZMA2 instead.", "info")
                if job.platform == "Wii" and not WII_KEYS[0]:
                    if not self.dolphin_convert(job, o, t, part, method):
                        return
                else:
                    write_rvz(src, part, prog, stop, rvz=t == "rvz", method=method, level=o["rvz_level"], chunk=o["rvz_chunk"])
            if o["verify_after"] and t not in ("iso", "wud"):
                if not verify_disc_file(part, vsrc or src, t, self.cso_progress(job, "Verifying"), stop):
                    job.status, job.result = "Verify failed", "the finished file didn't match the disc it was made from; it was discarded."
                    self.post("log", f"{job.path.name}: {job.result}", "err")
                    return
            os.replace(part, out)
        except UnpackError as e:
            if self.stop_flag:
                job.status = "Stopped"
            else:
                job.status, job.result = "Failed (can't convert)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
            return
        except (OSError, struct.error, zlib.error, lzma.LZMAError) as e:
            job.status, job.result = "Failed (disk or file)", str(e)
            self.post("log", f"{job.path.name}: {e}", "err")
            return
        finally:
            for d in (src, vsrc):
                if d:
                    d.close()
            for f in (part, tmpiso):
                if f:
                    f.unlink(missing_ok=True)
        job.out_size = out.stat().st_size
        job.status, job.result = "Done", f"→ {out.name}"
        self.post("log", f"OK  {out}  ({fmt_size(job.size)} → {fmt_size(job.out_size)})", "ok")

    def extract_to_ecm(self, job, o):
        """CD CHD with one raw data track -> ECM."""
        out, _, skip = self.handle_exists(job, self.output_path(job, o, ".ecm"), o)
        if skip:
            return
        tmp = out.parent / f".{out.stem[:40]}.chdm_tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        part = out.with_name(out.name + ".part")
        stop = lambda: self.stop_flag  # noqa: E731
        try:
            cue = tmp / "t.cue"
            cmd = [CHDMAN, "extractcd", "-i", str(job.path), "-o", str(cue), "-f"]
            self.post("log", f"\n{job.path.name}  [CD → ECM]", "info")
            self.post("log", "$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), "cmd")
            rc = self.run_proc(cmd, job, "Extracting")
            if self.stop_flag or rc != 0:
                job.status = "Stopped" if self.stop_flag else f"Failed (code {rc})"
                return
            tracks = parse_cue_tracks(cue)
            if len(tracks) != 1 or tracks[0][1] not in ("MODE1/2352", "MODE2/2352"):
                job.status, job.result = "Failed (can't be an ECM)", "ECM needs one track of raw 2352-byte sectors."
                self.post("log", f"{job.path.name}: {job.result}", "err")
                return
            src_bin = parse_cue(cue)[0]
            ecm_encode(src_bin, part, self.cso_progress(job, "Compressing"), stop)
            if o["verify_after"] and not verify_ecm(part, src_bin, self.cso_progress(job, "Verifying"), stop):
                job.status, job.result = "Verify failed", "the finished ECM didn't decode back to the disc; it was discarded."
                self.post("log", f"{job.path.name}: {job.result}", "err")
                return
            os.replace(part, out)
        except UnpackError as e:
            if self.stop_flag:
                job.status = "Stopped"
            else:
                job.status, job.result = "Failed (can't write)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
            return
        except OSError as e:
            job.status, job.result = "Failed (disk)", str(e)
            self.post("log", f"{job.path.name}: {e}", "err")
            return
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            part.unlink(missing_ok=True)
        job.out_path, job.out_size = out, out.stat().st_size
        job.status, job.result = "Done", f"→ {out.name}"
        self.post("log", f"OK  {out}  ({fmt_size(job.size)} → {fmt_size(job.out_size)})", "ok")

    # ---- artwork for PBPs
    def find_art(self, o, stem, game_id, title):
        """Your artwork for this game, if the artwork folder (Settings, Formats) has a subfolder named after the output file,
        the game ID or the title. Returns a dict for write_pbp, or None."""
        root = o.get("art_dir")
        if not root or not Path(root).is_dir():
            return None
        for name in dict.fromkeys(n for n in (stem, game_id, title) if n):
            art, warn = read_art_folder(Path(root) / name)
            if art:
                self.post("log", f"Using your artwork from {Path(root) / name}: {', '.join(k.upper() for k in art)}.", "info")
                for w in warn:
                    self.post("log", f"Artwork: {w}.", "warn")
                return art
        return None

    def pbp_art(self, o, stem, game_id, title):
        """The artwork for a new PBP: yours where you have it, and for the icon and background you don't have, a plain
        title card with the game's name and code, so the game can be told apart in the PSP menu."""
        art = self.find_art(o, stem, game_id, title) or {}
        if o.get("title_cards"):
            dbf = find_gamedb()
            ent = load_gamedb(dbf).get(game_id) if dbf else None
            shown = (ent["main_title"] or ent["title"]) if ent else title
            made = make_title_art(shown, game_id, art)
            if made:
                self.post("log", f"Drew title cards for the PSP menu ({', '.join(k.upper() for k in made)}): {shown}.", "info")
            art = {**made, **art}
        return art or None

    def do_extract_art(self, job, o):
        """PBP -> its icon, pictures and sound as files (into the artwork folder when one is set, so they are picked up again)."""
        root = o.get("art_dir")
        out = Path(root) / job.home.stem if root and Path(root).is_dir() else self.output_path(job, o, ".x").parent / f"{job.home.stem} (artwork)"
        try:
            made = extract_pbp_art(job.path, out)
        except (UnpackError, OSError) as e:
            job.status, job.result = "Failed (can't read)", str(e)
            self.post("log", f"{job.path.name}: {e}", "err")
            return
        if not made:
            job.status, job.result = "Nothing to extract", "this PBP has no icon, pictures or sound"
            self.post("log", f"{job.path.name}: {job.result}", "warn")
            return
        job.out_path = out
        job.status, job.result = "Done", f"→ {out.name}\\ ({', '.join(made)})"
        self.post("log", f"OK  {out}: {', '.join(made)}", "ok")

    def do_fix(self, job, o):
        """→ CUE/BIN with the EDC/ECC of every Mode 2 Form 1 sector rebuilt (audio, Form 2 and Mode 1 sectors stay as they are)."""
        stem = self.out_stem(job, o)
        tmp = self.output_path(job, o, ".x").parent / f".{stem[:40]}.chdm_fix"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        ext, made = job.path.suffix.lower(), None
        last = [-1]

        def prog(label):
            def f(x):
                pct = int(100 * x)
                if pct != last[0]:
                    last[0] = pct
                    job.status = f"{label} {pct}%"
                    self.post("job", job)
                    self.post("progress", pct)
            return f
        try:
            self.post("log", f"\n{job.path.name}  [rebuilding EDC/ECC]", "info")
            if ext == ".chd":
                cue = tmp / "t.cue"
                cmd = [CHDMAN, "extractcd", "-i", str(job.path), "-o", str(cue), "-f"]
                self.post("log", "$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), "cmd")
                rc = self.run_proc(cmd, job, "Extracting")
                if self.stop_flag or rc != 0:
                    job.status = "Stopped" if self.stop_flag else f"Failed (code {rc})"
                    return
            elif job.container:
                cue = self.unpack_job(job, pbp_discs(job.path)[0] if job.container == "pbp" else None, tmp, "t", prog("Unpacking"))
            elif job.wrap_cue:
                cue = tmp / "t.cue"
                cue.write_text(f'FILE "{job.path}" BINARY\n  TRACK 01 {job.wrap_cue}\n    INDEX 01 00:00:00\n', "utf-8")
            else:
                cue = job.path
            files = list(dict.fromkeys(parse_cue(cue)))
            if not files or not all(f.is_file() for f in files):
                job.status, job.result = "Failed (can't read disc)", "the CUE's data files weren't found"
                self.post("log", f"{job.path.name}: {job.result}", "err")
                return
            multi = len(files) > 1
            out_cue = self.output_path(job, o, ".cue", multi=multi)
            out_cue, force, skip = self.handle_exists(job, out_cue, o)
            if skip:
                return
            names = {f.name.lower(): (f"{out_cue.stem}.bin" if not multi else f"{out_cue.stem} (Track {i + 1}).bin") for i, f in enumerate(files)}
            changed = 0
            part_files = []
            for f in files:
                dst = out_cue.with_name(names[f.name.lower()])
                part_files.append(dst)
                changed += repair_bin(f, dst, prog("Rebuilding"), lambda: self.stop_flag)
            text = cue.read_text("utf-8", "replace")
            text = re.sub(r'(FILE\s+)(?:"([^"]+)"|(\S+))', lambda mt: mt.group(1) + '"' + names.get(Path(mt.group(2) or mt.group(3)).name.lower(), "x.bin") + '"', text, flags=re.I)
            out_cue.write_text(text, "utf-8")
            made = out_cue
            job.out_path, job.out_size = out_cue, sum(p.stat().st_size for p in part_files)
            job.status = "Done"
            job.result = f"→ {out_cue.name}" + (f" ({changed} sectors changed)" if changed else " (nothing needed fixing)")
            self.post("log", f"OK  {out_cue}: {changed} sector(s) changed.", "ok")
        except UnpackError as e:
            if self.stop_flag:
                job.status = "Stopped"
            else:
                job.status, job.result = "Failed (can't read)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
        except OSError as e:
            job.status, job.result = "Failed (disk)", str(e)
            self.post("log", f"{job.path.name}: {e}", "err")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def extract_to_pbp(self, job, o):
        """CD CHD (or an .m3u playlist of CHDs, one per disc) -> one .pbp. PlayStation 1 discs only."""
        is_m3u = job.path.suffix.lower() == ".m3u"
        chds = job.m3u_chds if is_m3u else [job.path]
        n = len(chds)
        out = self.output_path(job, o, ".pbp")
        out, force, skip = self.handle_exists(job, out, o)
        if skip:
            return
        tmp = out.parent / f".{out.stem[:40]}.chdm_tmp"
        part = out.with_name(out.name + ".part")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        job.out_path = out
        last = [-1]

        def prog(label, x):
            pct = int(100 * x)
            if pct != last[0]:
                last[0] = pct
                job.status = f"{label} {pct}%"
                self.post("job", job)
                self.post("progress", pct)

        def fail(status, why):
            job.status, job.result = status, why
            self.post("log", f"{job.path.name}: {why}", "err")

        try:
            self.post("log", f"\n{job.path.name}  [{'playlist, ' + str(n) + ' discs' if n > 1 else 'CD'} → PBP]", "info")
            layouts, ids = [], []
            for i, chd in enumerate(chds):
                label = f"disc {i + 1} of {n}" if n > 1 else ""
                cue = tmp / f"d{i}.cue"
                if chd_kind(chd) != "CD":
                    return fail("Failed (not a CD)", f"{chd.name} is not a CD image; PBP files hold PlayStation 1 CDs only.")
                cmd = [CHDMAN, "extractcd", "-i", str(chd), "-o", str(cue), "-f"]
                self.post("log", "$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), "cmd")
                rc = self.run_proc(cmd, job, ("Extracting " + label).strip())
                if self.stop_flag or rc != 0:
                    job.status = "Stopped" if self.stop_flag else f"Failed (code {rc})"
                    return
                try:
                    lay = cue_layout(cue)
                except (UnpackError, OSError) as e:
                    return fail("Failed (can't read disc)", f"{chd.name}: {e}")
                gid = ps1_boot_id(lay["bin"], 24) or ps1_boot_id(lay["bin"], 16)
                if not gid:
                    return fail("Failed (not PS1)", f"{chd.name} doesn't look like a PlayStation 1 game, so it can't go in a PBP.")
                layouts.append(lay)
                ids.append(gid)
            if o["name_format"].strip() != "%FILENAME%":
                job.game_id = ids[0]                    # the code read from the disc names the file
                new = self.output_path(job, o, ".pbp")
                if new != out:
                    out, force, skip = self.handle_exists(job, new, o)
                    if skip:
                        return
                    part = out.with_name(out.name + ".part")
                    job.out_path = out
            donor = None
            if o.get("base_pbp_file"):
                try:
                    donor = read_base_pbp(o["base_pbp_file"])
                except (UnpackError, OSError) as e:
                    self.post("log", f"The base PBP wasn't used: {e}", "warn")
            self.post("log", f"Using base PBP {Path(o['base_pbp_file']).name} (loader, icon and boot logo for a PSP/Vita)." if donor else
                      "No base PBP: this PBP works in emulators and frontends, but a PSP/Vita needs one (Settings, Formats).",
                      "info" if donor else "warn")
            title = re.sub(r"\s*\((?:disc|cd)\s*\d+\)", "", out.stem, flags=re.I)
            title = re.sub(r"\s*\[[^\]]*\]", "", title).strip() or out.stem
            try:
                write_pbp(part, layouts, ids, title, lambda x: prog("Writing PBP", x), lambda: self.stop_flag, donor=donor,
                          level=o["pbp_level"], art=self.pbp_art(o, out.stem, ids[0], title))
                if o["verify_after"]:
                    if not verify_pbp(part, layouts, lambda x: prog("Verifying", x), lambda: self.stop_flag):
                        return fail("Verify failed", "the finished PBP didn't match the disc it was made from; it was discarded.")
            except UnpackError as e:
                if self.stop_flag:
                    job.status = "Stopped"
                    return
                return fail("Failed (can't write)", str(e))
            except OSError as e:
                return fail("Failed (disk)", str(e))
            os.replace(part, out)
            job.out_size = out.stat().st_size
            job.status = "Done"
            job.result = f"→ {out.name}"
            self.post("log", f"OK  {out}  ({fmt_size(job.out_size)})", "ok")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            if part.exists():
                try:
                    part.unlink()
                except OSError:
                    pass

    def extract_to_cdi(self, job, o):
        """CD CHD -> DiscJuggler .cdi (the reverse of reading one)."""
        out = self.output_path(job, o, ".cdi")
        out, force, skip = self.handle_exists(job, out, o)
        if skip:
            return
        tmp = out.parent / f".{out.stem[:40]}.chdm_tmp"
        part = out.with_name(out.name + ".part")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        job.out_path = out
        last = [-1]

        def prog(label, x):
            pct = int(100 * x)
            if pct != last[0]:
                last[0] = pct
                job.status = f"{label} {pct}%"
                self.post("job", job)
                self.post("progress", pct)

        def fail(status, why):
            job.status, job.result = status, why
            self.post("log", f"{job.path.name}: {why}", "err")

        try:
            cue = tmp / "t.cue"
            cmd = [CHDMAN, "extractcd", "-i", str(job.path), "-o", str(cue), "-f"]
            self.post("log", f"\n{job.path.name}  [CD → CDI]", "info")
            self.post("log", "$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), "cmd")
            rc = self.run_proc(cmd, job, "Extracting")
            if self.stop_flag or rc != 0:
                job.status = "Stopped" if self.stop_flag else f"Failed (code {rc})"
                return
            try:
                write_cdi(part, cue, lambda x: prog("Writing CDI", x), lambda: self.stop_flag)
                if o["verify_after"] and not verify_cdi(part, cue, lambda x: prog("Verifying", x), lambda: self.stop_flag):
                    return fail("Verify failed", "the finished CDI didn't match the disc it was made from; it was discarded.")
            except UnpackError as e:
                if self.stop_flag:
                    job.status = "Stopped"
                    return
                return fail("Failed (can't write)", str(e))
            except OSError as e:
                return fail("Failed (disk)", str(e))
            os.replace(part, out)
            job.out_size = out.stat().st_size
            job.status = "Done"
            job.result = f"→ {out.name}"
            self.post("log", f"OK  {out}  ({fmt_size(job.out_size)})", "ok")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            if part.exists():
                try:
                    part.unlink()
                except OSError:
                    pass

    def extract_to_iso(self, job, o, to_cso=False, lz4=False):
        """CD CHD -> .iso (or .cso / .zso). Only possible when the disc is one data track; anything else would lose data."""
        out = self.output_path(job, o, (".zso" if lz4 else ".cso") if to_cso else ".iso")
        out, force, skip = self.handle_exists(job, out, o)
        if skip:
            return
        tmp = out.parent / f".{out.stem[:40]}.chdm_tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        part = out.with_name(out.name + ".part")
        try:
            cue = tmp / "t.cue"
            cmd = [CHDMAN, "extractcd", "-i", str(job.path), "-o", str(cue), "-f"]
            job.out_path = out
            self.post("log", f"\n{job.path.name}  [CD → {('ZSO' if lz4 else 'CSO') if to_cso else 'ISO'}]", "info")
            self.post("log", "$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), "cmd")
            rc = self.run_proc(cmd, job, "Extracting")
            if self.stop_flag or rc != 0:
                job.status = "Stopped" if self.stop_flag else f"Failed (code {rc})"
                return
            tracks = parse_cue_tracks(cue)
            if len(tracks) != 1 or tracks[0][1] not in ("MODE1/2048", "MODE1/2352", "MODE2/2352"):
                what = "audio tracks or several tracks" if len(tracks) != 1 else f"{tracks[0][1]} data"
                job.status = "Failed (can't be an ISO)"
                job.result = f"This disc has {what}; an ISO would lose data. Extract it as CUE/BIN instead."
                self.post("log", f"{job.path.name}: {job.result}", "err")
                return
            src, ttype = tracks[0]
            form2 = False
            if ttype == "MODE1/2048":
                os.replace(src, part)
            else:                                   # raw 2352-byte sectors: keep the 2048 bytes of user data in each
                mode2 = ttype == "MODE2/2352"
                off = 24 if mode2 else 16           # Mode 2 sectors carry an 8-byte subheader before the data
                total = max(1, src.stat().st_size // 2352)
                done, last = 0, -1
                with open(src, "rb") as f, open(part, "wb") as g:
                    while True:
                        chunk = f.read(2352 * 1024)
                        if not chunk:
                            break
                        mv = memoryview(chunk)
                        if mode2 and any(mv[i + 18] & 0x20 for i in range(0, len(chunk) - 2351, 2352)):
                            form2 = True           # a Form 2 sector (audio/video) cannot fit in an ISO
                            break
                        g.write(b"".join(mv[i + off:i + off + 2048] for i in range(0, len(chunk) - 2351, 2352)))
                        done += len(chunk) // 2352
                        pct = int(100 * done / total)
                        if pct != last:
                            last = pct
                            job.status = f"Making ISO {pct}%"
                            self.post("job", job)
                            self.post("progress", pct)
                        if self.stop_flag:
                            break
            if form2:
                job.status = "Failed (can't be an ISO)"
                job.result = ("This disc has audio/video (Mode 2 Form 2) sectors, common on PS1 discs; an ISO would lose "
                              "them. Extract it as CUE/BIN instead.")
                self.post("log", f"{job.path.name}: {job.result}", "err")
                return
            if self.stop_flag:
                job.status = "Stopped"
                return
            if to_cso:
                self.write_cso_file(job, o, part, out.with_name(out.name + ".part2"), out, lz4)
                return
            os.replace(part, out)
            ok = DiscReader(out, 2048, 0).sector(16)[1:6] == b"CD001"
            job.status = "Done"
            job.result = f"→ {out.name}" + ("" if ok else "  (no ISO 9660 header found - check it)")
            self.post("log", f"OK  {out}", "ok" if ok else "warn")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            out.with_name(out.name + ".part2").unlink(missing_ok=True)
            if part.exists():
                try:
                    part.unlink()
                except OSError:
                    pass

    def do_recompress(self, job, o):
        """CHD -> CHD with different compression / hunk size (chdman 'copy'). The original is never touched."""
        kind = job.disc if job.disc not in ("", "?") else chd_kind(job.path)
        job.disc = kind
        if kind not in ("CD", "GD-ROM", "DVD"):
            job.status = "Failed (not possible)"
            job.result = f"Recompress works for CD, GD-ROM and DVD CHDs; this one is {kind}."
            self.post("log", f"{job.path.name}: {job.result}", "err")
            return
        typ = "DVD" if kind == "DVD" else "CD"
        out = self.output_path(job, o, ".chd")
        try:
            if out.resolve() == job.path.resolve():
                out = out.with_name(out.stem + " (recompressed).chd")
        except OSError:
            pass
        out, force, skip = self.handle_exists(job, out, o)
        if skip:
            return
        cmd = [CHDMAN, "copy", "-i", str(job.path), "-o", str(out)] + (["-f"] if force else []) + self.common_args(o, typ)
        job.out_path = out
        self.post("log", f"\n{job.path.name}  [recompress {kind}]", "info")
        self.post("log", "$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), "cmd")
        rc = self.run_proc(cmd, job, "Recompressing")
        if self.stop_flag or rc != 0:
            job.status = "Stopped" if self.stop_flag else f"Failed (code {rc})"
            if out.exists():
                try:
                    out.unlink()
                except OSError:
                    pass
            return
        job.out_size = out.stat().st_size
        if o["verify_after"]:
            self.post("log", "Verifying…", "info")
            _, ok = self.verify_file(out, job, "Verifying")
            if ok is False:
                job.status = "Verify failed"
                self.post("log", f"VERIFY FAILED for {out.name} - your original CHD was not touched.", "err")
                return
        job.status = "Done"
        self.post("log", f"OK  {out.name}  ({fmt_size(job.size)} → {fmt_size(job.out_size)})", "ok")

    def run_proc(self, cmd, job, label, capture=None):
        flags = CREATE_NO_WINDOW | (0x00004000 if getattr(self, "cur_opts", {}).get("low_priority") else 0)  # BELOW_NORMAL
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     bufsize=0, creationflags=flags)
        buf, last = b"", -1
        self.last_msg = self.reason_msg = ""
        while True:
            chunk = self.proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for part in parts:
                line = part.decode("utf-8", "replace").strip()
                if not line:
                    continue
                m = re.search(r"([\d.]+)%\s+complete", line)
                if m:
                    pct = float(m.group(1))
                    if int(pct) != last:
                        last = int(pct)
                        job.status = f"{label} {last}%"
                        self.post("job", job)
                        self.post("progress", pct)
                    continue
                if capture is not None:
                    capture.append(line)
                if not line.startswith("chdman - "):
                    self.last_msg = line
                    low = line.lower()
                    bad = any(w in low for w in ("error", "fatal", "not divisible", "cannot", "can't", "failed", "invalid", "unsupported"))
                    if bad and not low.startswith("fatal error occurred"):
                        self.reason_msg = line              # the specific reason, not chdman's generic last line
                    self.post("log", "  " + line, "err" if bad else "info")
        if buf.strip():
            line = buf.decode("utf-8", "replace").strip()
            if capture is not None:
                capture.append(line)
            self.post("log", "  " + line, "info")
        rc = self.proc.wait()
        if rc != 0 and not self.stop_flag and (self.reason_msg or self.last_msg):
            job.result = (self.reason_msg or self.last_msg)[:140]              # the reason, visible in the table
        self.post("progress", 100 if rc == 0 else 0)
        return rc

    # ------------------------------------------------------------------ info / verify buttons
    def info_selected(self):
        sel = self.selected_jobs()
        if not sel:
            return
        job = sel[0]
        if job.path.suffix.lower() == ".chd":
            def work():
                rc, text = run_capture([CHDMAN, "info", "-v", "-i", str(job.path)])
                self.post("info", job.path.name, text)
            threading.Thread(target=work, daemon=True).start()
        else:
            lines = [f"File:       {job.path}", f"Platform:   {job.platform or 'unknown'}",
                     f"Disc type:  {job.type}{'  (forced)' if job.override != 'Auto' else '  (auto)'}",
                     f"Why:        {job.reason}", f"Total size: {fmt_size(job.size)}", "", "Files:"]
            lines += [f"  {'(MISSING) ' if not f.exists() else ''}{f}" for f in job.src_files]
            self.show_info(job.path.name, "\n".join(lines))

    def show_info(self, title, text):
        win = tk.Toplevel(self.root)
        win.title(f"Info - {title}")
        win.geometry("780x520")
        win.configure(bg=self.t["bg"])
        t = tk.Text(win, wrap="none", font=("Consolas", 9), bg=self.t["panel"], fg=self.t["fg"], relief="flat",
                    bd=0, padx=10, pady=8, insertbackground=self.t["fg"], selectbackground=self.t["sel"])
        t.insert("1.0", text)
        t.configure(state="disabled")
        sb = ttk.Scrollbar(win, command=t.yview)
        t.configure(yscrollcommand=sb.set)
        bt = ttk.Frame(win)
        bt.pack(side="bottom", fill="x", pady=6)
        ttk.Button(bt, text="Copy", command=lambda: (self.root.clipboard_clear(), self.root.clipboard_append(text))).pack(side="left", padx=8)
        ttk.Button(bt, text="Close", command=win.destroy).pack(side="right", padx=8)
        sb.pack(side="right", fill="y")
        t.pack(fill="both", expand=True)

    def verify_selected(self):
        chds = [j for j in self.selected_jobs() if j.path.suffix.lower() == ".chd"]
        if not chds:
            self.status.config(text="Select one or more .chd files to verify.")
            return
        for j in chds:
            j.set_target("verify")
            self.update_action_labels()
        self.start(chds)

    # ------------------------------------------------------------------ CHD Manager's own updates
    def check_app_update_async(self, manual=False):
        def work():
            try:
                self.post("app_upd_info", fetch_app_release(), "", manual)
            except Exception as e:  # noqa: BLE001
                self.post("app_upd_info", None, str(e), manual)
        threading.Thread(target=work, daemon=True).start()
        if manual:
            self.status.config(text="Checking GitHub for a newer CHD Manager…")
            self.set_footer("app", "checking…")

    def app_update_available(self):
        return bool(getattr(self, "app_latest", None) and ver_tuple(self.app_latest["tag"]) > ver_tuple(APP_VER))

    def diagnostics_text(self):
        """Handy for bug reports. Deliberately leaves out folder paths and user names."""
        ram = total_ram_gb()
        return "\n".join([
            f"CHD Manager {APP_VER} ({'exe' if FROZEN else 'script'})",
            f"chdman {self.chd_version or 'not found'}" + ("" if self.has_dvd else " (no createdvd)"),
            f"Windows: {platform.platform()}",
            f"Python {sys.version.split()[0]}, Tk {tk.TkVersion}",
            f"CPU threads: {os.cpu_count()}" + (f", RAM: {ram:.0f} GB" if ram else ""),
            f"Settings: threads {self.v['threads'].get()}, compression {self.v['compression'].get()}, "
            f"low priority {self.v['low_priority'].get()}, theme {self.v['theme'].get()}",
        ])

    def copy_diagnostics(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.diagnostics_text())
        self.status.config(text="Diagnostics copied to the clipboard.")

    def ask_update_choice(self, msg):
        """Three-way prompt -> 'now' | 'later' | 'skip'."""
        if self.silent:
            return getattr(self, "silent_choice", None) or ("now" if getattr(self, "silent_answer", True) else "later")
        result = {"v": "later"}
        win = tk.Toplevel(self.root)
        win.title("CHD Manager update")
        win.configure(bg=self.t["bg"])
        win.transient(self.root)
        win.resizable(False, False)
        ttk.Label(win, text=msg, wraplength=470, justify="left").pack(padx=22, pady=(20, 12))
        bf = ttk.Frame(win)
        bf.pack(padx=22, pady=(0, 18), anchor="e")

        def pick(v):
            result["v"] = v
            win.destroy()

        ttk.Button(bf, text="Update now", style="Accent.TButton", command=lambda: pick("now")).pack(side="left")
        ttk.Button(bf, text="Remind me next time", command=lambda: pick("later")).pack(side="left", padx=8)
        ttk.Button(bf, text="Skip this version", command=lambda: pick("skip")).pack(side="left")
        win.protocol("WM_DELETE_WINDOW", lambda: pick("later"))
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_width()) // 2
        y = self.root.winfo_rooty() + 120
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        win.grab_set()
        win.wait_window()
        return result["v"]

    def on_app_update_info(self, info, err, manual):
        if err or not info:
            if manual:
                self.status.config(text="Could not check for a CHD Manager update.")
                self.log("CHD Manager update check failed: " + err, "warn")
                self.set_footer("app", "couldn't check", revert_after=8000)
            return
        self.app_latest = info
        new_v = ".".join(str(x) for x in ver_tuple(info["tag"]))
        if not self.app_update_available():
            self.draw_header()
            self.app_upd_label.config(text=f"Up to date ({APP_VER}).", style="Muted.TLabel")
            self.set_footer("app", "up to date" if manual else "", revert_after=8000 if manual else 0)
            if manual:
                self.status.config(text=f"CHD Manager {APP_VER} is the latest version.")
                self.log(f"CHD Manager {APP_VER} is up to date.", "ok")
            return
        self.app_upd_label.config(text=f"{new_v} available (you have {APP_VER}).", style="Warn.TLabel")
        self.set_footer("app", f"{new_v} available - click to update", "warn")
        self.draw_header()                      # shows the clickable "update available" badge
        self.log(f"A newer CHD Manager is available: {new_v} (you have {APP_VER}). Click the badge at the top right, "
                 "or use Settings, About page.", "warn")
        # startup: ask unless this exact version was skipped; the button and the badge always ask
        if manual or self.v["skip_version"].get() != info["tag"]:
            self.confirm_app_update()

    def confirm_app_update(self):
        info = self.app_latest
        new_v = ".".join(str(x) for x in ver_tuple(info["tag"]))
        notes = info["notes"].split("\n\n")[0][:300]
        if self.running:
            self.notify("Wait for the current jobs to finish, then click the update badge again.")
            return
        if FROZEN:
            msg = (f"CHD Manager {new_v} is available (you have {APP_VER}).\n\n{notes}\n\n"
                   f"Update now? It downloads {info['name']} ({fmt_size(info['size'])}) from github.com/{APP_REPO}, "
                   "checks its SHA-256, test-runs it, and restarts into the new version. "
                   "Your settings and chdman stay as they are.")
        else:
            msg = (f"CHD Manager {new_v} is available (you have {APP_VER}).\n\n{notes}\n\n"
                   "You're running the script version, so it can't replace itself. "
                   "'Update now' opens the download page.")
        choice = self.ask_update_choice(msg)
        if choice == "skip":
            self.v["skip_version"].set(info["tag"])
            self.log(f"Skipping {new_v} at startup. The update badge and Settings, About page still offer it.", "info")
        elif choice == "now":
            if not FROZEN:
                if not self.silent:
                    webbrowser.open(info["page"])
                return
            self.app_upd_btn.config(state="disabled")
            self.app_btn.config(state="disabled")
            threading.Thread(target=self.app_update_worker, args=(info,), daemon=True).start()
        # "later": nothing recorded, so it asks again next launch

    def app_update_worker(self, info):
        report_dir = None
        try:
            new_exe = stage_app_update(info, HERE, lambda pct, txt: self.post("upd_progress", pct, txt))
            self.post("upd_progress", 100, "Testing the new version…")
            report_dir = Path(tempfile.mkdtemp(prefix="chdm_selfcheck_"))
            report = report_dir / "report.json"
            run_capture([str(new_exe), "--smoke", str(report)], timeout=180)
            r = json.loads(report.read_text("utf-8")) if report.exists() else {}
            if r.get("status") != "Done" or ver_tuple(r.get("app_version", "")) != ver_tuple(info["tag"]):
                try:
                    new_exe.unlink()
                except OSError:
                    pass
                raise RuntimeError("The downloaded version failed its self-test, so it was not installed. "
                                   + (r.get("error", "") or f"(status: {r.get('status')}, version: {r.get('app_version')})"))
            old_exe = Path(sys.executable).resolve()
            self.post("app_upd_ready", str(write_swap_script(new_exe, old_exe)))
        except Exception as e:  # noqa: BLE001
            self.post("app_upd_done", False, str(e))
        finally:
            if report_dir:
                shutil.rmtree(report_dir, ignore_errors=True)

    def on_app_update_done(self, ok, msg):
        self.app_upd_btn.config(state="normal")
        self.app_btn.config(state="normal")
        self.set_footer("app")
        self.cur_bar["value"] = 0
        self.status.config(text="CHD Manager update failed - nothing was changed.")
        self.log("CHD Manager update failed: " + msg, "err")
        if not self.silent:
            messagebox.showerror(APP_NAME, "The update failed:\n" + msg + "\n\nYour current version was not changed.")

    def on_app_update_ready(self, script):
        self.status.config(text="Restarting into the new version…")
        self.log("Restarting into the new version…", "ok")
        self.app_swap_script = script
        if self.silent:
            return
        spawn_swap(script)
        self.save_settings()
        self.root.destroy()

    # ------------------------------------------------------------------ chdman update
    def check_update_async(self, manual=False):
        def work():
            try:
                self.post("upd_info", fetch_latest(), "", manual)
            except Exception as e:  # noqa: BLE001
                self.post("upd_info", None, str(e), manual)
        threading.Thread(target=work, daemon=True).start()
        if manual:
            self.status.config(text="Checking GitHub for the latest chdman…")
            self.set_footer("chdman", "checking…")

    def update_available(self):
        cur = ver_to_int(self.chd_version) if CHDMAN else 0
        return bool(self.latest and self.latest["num"] > cur)

    def update_clicked(self):
        if self.running:
            self.status.config(text="Wait for the current jobs to finish before updating chdman.")
            return
        if self.update_available():
            self.confirm_install()
        else:
            self.check_update_async(manual=True)

    def on_update_info(self, info, err, manual):
        if err or not info:
            if manual:
                self.status.config(text="Could not check for updates.")
                self.log("Update check failed: " + err, "warn")
                self.set_footer("chdman", "couldn't check", revert_after=8000)
            return
        self.latest = info
        new_txt = f"0.{info['num']:03d}"
        if self.update_available():
            self.set_footer("chdman", f"{new_txt} available - click to update", "warn")
            self.log(f"A newer chdman is available: {new_txt} (you have {self.chd_version or 'none'}).", "warn")
            if manual:
                self.confirm_install()
        else:
            self.set_footer("chdman", "up to date" if manual else "", revert_after=8000 if manual else 0)
            if manual:
                self.status.config(text=f"chdman is up to date ({self.chd_version}).")
                self.log(f"chdman {self.chd_version} is the latest release.", "ok")

    def notify(self, msg, error=False):
        self.messages = getattr(self, "messages", []) + [msg]
        self.log(msg.replace("\n\n", "  ").replace("\n", " "), "warn")
        if not self.silent:
            (messagebox.showerror if error else messagebox.showinfo)(APP_NAME, msg)

    def ensure_7zip(self):
        """True if 7-Zip is available (offering to install it if not)."""
        if find_7z():
            return True
        if not self.ask("Updating chdman needs 7-Zip, a free file tool that isn't installed on this PC.\n\n"
                        "Install it now? It comes from the official Windows Package Manager (winget), is about 2 MB, "
                        "and Windows will ask for your permission first."):
            self.notify("No problem. Without 7-Zip, CHD Manager can't update chdman automatically, so the update "
                        "button won't work. Everything else in the program works normally.\n\n"
                        "To update by hand, download chdman from github.com/mamedev/mame/releases and pick it with "
                        "Browse in Settings, Output page. You can also install 7-Zip later from 7-zip.org.")
            return False
        self.status.config(text="Installing 7-Zip… (approve the Windows prompt if it appears)")
        self.root.update_idletasks()
        ok, msg = install_7zip()
        if ok:
            self.log("7-Zip installed.", "ok")
            return True
        if msg == "no-winget":
            self.notify("This PC doesn't have the Windows Package Manager (winget), so I can't install 7-Zip "
                        "automatically.\n\nPlease install it from 7-zip.org, then press the update button again. "
                        "Until then the update button won't work, but everything else does.", error=True)
        else:
            self.notify("7-Zip wasn't installed (the permission prompt may have been declined).\n\n"
                        "Without it CHD Manager can't update chdman automatically. Everything else works normally.\n"
                        "You can install 7-Zip from 7-zip.org and try again.\n\nDetails: " + msg, error=True)
        return False

    def confirm_install(self):
        info = self.latest
        name, _, size = info["exe"]
        if not self.ensure_7zip():
            self.status.config(text="Update cancelled (7-Zip not available).")
            return
        cur = self.chd_version or "none"
        if self.ask(f"Update chdman {cur}  →  0.{info['num']:03d}?\n\n"
                    f"Download: {name} ({fmt_size(size)}) from the official MAME release on GitHub.\n"
                    "It is checked against the release's SHA-256, only chdman.exe is taken out of it "
                    "(nothing is run), and your current chdman is backed up in a 'chdman_old' folder.\n\nContinue?"):
            self.upd_btn.config(state="disabled")
            threading.Thread(target=self.install_update, args=(info,), daemon=True).start()

    def install_update(self, info):
        tmp = Path(tempfile.mkdtemp(prefix="chdman_update_"))
        try:
            name, url, size = info["exe"]
            sums = http_get(info["sums"]).read().decode("utf-8", "replace")
            want = None
            for line in sums.splitlines():
                m = re.match(r"([0-9a-fA-F]{64})\s+\*?(.+)$", line.strip())
                if m and m.group(2).strip() == name:
                    want = m.group(1).lower()
            if not want:
                raise RuntimeError(f"no checksum is published for {name}")
            dest, h, got = tmp / name, hashlib.sha256(), 0
            with http_get(url, 60) as r, open(dest, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    self.post("upd_progress", 100 * got / max(size, 1),
                              f"Downloading {name}: {fmt_size(got)} / {fmt_size(size)}")
            if h.hexdigest() != want:
                raise RuntimeError("checksum mismatch - the download was discarded")
            self.post("upd_progress", 100, "Checksum OK. Extracting chdman.exe…")
            out = tmp / "x"
            out.mkdir()
            rc, text = run_capture([find_7z(), "e", str(dest), f"-o{out}", "chdman.exe", "-y"], timeout=900)
            newexe = out / "chdman.exe"
            if rc != 0 or not newexe.is_file():
                raise RuntimeError("could not extract chdman.exe: " + text[-200:])
            _, t2 = run_capture([str(newexe)])
            mv = re.search(r"manager ([\d.]+)", t2)
            if not mv or ver_to_int(mv.group(1)) != info["num"]:
                raise RuntimeError("the downloaded chdman reports an unexpected version")
            target = Path(CHDMAN) if CHDMAN else HERE / "chdman.exe"
            backup = ""
            if target.exists():
                bdir = target.parent / "chdman_old"
                bdir.mkdir(exist_ok=True)
                cand = bdir / f"chdman_{self.chd_version or 'old'}.exe"
                n = 1
                while cand.exists():
                    cand = bdir / f"chdman_{self.chd_version or 'old'}_{n}.exe"
                    n += 1
                shutil.copy2(target, cand)
                backup = str(cand)
            staged = target.with_name("chdman.new.exe")
            shutil.copy2(newexe, staged)
            os.replace(staged, target)
            self.post("upd_done", True, str(target), mv.group(1), backup)
        except Exception as e:  # noqa: BLE001
            self.post("upd_done", False, str(e), "", "")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def on_update_done(self, ok, a, b, c):
        global CHDMAN
        self.upd_btn.config(state="normal")
        self.cur_bar["value"] = 0
        if not ok:
            self.status.config(text="chdman update failed - your current chdman was not changed.")
            self.log("chdman update failed: " + a, "err")
            if not self.silent:
                messagebox.showerror(APP_NAME, "chdman update failed:\n" + a + "\n\nYour current chdman was not changed.")
            return
        CHDMAN = a
        self.chd_path_var.set(a)
        self.chd_version, self.has_dvd = chdman_info()
        self.set_footer("chdman", "updated", revert_after=8000)
        self.status.config(text=f"chdman updated to {self.chd_version}.")
        self.log(f"chdman updated to {self.chd_version}: {a}", "ok")
        if c:
            self.log(f"Previous version backed up to {c}", "info")

    # ------------------------------------------------------------------ UI pump
    def pump(self):
        try:
            while True:
                ev = self.q.get_nowait()
                kind = ev[0]
                if kind == "job":
                    self.refresh_job(ev[1])
                elif kind == "log":
                    self.log(ev[1], ev[2])
                elif kind == "progress":
                    self.cur_bar["value"] = ev[1]
                elif kind == "overall":
                    self.all_bar["value"] = 100 * ev[1] / max(1, ev[2])
                elif kind == "label":
                    self.status.config(text=ev[1])
                    if self.echo:
                        self.echo(ev[1], "label")
                elif kind == "info":
                    self.show_info(ev[1], ev[2])
                elif kind == "upd_info":
                    self.on_update_info(ev[1], ev[2], ev[3])
                elif kind == "upd_progress":
                    self.cur_bar["value"] = ev[1]
                    self.status.config(text=ev[2])
                elif kind == "upd_done":
                    self.on_update_done(ev[1], ev[2], ev[3], ev[4])
                elif kind == "app_upd_info":
                    self.on_app_update_info(ev[1], ev[2], ev[3])
                elif kind == "app_upd_ready":
                    self.on_app_update_ready(ev[1])
                elif kind == "app_upd_done":
                    self.on_app_update_done(ev[1], ev[2])
                elif kind == "finish":
                    self.running = False
                    if self.v["auto_log"].get():
                        try:
                            with open(HERE / "chd_manager_log.txt", "a", encoding="utf-8") as lf:
                                lf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n"
                                         + self.logbox.get(getattr(self, "log_mark", "1.0"), "end-1c") + "\n")
                        except (OSError, tk.TclError):
                            pass
                    for j in self.jobs:
                        if j.status.startswith("Done"):
                            self.drop_arc(j)
                    self.root.after_idle(self.autofit_columns)
                    self.start_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    self.status.config(text=ev[1])
                    self.log("\n" + ev[1], "ok" if ev[2] else "warn")
                    self.set_state("done" if ev[2] else "fail")
                    if not ev[2]:
                        self.show_log(True)
                    self.cur_bar["value"] = 0
                    self.proc = None
                    if not self.silent:
                        try:
                            import winsound
                            winsound.MessageBeep()
                        except Exception:  # noqa: BLE001
                            pass
        except queue.Empty:
            pass
        try:
            self.root.after(80, self.pump)
        except tk.TclError:
            pass


def smoke_test(outfile):
    """Hidden self-check used to verify the packaged exe: convert + verify a tiny disc, write a JSON report."""
    report = {"frozen": FROZEN, "here": str(HERE), "app_version": APP_VER}
    tmp = Path(tempfile.mkdtemp(prefix="chdm_smoke_"))
    try:
        root = tk.Tk()
        root.withdraw()
        app = App(root, silent=True)
        app.v["theme"].set("light")
        report.update(chdman=CHDMAN, chdman_version=app.chd_version, has_dvd=app.has_dvd)
        iso = tmp / "Smoke Test.iso"
        iso.write_bytes((b"CHD MANAGER SMOKE TEST " * 100)[:2048] * 600)
        app.add_paths([str(iso)])
        app.v["verify_after"].set(True)
        report["finished"] = app.run_blocking(timeout=120)
        report["status"] = app.jobs[0].status if app.jobs else "no job"
        report["chd_made"] = (tmp / "Smoke Test.chd").is_file()
        root.destroy()
    except Exception:  # noqa: BLE001
        report["error"] = traceback.format_exc()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    Path(outfile).write_text(json.dumps(report, indent=2), "utf-8")


CLI_EPILOG = """\
examples:
  CHD Manager cli game.cue --to chd -o D:\\chd
  CHD Manager cli game.chd --to pbp --level 9 -o D:\\psp
  CHD Manager cli D:\\roms --to chd --exists overwrite --log convert.txt
  CHD Manager cli game.chd game2.iso --list

targets (--list shows which ones a given file can become):
""" + "\n".join(f"  {k:<11}{v}" for k, v in TARGET_LABELS.items()) + """
  chd_d2, cue_d2, ...   one disc (here the 2nd) of a multi-disc PBP

exit code: 0 = everything done or skipped, 1 = something failed, 2 = wrong command line
"""


def attach_console():
    """The packaged program is a windowed one, so it has no console of its own; use the one it was started from."""
    ok = False
    try:
        ok = sys.stdout is not None and sys.stdout.writable()
    except (AttributeError, ValueError, OSError):
        pass
    if not ok and os.name == "nt":
        try:
            ctypes.windll.kernel32.AttachConsole(-1)
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stderr = sys.stdout
        except Exception:  # noqa: BLE001
            pass
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def cli_main(argv):
    """chdm cli ...: the same conversions as the window, without the window."""
    import argparse
    attach_console()
    ap = argparse.ArgumentParser(prog="CHD Manager cli", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=CLI_EPILOG,
                                 description=f"{APP_NAME} {APP_VER}: convert disc images from the command line. "
                                             "Takes files, folders and archives; what each one becomes is chosen with --to.")
    ap.add_argument("inputs", nargs="*", help="files, folders or archives")
    ap.add_argument("--to", metavar="TARGET", help="what to make (see below); default: each file's usual action")
    ap.add_argument("--list", action="store_true", help="only show what each input can become")
    ap.add_argument("-o", "--output", metavar="DIR", help="output folder (default: next to each source file)")
    ap.add_argument("--flat", action="store_true", help="with -o: don't recreate the input folders' structure")
    ap.add_argument("--name", metavar="PATTERN", help="output file name, e.g. \"%%GAMEID%% - %%TITLE%%\" (%%FILENAME%% %%GAMEID%% "
                    "%%TITLE%% %%MAINTITLE%% %%MAINGAMEID%% %%REGION%%)")
    ap.add_argument("--exists", choices=("skip", "overwrite", "rename"), default="skip", help="if the output exists (default: skip)")
    ap.add_argument("--no-verify", action="store_true", help="don't verify each result")
    ap.add_argument("--compression", choices=("default", "none", "fast", "smallest"), default="default", help="CHD compression")
    ap.add_argument("--codecs", metavar="LIST", help="custom CHD codecs, e.g. cdlz,cdzl (instead of --compression)")
    ap.add_argument("--hunk", metavar="BYTES", help="CHD hunk size")
    ap.add_argument("--threads", type=int, default=0, metavar="N", help="CHD processors (default: all)")
    ap.add_argument("--level", type=int, choices=range(10), metavar="0-9", help="PBP compression (default 6)")
    ap.add_argument("--wii-key", metavar="HEX", help="your Wii common key (32 hex characters); or put it in the keys folder")
    ap.add_argument("--dolphintool", metavar="EXE", help="your DolphinTool.exe, used for Wii RVZ/WIA when there is no key")
    ap.add_argument("--rvz-method", choices=("zstd", "lzma", "lzma2", "bzip2", "none"), help="RVZ/WIA compression (default zstd)")
    ap.add_argument("--rvz-level", type=int, choices=range(1, 10), metavar="1-9", help="RVZ/WIA compression level (default 5)")
    ap.add_argument("--rvz-block", type=int, metavar="KIB", help="RVZ block size in KiB, 32 to 2048 (default 128)")
    ap.add_argument("--no-scrub", action="store_true", help="WBFS: keep all data, don't leave out what nothing uses")
    ap.add_argument("--art", metavar="DIR", help="your PBP artwork folder (one subfolder per game)")
    ap.add_argument("--no-cards", action="store_true", help="don't draw title cards for PBPs without artwork")
    ap.add_argument("--base-pbp", metavar="FILE", help="base PBP for a real PSP/Vita")
    ap.add_argument("--chdman", metavar="EXE", help="use this chdman")
    ap.add_argument("--log", metavar="FILE", help="also write the full log to this file")
    ap.add_argument("-v", "--verbose", action="store_true", help="show the full log, including chdman's output")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VER}")
    a = ap.parse_args(argv)
    if not a.inputs:
        ap.print_usage()
        print("nothing to do: give at least one file, folder or archive (or --help)")
        return 2
    if a.to and a.to.partition("_d")[0] not in TARGET_LABELS:
        print(f"unknown target '{a.to}'. Choose one of: {', '.join(TARGET_LABELS)}")
        return 2
    logf = None
    if a.log:
        try:
            logf = open(a.log, "a", encoding="utf-8")
            logf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')}  {' '.join(argv)} =====\n")
        except OSError as e:
            print(f"can't write the log file: {e}")
            return 2

    def say(text):
        print(text, flush=True)
        if logf:
            logf.write(text + "\n")

    root = tk.Tk()
    root.withdraw()
    app = App(root, silent=True)
    app.no_save = True                                  # a command-line run never changes the window's saved settings
    code = 1
    try:
        for k, d in DEFAULTS.items():                    # only the machine's own paths come from the saved settings
            if k in app.v and k not in ("base_pbp", "art_dir", "dolphintool", "wii_common_key", "wii_korean_key"):
                app.v[k].set(d)
        v = app.v
        if a.output:
            v["out_mode"].set("custom")
            v["out_dir"].set(a.output)
            v["keep_structure"].set(not a.flat)
        if a.name:
            v["name_format"].set(a.name)
        v["exists"].set(a.exists.capitalize())
        v["verify_after"].set(not a.no_verify)
        if a.codecs:
            v["compression"].set("Custom")
            v["custom_comp"].set(a.codecs)
        else:
            v["compression"].set(a.compression.capitalize())
        v["hunk"].set(a.hunk or "")
        v["threads"].set(a.threads)
        if a.level is not None:
            v["pbp_level"].set(a.level)
        if a.wii_key:
            v["wii_common_key"].set(a.wii_key)
        if a.dolphintool:
            v["dolphintool"].set(a.dolphintool)
        if a.rvz_method:
            v["rvz_method"].set(a.rvz_method)
        if a.rvz_level:
            v["rvz_level"].set(a.rvz_level)
        if a.rvz_block:
            v["rvz_chunk"].set(a.rvz_block * 1024)
        if a.no_scrub:
            v["wbfs_scrub"].set(False)
        if a.art:
            v["art_dir"].set(a.art)
        if a.base_pbp:
            v["base_pbp"].set(a.base_pbp)
        if a.no_cards:
            v["title_cards"].set(False)
        if a.chdman:
            global CHDMAN
            found = find_chdman(a.chdman)
            if not found:
                say(f"chdman not found: {a.chdman}")
                return 2
            CHDMAN = found
            app.chd_version, app.has_dvd = chdman_info()

        def echo(text, tag):
            if logf:
                logf.write(text + "\n")
            if text.startswith(("Added ", "Click the Action")):
                return                                  # window hints that mean nothing here
            if a.verbose and tag != "cmd" or tag in ("label", "warn", "err"):
                print(text, flush=True)
        app.echo = echo
        app.add_paths(a.inputs)
        if not app.jobs:
            say("no usable files found in the inputs")
            return 1
        if a.list:
            for j in app.jobs:
                say(f"{j.path}\n  {j.platform or '?'} · {j.type_label()} · {fmt_size(j.size)}")
                for k, label in j.available_targets():
                    say(f"    {k:<11}{label}" + ("   (default)" if k == j.default_target() else ""))
            return 0
        todo, bad = [], 0
        for j in app.jobs:
            ok = [k for k, _ in j.available_targets()]
            if a.to and a.to not in ok:
                hint = ("; RVZ, WIA and WBFS from a Wii disc need your Wii key (--wii-key, or the keys folder) or DolphinTool.exe (--dolphintool)"
                        if j.platform == "Wii" and a.to in ("rvz", "wia", "wbfs") else "")
                say(f"FAIL {j.path.name}: can't become '{a.to}' (it can become: {', '.join(ok) or 'nothing'}){hint}")
                bad += 1
                continue
            if a.to:
                j.set_target(a.to)
            todo.append(j)
        if todo:
            app.start(todo)
            try:
                while app.running:
                    root.update()
                    time.sleep(0.02)
                root.update()
            except KeyboardInterrupt:
                app.stop()
                end = time.time() + 15
                while app.running and time.time() < end:
                    root.update()
                    time.sleep(0.02)
                say("stopped")
                return 130
        for j in todo:
            word = "OK  " if j.status.startswith("Done") else "SKIP" if j.status.startswith("Skipped") else "FAIL"
            if word == "FAIL":
                bad += 1
            say(f"{word} {j.path.name}: " + (j.result if word == "OK  " else j.status if word == "SKIP" else f"{j.status} {j.result}".strip()))
        code = 1 if bad else 0
    finally:
        try:
            app.drop_all_arcs()
            root.destroy()
        except tk.TclError:
            pass
        if logf:
            logf.close()
    return code


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "--relaunch-test":
        spawn_swap(write_swap_script(Path(sys.argv[2]), Path(sys.argv[3])))
        return
    if len(sys.argv) >= 2 and sys.argv[1].lower() in ("cli", "--cli"):
        sys.exit(cli_main(sys.argv[2:]))
    if len(sys.argv) >= 3 and sys.argv[1] == "--smoke":
        SETTINGS_FILE_OVERRIDE = Path(sys.argv[2]).with_suffix(".settings.json")
        globals()["SETTINGS_FILE"] = SETTINGS_FILE_OVERRIDE      # don't touch the real settings during the check
        smoke_test(sys.argv[2])
        return
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
    root = tk.Tk()
    files = [a for a in sys.argv[1:] if not a.startswith("--")]
    App(root, initial_paths=files)
    root.mainloop()


if __name__ == "__main__":
    main()
