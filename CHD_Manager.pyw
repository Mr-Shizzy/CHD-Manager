"""
CHD Manager - a friendly GUI for MAME's chdman.exe

Put this file next to chdman.exe (or point it at chdman.exe in Settings, Output page).
Needs only Python 3 (tkinter is included with it). Double-click to run.

What it does
  * Queue of disc images (.cue .gdi .iso .toc, lone .bin) -> .chd, and .chd -> extract / verify
  * Detects CD vs DVD automatically (and the platform: PS1, PS2, PSP, Dreamcast, Saturn, Sega CD)
  * Compression presets, threads, hunk size, overwrite / skip / rename, output folder
  * Verify after create; optional "send sources to Recycle Bin" ONLY after a verified CHD
  * Live progress, log, summary of space saved
"""
import ctypes
import hashlib
import json
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
import webbrowser
import tkinter as tk
from ctypes import wintypes
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_NAME = "CHD Manager"
APP_VER = "1.4.0"
FROZEN = getattr(sys, "frozen", False)          # True when running as the packaged .exe
HERE = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
SETTINGS_FILE = HERE / "chd_manager_settings.json"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

CD_MAX_BYTES = 737_280_000          # 80-minute CD: 360,000 sectors * 2048 bytes
SRC_EXTS = {".cue", ".gdi", ".toc", ".iso", ".bin"}
UNSUPPORTED_EXTS = {".cdi", ".mds", ".mdf", ".nrg", ".ccd", ".img", ".pbp", ".zip", ".7z", ".rar"}
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


KIND = {"Extract": "extract", "Verify": "verify", "Recompress": "recompress"}
FORMAT_LABELS = {"auto": "Automatic (CUE/BIN, GDI or ISO)", "iso": "ISO (data discs)", "split": "CUE/BIN, one BIN per track",
                 "single": "CUE/BIN, one single BIN"}


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
class Job:
    def __init__(self, path, root=None):
        self.path = Path(path)
        self.root = Path(root) if root else None
        self.kind = "convert"          # convert | extract | verify | recompress
        self.task_override = None      # per-item choice for a CHD: "Extract" | "Verify" | "Recompress"
        self.action_text = ""
        self.override = "Auto"         # Auto | CD | DVD
        self.auto_type, self.reason = "CD", ""
        self.platform = ""
        self.disc = ""                 # for CHDs: CD / GD-ROM / DVD / HD / ...
        self.tracks = 0                # for CD CHDs: number of tracks
        self.size = 0
        self.src_files = []
        self.missing = []
        self.lone_bin = False
        self.status = "Queued"
        self.result = ""
        self.out_path = None
        self.out_size = 0

    @property
    def type(self):
        return self.override if self.override != "Auto" else self.auto_type

    def analyze(self, chd_action="Extract"):
        p, ext = self.path, self.path.suffix.lower()
        if ext == ".chd":
            self.kind = KIND.get(self.task_override or chd_action, "extract")
            self.size = p.stat().st_size
            self.disc, self.tracks = chd_info(p) if CHDMAN else ("?", 0)
            self.src_files = [p]
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
        if self.lone_bin:
            self.reason += " (BIN without a CUE)"
        if self.missing:
            self.status = "Missing: " + ", ".join(self.missing[:2])

    def action_label(self):
        return self.action_text or {"convert": "→ CHD", "extract": "Extract", "verify": "Verify", "recompress": "Recompress"}[self.kind]

    def type_label(self):
        if self.kind == "convert":
            return self.type + ("*" if self.override != "Auto" else "")
        return self.disc

    def row(self):
        return (self.path.name, self.action_label(), self.platform, self.type_label(),
                fmt_size(self.size), self.status, self.result, str(self.path.parent))


# --------------------------------------------------------------------------- App
DEFAULTS = {
    "chdman": "", "compression": "Default", "custom_comp": "", "hunk": "", "threads": 0,
    "exists": "Skip", "verify_after": True, "delete_src": False, "auto_cue": True,
    "chd_action": "Extract", "extract_format": "auto", "split_bin": False, "extract_subfolder": False,
    "out_mode": "same", "out_dir": "", "keep_structure": True, "last_dir": "", "geometry": "",
    "auto_update_check": True, "theme": "dark", "auto_suggest": True, "low_priority": False, "skip_version": "", "auto_app_check": True,
}


class App:
    def __init__(self, root, silent=False, initial_paths=None):
        global CHDMAN
        self.root = root
        self.silent = silent
        self.jobs = []
        self.q = queue.Queue()
        self.running = False
        self.stop_flag = False
        self.proc = None
        self.worker = None
        self.tree_ids = {}
        self.settings = dict(DEFAULTS)
        try:
            self.settings.update(json.loads(SETTINGS_FILE.read_text("utf-8")))
        except Exception:  # noqa: BLE001
            pass
        CHDMAN = find_chdman(self.settings.get("chdman", ""))
        restored = None
        if not CHDMAN:
            restored = restore_bundled_chdman()
            CHDMAN = find_chdman("")
        self.chd_version, self.has_dvd = chdman_info()
        self.build_ui()
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
        if initial_paths:
            self.root.after(400, lambda: self.add_paths([str(x) for x in initial_paths]))
        self.root.after(80, self.pump)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        if self.v["auto_update_check"].get() and not silent:
            self.root.after(1500, lambda: self.check_update_async(manual=False))
        if self.v["auto_app_check"].get() and not silent:
            self.root.after(2500, lambda: self.check_app_update_async(manual=False))

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
        self.chd_ctl = ttk.Frame(bar)
        ttk.Label(self.chd_ctl, text="CHD files:", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        self.chd_act_cb = ttk.Combobox(self.chd_ctl, state="readonly", width=11, textvariable=self.v["chd_action"],
                                       values=["Extract", "Verify", "Recompress"])
        self.chd_act_cb.pack(side="left")
        self.fmt_disp = tk.StringVar()
        self.chd_fmt_cb = ttk.Combobox(self.chd_ctl, state="readonly", width=28, textvariable=self.fmt_disp,
                                       values=list(FORMAT_LABELS.values()))
        self.chd_fmt_cb.bind("<<ComboboxSelected>>", self.on_format_selected)
        self.v["extract_format"].trace_add("write", lambda *a: (self.sync_fmt_disp(), self.update_action_labels()))

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
        for c, t, w, a in (("file", "File", 240, "w"), ("action", "Action", 150, "w"), ("platform", "Platform", 100, "w"),
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
                                    ".cue / .gdi / .iso / .toc  →  CHD (CD or DVD chosen for you)\n"
                                    ".chd  →  extract or verify")
        self.empty.place(relx=0.5, rely=0.45, anchor="center")
        self.tree.bind("<Double-1>", lambda e: self.info_selected())
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
        self.menu.add_command(label="This CHD: extract it", command=lambda: self.set_item_action("Extract"))
        self.menu.add_command(label="This CHD: verify only", command=lambda: self.set_item_action("Verify"))
        self.menu.add_command(label="This CHD: recompress it", command=lambda: self.set_item_action("Recompress"))
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

        for key in ("compression", "custom_comp", "hunk", "threads", "verify_after", "low_priority", "delete_src",
                    "exists", "out_mode", "out_dir", "auto_suggest"):
            self.v[key].trace_add("write", lambda *a: self.refresh_summary())
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
            row=7, column=0, columnspan=2, sticky="w")
        ttk.Label(f, style="Muted.TLabel", wraplength=560, justify="left",
                  text="Tip: 'Fast' uses zlib only, the safest pick if an emulator rejects a CHD "
                       "(NetherSX2/AetherSX2 are reported to need zlib). 'None' makes full-size files, and chdman can't verify an uncompressed CHD.").grid(
            row=8, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(f, text="Verify each CHD after creating it", variable=v["verify_after"]).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(f, text="Send source files to the Recycle Bin after a VERIFIED CHD",
                        variable=v["delete_src"], command=self.on_delete_toggle).grid(
            row=5, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(f, text="Auto-create a CUE for a lone .bin (assumes MODE2/2352, typical PS1)",
                        variable=v["auto_cue"]).grid(row=6, column=0, columnspan=2, sticky="w")

        # --- CHD files
        f = tab("CHD files")
        row(f, 0, "What to do with .chd files", ttk.Combobox(f, textvariable=v["chd_action"], width=12, state="readonly",
                                                              values=["Extract", "Verify", "Recompress"]))
        ttk.Label(f, text="When extracting, make:").grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 2))
        for i, (key, label) in enumerate(FORMAT_LABELS.items()):
            ttk.Radiobutton(f, text=label, variable=v["extract_format"], value=key).grid(
                row=2 + i, column=0, columnspan=2, sticky="w", padx=(14, 0))
        ttk.Checkbutton(f, text="Always use a subfolder (discs with several files get one automatically)", variable=v["extract_subfolder"]).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(f, style="Muted.TLabel", wraplength=560, justify="left",
                  text="Automatic reads the CHD: CD discs become .cue + .bin (one BIN per track when the disc has more than one track), Dreamcast GD-ROM discs become .gdi, DVD discs become .iso. "
                       "ISO works for data discs with a single track and refuses audio or multi-track discs rather than lose data. "
                       "Recompress writes a new CHD with the Convert page's compression and hunk size; your original is kept.").grid(
            row=7, column=0, columnspan=2, sticky="w", pady=10)
        v["chd_action"].trace_add("write", lambda *a: self.apply_chd_action())

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
                  text="Free and open source (0BSD license). chdman and the other bundled parts keep their own licenses, "
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
        st.configure("Treeview.Heading", background=t["head"], foreground=t["muted"], relief="flat", borderwidth=0,
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
        c.create_text(78, 47, text="Compress & convert disc images  ·  CD / DVD detected for you", anchor="w",
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

    # ------------------------------------------------------------------ what to do with CHD files
    def sync_fmt_disp(self):
        self.fmt_disp.set(FORMAT_LABELS.get(self.v["extract_format"].get(), FORMAT_LABELS["auto"]))

    def on_format_selected(self, event=None):
        back = {label: key for key, label in FORMAT_LABELS.items()}
        self.v["extract_format"].set(back.get(self.fmt_disp.get(), "auto"))

    def refresh_chd_controls(self):
        """The 'CHD files:' choices only appear when there is a CHD in the list, since that is the only
        case where the app can't know what you want."""
        has_chd = any(j.path.suffix.lower() == ".chd" for j in self.jobs)
        if has_chd and not self.chd_ctl.winfo_manager():
            self.chd_ctl.pack(side="right", padx=(0, 12))
        elif not has_chd and self.chd_ctl.winfo_manager():
            self.chd_ctl.pack_forget()
        extracting = self.v["chd_action"].get() == "Extract"
        if extracting and not self.chd_fmt_cb.winfo_manager():
            self.chd_fmt_cb.pack(side="left", padx=(8, 0))
        elif not extracting and self.chd_fmt_cb.winfo_manager():
            self.chd_fmt_cb.pack_forget()
        self.sync_fmt_disp()
        self.root.after(50, self.fit_toolbar)

    def wants_split(self, job, o=None):
        """One BIN per track: forced by the user, or automatically when a CD has more than one track."""
        fmt = (o or {}).get("extract_format") if o else self.v["extract_format"].get()
        split_opt = (o or {}).get("split_bin") if o else self.v["split_bin"].get()
        if fmt == "single":
            return False
        return bool(split_opt) or fmt == "split" or (fmt == "auto" and job.tracks > 1)

    def action_text_for(self, job):
        if job.kind == "convert":
            return "→ CHD"
        if job.kind == "verify":
            return "Verify"
        if job.kind == "recompress":
            return "Recompress"
        fmt = self.v["extract_format"].get()
        if job.disc == "GD-ROM":
            return "→ GDI"
        if job.disc == "DVD":
            return "→ ISO"
        if job.disc == "CD":
            return "→ ISO" if fmt == "iso" else "→ CUE/BIN (per track)" if self.wants_split(job) else "→ CUE/BIN"
        return "Extract"

    def update_action_labels(self):
        for j in self.jobs:
            j.action_text = self.action_text_for(j)
            self.refresh_job(j)
        self.refresh_chd_controls()

    def plan_text(self):
        c = collections_counter(self.jobs)
        parts = []
        if c.get("convert"):
            parts.append(f"{c['convert']} disc image(s) → CHD")
        if c.get("extract"):
            parts.append(f"{c['extract']} CHD file(s) → extract")
        if c.get("verify"):
            parts.append(f"{c['verify']} CHD file(s) → verify")
        if c.get("recompress"):
            parts.append(f"{c['recompress']} CHD file(s) → recompress")
        return "  ·  ".join(parts)

    def set_item_action(self, action):
        """Per-item override from the right-click menu."""
        for j in self.selected_jobs():
            if j.path.suffix.lower() == ".chd" and not j.status.startswith("Done"):
                j.task_override = action
                j.kind = KIND[action]
        self.update_action_labels()
        self.update_status_idle()

    def theme_menu_label(self):
        return "Switch to the light theme" if self.v["theme"].get() == "dark" else "Switch to the dark theme"

    def show_more_menu(self):
        self.more_menu.update_idletasks()
        x = self.more_btn.winfo_rootx() + self.more_btn.winfo_width() - self.more_menu.winfo_reqwidth()
        self.more_menu.tk_popup(max(x, 0), self.more_btn.winfo_rooty() + self.more_btn.winfo_height())

    def open_settings(self, tab="Convert"):
        w = self.settings_win
        if not getattr(self, "_settings_placed", False):
            w.geometry(f"780x580+{max(self.root.winfo_rootx() + 80, 0)}+{max(self.root.winfo_rooty() + 60, 0)}")
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
            parts.append("recycle originals after verify")
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
        d = {k: var.get() for k, var in self.v.items()}
        d["chdman"] = self.chd_path_var.get() if hasattr(self, "chd_path_var") else ""
        d["last_dir"] = self.settings.get("last_dir", "")
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
        self.root.destroy()

    def ask(self, msg):
        return getattr(self, "silent_answer", True) if self.silent else messagebox.askyesno(APP_NAME, msg)

    def on_delete_toggle(self):
        if self.v["delete_src"].get() and not self.silent:
            messagebox.showinfo(APP_NAME, "Source files will go to the Recycle Bin (so you can restore them), "
                                          "and only after the new CHD has been created AND verified. "
                                          "Verification is turned on automatically.")
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
            filetypes=[("Disc images / CHD", "*.cue *.gdi *.iso *.toc *.bin *.chd"), ("All files", "*.*")])
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
                allf = [Path(dp) / fn for dp, _, fns in os.walk(p) for fn in fns]
                referenced = set()
                for f in allf:
                    e = f.suffix.lower()
                    refs = parse_cue(f) if e == ".cue" else parse_gdi(f) if e == ".gdi" else parse_toc(f) if e == ".toc" else []
                    referenced.update(str(r).lower() for r in refs)
                ignored = {}
                for f in sorted(allf):
                    e = f.suffix.lower()
                    if e == ".chd" or e in (".cue", ".gdi", ".toc", ".iso"):
                        found.append((f, p))
                    elif e == ".bin" and str(f).lower() not in referenced:
                        found.append((f, p))
                    elif e in UNSUPPORTED_EXTS:
                        ignored[e] = ignored.get(e, 0) + 1
                if ignored:
                    self.log(f"Ignored {sum(ignored.values())} file(s) chdman can't read directly ({', '.join(sorted(ignored))}). "
                             "Convert .cdi/.nrg/.mds/.ccd images to .cue or .gdi first, and unpack archives.", "warn")
            elif p.suffix.lower() in SRC_EXTS | {".chd"}:
                found.append((p, None))
            else:
                self.log(f"Skipped (unsupported type): {p.name}", "warn")
        have = {str(j.path).lower() for j in self.jobs}
        was_empty = not self.jobs
        added = 0
        for f, root in found:
            if str(f).lower() in have:
                continue
            try:
                job = Job(f, root)
                job.analyze(self.v["chd_action"].get())
            except Exception as e:  # noqa: BLE001
                self.log(f"Could not read {f.name}: {e}", "err")
                continue
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
        if added and any(j.path.suffix.lower() == ".chd" for j in self.jobs):
            self.log("CHD files will be " + {"Extract": "extracted", "Verify": "verified", "Recompress": "recompressed"}.get(
                self.v["chd_action"].get(), "extracted") + ". Change this with the 'CHD files' choice at the top "
                     "(or right-click a file).", "info")
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
            self.tree.delete(iid)
        self.restripe()
        self.refresh_empty()
        self.update_status_idle()
        self.refresh_suggestions()
        self.refresh_chd_controls()

    def clear_all(self):
        if self.running:
            return
        self.tree.delete(*self.tree.get_children())
        self.tree_ids.clear()
        self.jobs.clear()
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

    def apply_chd_action(self):
        act = self.v["chd_action"].get()
        for j in self.jobs:
            if j.path.suffix.lower() == ".chd" and j.status in ("Queued",) and not j.task_override:
                j.kind = KIND.get(act, "extract")
        self.update_action_labels()
        self.update_status_idle()

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
        return o

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
        for j in todo:
            j.status, j.result = "Queued", ""
            self.refresh_job(j)
        self.save_settings()
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

    def output_path(self, job, o, ext, multi=False):
        stem = job.path.stem
        if o["out_mode"] == "custom" and o["out_dir"]:
            base = Path(o["out_dir"])
            if o["keep_structure"] and job.root:
                try:
                    base = base / job.path.parent.relative_to(job.root)
                except ValueError:
                    pass
        else:
            base = job.path.parent
        if job.kind == "extract" and (multi or o["extract_subfolder"]):
            base = base / stem
        base.mkdir(parents=True, exist_ok=True)
        return base / (stem + ext)

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

    def do_convert(self, job, o):
        if job.missing:
            job.status = "Failed (missing files)"
            self.post("log", f"{job.path.name}: missing {', '.join(job.missing)}", "err")
            return
        typ = job.type
        src, temp_cue = job.path, None
        if job.lone_bin:
            if not o["auto_cue"]:
                job.status = "No CUE"
                self.post("log", f"{job.path.name}: .bin without a CUE (enable auto-create in Settings, Convert page).", "err")
                return
            temp_cue = job.path.with_name(job.path.stem + ".__chdm_auto__.cue")
            temp_cue.write_text(f'FILE "{job.path.name}" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n', "utf-8")
            src = temp_cue
            self.post("log", f"{job.path.name}: created a temporary CUE (MODE2/2352).", "warn")
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
            if recycle(job.src_files):
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
        fmt = o.get("extract_format", "auto")
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

    def extract_to_iso(self, job, o):
        """CD CHD -> .iso. Only possible when the disc is one MODE1 data track; anything else would lose data."""
        out = self.output_path(job, o, ".iso")
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
            self.post("log", f"\n{job.path.name}  [CD → ISO]", "info")
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
            os.replace(part, out)
            ok = DiscReader(out, 2048, 0).sector(16)[1:6] == b"CD001"
            job.status = "Done"
            job.result = f"→ {out.name}" + ("" if ok else "  (no ISO 9660 header found - check it)")
            self.post("log", f"OK  {out}", "ok" if ok else "warn")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
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
            j.kind = "verify"
            self.refresh_job(j)
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


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "--relaunch-test":
        spawn_swap(write_swap_script(Path(sys.argv[2]), Path(sys.argv[3])))
        return
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
