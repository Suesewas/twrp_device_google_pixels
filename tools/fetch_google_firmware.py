#!/usr/bin/env python3
"""fetch_google_firmware.py — overnight fetcher for official Google firmware.

Google publishes no directory listing on dl.google.com and no static index:
the factory/OTA tables render via JS behind a ToS-ack wall. So this script
drives the locally installed Chrome headless (via playwright), clicks the
ack, and reads the tables — two page loads total, zero hammering. All bytes
afterwards come straight from the official dl.google.com CDN.

Tables parsed (both pages share the row shape):
  <tr id="<device><build>"><td>version</td><td><a href="dl.google.com...">Link</a></td><td>sha256</td></tr>

  factory: https://developers.google.com/android/images
  ota:     https://developers.google.com/android/ota

Modes:
  * TTY        — interactive tree: pick devices/firmwares, then s/d.
                 s asks modules --out, fetch method (full/surgical) and
                 thread count; every finished file is piped into
                 tools/extract_touch_modules.py automatically, sources
                 deleted. d downloads zips only.
  * --dry-run  — list only (version/url/sha), download nothing.
  * no TTY     — headless queue: --mode full|dl, --fetch-method
                 full|surgical, --jobs N; plain text progress.

Usage:
  ./tools/fetch_google_firmware.py --outdir /mnt/disk2/fw --extract-out /mnt/disk2/mods
  ./tools/fetch_google_firmware.py --devices yogi,cubs --kind ota --latest-only --dry-run
  nohup ./tools/fetch_google_firmware.py --devices all --outdir /mnt/disk2/fw \
      --extract-out /mnt/disk2/mods > fw.log 2>&1 &   # headless (no TUI there)

Needs: pip install playwright (uses system google-chrome, no browser download).
"""
import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request

TREE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTRACT_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "extract_touch_modules.py")
PAGES = {
    "factory": "https://developers.google.com/android/images?hl=en",
    "ota": "https://developers.google.com/android/ota?hl=en",
}
DEFAULT_CACHE = os.path.join(os.path.expanduser("~"), ".cache",
                             "fetch_google_firmware_index.json")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_WORKERS = 4
DEFAULT_WORKERS = MAX_WORKERS


def log(msg):
    print(f"[fwfetch] {msg}", flush=True)


# ---------------- listing (unchanged mechanics) ----------------

def tree_devices():
    devdir = os.path.join(TREE_ROOT, "devices")
    if os.path.isdir(devdir):
        return sorted(d for d in os.listdir(devdir)
                      if os.path.isdir(os.path.join(devdir, d)))
    return []


def device_model(dev):
    try:
        data = json.load(open(os.path.join(TREE_ROOT, "devices", dev, "pixel.json")))
        return data.get("props", {}).get("ro.product.system.model", dev)
    except (OSError, ValueError):
        return dev


def device_family(dev):
    try:
        data = json.load(open(os.path.join(TREE_ROOT, "devices", dev, "pixel.json")))
        return data.get("family", "?")
    except (OSError, ValueError):
        return "?"


def collect_rows(pw, url):
    browser = pw.chromium.launch(
        channel="chrome", headless=True,
        args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
    try:
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(8000)
        for _ in range(3):
            try:
                btns = page.locator("button.devsite-acknowledgement-link")
                clicked = False
                for i in range(btns.count()):
                    b = btns.nth(i)
                    if b.is_visible(timeout=3000):
                        b.click(timeout=8000)
                        clicked = True
                if clicked:
                    page.wait_for_timeout(5000)
                else:
                    break
            except Exception:
                break
        try:
            page.wait_for_selector("a[href*='dl.google.com']", timeout=30000)
        except Exception:
            return []
        return page.evaluate("""() => {
            const out = [];
            for (const tr of document.querySelectorAll('tr[id]')) {
                const a = tr.querySelector("a[href*='dl.google.com']");
                if (!a) continue;
                const tds = [...tr.querySelectorAll('td')].map(t => t.innerText.trim());
                const sha = tds.find(t => /^[0-9a-f]{64}$/.test(t)) || '';
                out.push({id: tr.id, version: tds[0] || '', url: a.href, sha});
            }
            return out;
        }""")
    finally:
        browser.close()


def device_of_row(row_id, wanted):
    rid = row_id.lower()
    best = ""
    for dev in wanted:
        if rid.startswith(dev.lower()) and len(dev) > len(best):
            best = dev
    return best


# ---------------- index cache ----------------
# The JS-rendered tables cost two headless-Chrome page loads. Cache the raw
# rows (per kind) as JSON; reuse until --new forces a refresh. playwright is
# only imported when a scrape is actually needed, so cached runs (incl.
# --dry-run) work without it.

def load_cache(path):
    try:
        with open(path) as f:
            data = json.load(f)
        pages = data.get("pages", {})
        if not isinstance(pages, dict):
            return None
        return {"saved_at": data.get("saved_at", 0), "pages": pages}
    except (OSError, ValueError):
        return None


def save_cache(path, pages):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump({"saved_at": time.time(), "pages": pages}, f)
    except OSError as e:
        log(f"cache save failed ({path}): {e}")


def cache_age_str(saved_at):
    dt = time.time() - saved_at
    if dt < 3600:
        return f"{int(dt // 60)} min ago"
    if dt < 86400:
        return f"{int(dt // 3600)} h ago"
    return f"{int(dt // 86400)} d ago"


def get_index(kinds, cache_path, force_new):
    """Return ({kind: [rows]}, source_str). Scrapes missing kinds only."""
    cached = None if force_new else load_cache(cache_path)
    pages = dict(cached["pages"]) if cached else {}
    need = [k for k in kinds if k not in pages]
    if need:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if pages:
                log(f"playwright missing and cache lacks {need} — "
                    f"continuing with cached kinds only")
                need = []
            else:
                raise SystemExit("need playwright: "
                                 "pip install --break-system-packages playwright")
        if need:
            with sync_playwright() as pw:
                for kind in need:
                    log(f"loading {kind} index: {PAGES[kind]}")
                    pages[kind] = collect_rows(pw, PAGES[kind])
                    log(f"{kind}: {len(pages[kind])} rows with links")
            save_cache(cache_path, pages)
            return {k: pages[k] for k in kinds if k in pages}, "fresh scrape"
    age = cache_age_str(cached["saved_at"]) if cached else "just now"
    return {k: pages[k] for k in kinds if k in pages}, f"cache ({age})"


# ---------------- download engine (stdlib, TUI-safe) ----------------

class Job:
    def __init__(self, kind, dev, ver, url, sha, dst, mode="full", fetch="full"):
        self.kind, self.dev, self.ver, self.url, self.sha = kind, dev, ver, url, sha
        self.dst = dst
        self.mode = mode  # "full": dl+extract+delete source; "dl": keep file
        self.fetch = fetch  # "full": whole zip; "surgical": OTA ranges (ota only)
        self.lock = threading.Lock()
        self.status = "queued"      # queued|active|done|failed|extracting|extracted
        self.total = 0              # 0 = unknown
        self.done = 0
        self.speed = 0.0            # bytes/s EMA
        self.err = ""
        self.extract_note = ""
        self.note = ""              # free-form progress note (surgical part name...)


def fmt_bytes(n):
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0


def fmt_speed(bps):
    return fmt_bytes(bps) + "/s" if bps else "--/s"


class Downloader:
    def __init__(self, limit_rate=0):
        self.limit_rate = limit_rate  # bytes/s, 0 = unlimited

    def _throttle(self, got, t0):
        if self.limit_rate and got > 0:
            want = got / self.limit_rate
            dt = time.monotonic() - t0
            if want > dt:
                time.sleep(min(want - dt, 2.0))

    def fetch(self, job):
        fname = os.path.basename(job.dst)
        for attempt in range(1, 4):
            try:
                return self._once(job)
            except (OSError, IOError) as e:
                with job.lock:
                    job.err = f"attempt {attempt}/3: {e}"
                time.sleep(5 * attempt)
        with job.lock:
            job.status = "failed"
            if not job.err:
                job.err = "3 attempts failed"
        return False

    def _once(self, job):
        part = job.dst + ".part"
        have = os.path.getsize(part) if os.path.isfile(part) else 0
        req = urllib.request.Request(job.url)
        if have:
            req.add_header("Range", f"bytes={have}-")
        with job.lock:
            job.status = "active"
        try:
            resp = urllib.request.urlopen(req, timeout=30)
        except Exception as e:
            raise IOError(f"connect: {e}")
        with resp:
            code = resp.getcode()
            if code == 206:
                cr = resp.headers.get("Content-Range", "")  # bytes 100-200/1234
                total = int(cr.split("/")[-1]) if "/" in cr else have
                mode, base, h = "ab", have, hashlib.sha256()
                if have:  # seed hash with existing prefix (re-read once)
                    with open(part, "rb") as f:
                        for chunk in iter(lambda: f.read(1 << 20), b""):
                            h.update(chunk)
            elif code == 200 and have:
                total, mode, base, h = 0, "wb", 0, hashlib.sha256()
                have = 0  # server ignored Range: restart
            elif code == 200:
                ln = resp.headers.get("Content-Length")
                total, mode, base, h = int(ln) if ln else 0, "wb", 0, hashlib.sha256()
            else:
                raise IOError(f"http {code}")
            with job.lock:
                job.total, job.done = total, base
            t0, win_t, win_b, last = time.monotonic(), time.monotonic(), base, base
            with open(part, mode) as f:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    base += len(chunk)
                    now = time.monotonic()
                    if now - win_t >= 1.0:
                        with job.lock:
                            job.done = base
                            job.speed = (base - win_b) / max(now - win_t, 1e-3)
                        win_t, win_b = now, base
                    self._throttle(base, t0)
                    last = base
            with job.lock:
                job.done, job.speed = base, 0.0
        if job.total and base < job.total:
            raise IOError(f"short read ({base}/{job.total})")
        if job.sha and SHA_RE.match(job.sha):
            if h.hexdigest() != job.sha:
                try:
                    os.remove(part)
                except OSError:
                    pass
                raise IOError("sha256 mismatch, .part deleted")
        os.rename(part, job.dst)
        with job.lock:
            job.status = "done"
        return True


def run_extractor(job, extract_out, extract_tmp, quiet, src="zip"):
    """Run extract_touch_modules.py on job.dst (src='zip') or on a fetched
    imgdir (src='imgdir': job.dst is the dir with *.img). src='zip' deletes
    the zip on success; src='imgdir' removes the whole fetched imgdir."""
    with job.lock:
        job.status = "extracting"
    logdir = (os.path.join(os.path.dirname(job.dst), "_extract_logs")
              if src == "zip" else os.path.join(job.dst, "_extract_logs"))
    try:
        os.makedirs(logdir, exist_ok=True)
        lg = open(os.path.join(logdir, os.path.basename(job.dst) + ".log"), "w")
    except OSError:
        lg = subprocess.DEVNULL
    if src == "zip":
        cmd = [sys.executable, EXTRACT_SCRIPT, "--zip", job.dst,
               "--out", extract_out, "--tmpdir", extract_tmp]
    else:
        cmd = [sys.executable, EXTRACT_SCRIPT, "--img", job.dst,
               "--out", extract_out, "--tmpdir", extract_tmp]
        if job.dev:
            cmd += ["--device", job.dev]
    try:
        r = subprocess.run(cmd, stdout=lg, stderr=subprocess.STDOUT, timeout=3600)
        ok = r.returncode == 0
    except Exception as e:  # timeout / spawn failure
        if lg is not subprocess.DEVNULL:
            lg.write(f"\nextractor launch failed: {e}\n")
        ok = False
    finally:
        if lg is not subprocess.DEVNULL:
            lg.close()
    with job.lock:
        if ok:
            # Modules are out: the source is dead weight. Delete it now
            # (not in a finally: a failed extract keeps everything for
            # manual retry) and free the slot for the next queued file.
            import shutil as _sh
            try:
                if src == "zip":
                    os.remove(job.dst)
                else:
                    _sh.rmtree(job.dst, ignore_errors=False)
                freed = True
            except OSError as e:
                freed = False
                job.err = f"source delete failed: {e}"
            job.status = "extracted" if freed else "extract-failed"
            job.extract_note = ("ok, source deleted" if freed
                                else "ok, source delete failed")
        else:
            job.status = "extract-failed"
            job.extract_note = "see _extract_logs (source KEPT)"
    return ok


# Partitions the module unpacker needs (extract_touch_modules WANT_PARTS).
SURGICAL_PARTS = ("vendor_dlkm", "vendor_kernel_boot", "vendor_boot", "system_dlkm")


def _prf():
    try:
        import payload_range_fetch as prf
        return prf
    except ImportError as e:
        raise SystemExit(f"surgical fetch needs payload_range_fetch.py next to this "
                         f"script: {e}")


def run_surgical(job, extract_out, extract_tmp):
    """OTA range path: fetch only SURGICAL_PARTS as *.img into a per-job
    dir, run the extractor in --img mode, delete the dir. No full zip ever
    lands on disk. OTA kind only; factory zips are not payload zips.

    TUI-SAFE: rangefetch chatter is collected into this job's own buffer
    (thread-local sink) instead of stdout, which curses owns. The buffer
    is written to a log file only if the job fails."""
    prf = _prf()
    lines = []
    prf.set_log_sink(lines.append)
    try:
        return _run_surgical_inner(job, extract_out, extract_tmp, prf, lines)
    finally:
        prf.set_log_sink(None)


def _surgical_log_path(job, extract_tmp):
    base = os.path.basename(job.dst) + ".rangefetch.log"
    if extract_tmp:
        d = os.path.join(extract_tmp, "surgical")
    else:
        d = os.path.join(os.path.dirname(job.dst), "_extract_logs")
    try:
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, base)
    except OSError:
        return None


def _dump_surgical_log(job, extract_tmp, lines):
    """Flush the job's collected rangefetch chatter to a file (failures
    only — successes stay silent)."""
    if not lines:
        return
    path = _surgical_log_path(job, extract_tmp)
    if path is None:
        return
    try:
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


def _run_surgical_inner(job, extract_out, extract_tmp, prf, lines):
    imgdir = os.path.join(extract_tmp, "surgical",
                          os.path.basename(job.dst) + ".imgs")
    os.makedirs(imgdir, exist_ok=True)
    with job.lock:
        job.status = "fetching-parts"
        job.dst = imgdir  # from now on the job IS the imgdir (for extractor+cleanup)
    try:
        meta = prf.fetch_manifest(job.url)
    except (SystemExit, Exception) as e:  # SystemExit is BaseException, not Exception
        with job.lock:
            job.status = "failed"
            job.err = f"manifest: {e}"
        _dump_surgical_log(job, extract_tmp, lines)
        return False
    names = set(prf.list_partitions(meta["manifest"]))
    missing = [p for p in SURGICAL_PARTS if p not in names]
    if missing:
        with job.lock:
            job.note = f"absent: {','.join(missing)}"
    with job.lock:
        job.total = 0  # unknown until first blob lands; progress is per-part
    dl_total, done = 0, 0
    for part in SURGICAL_PARTS:
        if part not in names:
            continue
        def _prog(got, exp, note, _p=[part]):
            with job.lock:
                job.note = f"{_p[0]} {note}"
                job.done = done + got
                job.total = dl_total + exp
        try:
            res = prf.fetch_partition(job.url, part, os.path.join(imgdir, f"{part}.img"),
                                      progress=_prog, _meta=meta)
        except (SystemExit, Exception) as e:  # incl. LZMAError on corrupt blobs
            with job.lock:
                job.status = "failed"
                job.err = f"{part}: {e}"
            _dump_surgical_log(job, extract_tmp, lines)
            return False
        dl_total += res["downloaded_bytes"]
        done = dl_total
    with job.lock:
        job.note = f"surgical traffic {dl_total/1e6:.1f}MB"
    if extract_out:
        return run_extractor(job, extract_out, extract_tmp, True, src="imgdir")
    with job.lock:
        job.status = "done"
    return True


# Job modes: 'full' = download + extract + delete zip, 'dl' = keep the file.
# Fetch paths: 'full' (whole zip) vs 'surgical' (OTA ranges, ota kind only).
def worker(jobs_iter, lock, downloader, extract_out, extract_tmp):
    while True:
        with lock:
            job = next((j for j in jobs_iter if j.status == "queued"), None)
            if job is None:
                return
            job.status = "active"
        # fetch() flips to done/failed itself. On success the same thread
        # immediately runs the extractor, then loops back for the next
        # queued file — the freed slot never idles while queue is non-empty.
        if job.mode == "dl":
            downloader.fetch(job)
        elif getattr(job, "fetch", "full") == "surgical" and job.kind == "ota":
            run_surgical(job, extract_out, extract_tmp)
        elif downloader.fetch(job) and extract_out:
            run_extractor(job, extract_out, extract_tmp, True)


# ---------------- TUI (curses, main thread only) ----------------

HELP = ("↑↓ move · Enter/→ expand · ← collapse · Space toggle · "
        "a: all files of device (full/partial→clear) · "
        "f: filter all→ota→factory · m: main builds only · "
        "s: start (asks --out, method, threads) · d: download only · q: quit")
FILTERS = (None, "ota", "factory")


def is_main_build(version):
    """Regional/carrier dupes carry an extra tag after the date
    (..., EMEA / Telia / VZW/US / TW, EM / ...). A main build has nothing
    there except the optional SB (bootloader-update) marker."""
    inner = version[version.find("(") + 1:version.rfind(")")] if "(" in version else version
    parts = [p.strip() for p in inner.split(",")]
    tail = parts[2:]
    return not tail or (len(tail) == 1 and tail[0] == "SB")


class Tui:
    def __init__(self, tree):
        # tree: [(dev, model, family, [row...])], rows newest-first
        self.tree = tree
        self.expanded = set()
        self.sel = {}          # (kind, dev, url) -> True
        self.cursor = 0
        self.top = 0
        self.phase = "browse"  # browse | running
        self.jobs = []
        self.stop = False
        self.filter_kind = None  # carousel: None -> "ota" -> "factory"
        self.main_only = False  # dupes (regional/carrier tags) hidden

    # ----- browse model -----
    def shown_items(self, items):
        out = items
        if self.filter_kind is not None:
            out = [it for it in out if it["kind"] == self.filter_kind]
        if self.main_only:
            out = [it for it in out if is_main_build(it["version"])]
        return out

    def visible(self):
        rows = []
        for dev, model, fam, items in self.tree:
            shown = self.shown_items(items)
            if not shown:
                continue  # nothing visible under an active filter: hide row
            rows.append(("dev", dev, None))
            if dev in self.expanded:
                for it in shown:
                    rows.append(("file", dev, it))
        return rows

    def dev_state(self, dev, items):
        n = sum(1 for it in items if self.sel.get((it["kind"], dev, it["url"])))
        if n == 0:
            return "[ ]"
        if n == len(items):
            return "[x]"
        return "[~]"

    def toggle_device_files(self, dev, items, to=None):
        """Rule: none->select all; full/partial->clear all."""
        any_sel = any(self.sel.get((it["kind"], dev, it["url"])) for it in items)
        target = (not any_sel) if to is None else to
        for it in items:
            self.sel[(it["kind"], dev, it["url"])] = target
        self.sel = {k: v for k, v in self.sel.items() if v}

    # ----- run -----
    def selected_jobs(self, outdir, mode="full", fetch="full"):
        jobs = []
        for dev, _, _, items in self.tree:
            for it in items:
                if self.sel.get((it["kind"], dev, it["url"])):
                    dst = os.path.join(outdir, it["kind"], dev,
                                       os.path.basename(it["url"]).split("?")[0])
                    jobs.append(Job(it["kind"], dev, it["version"], it["url"], it["sha"],
                                    dst, mode=mode, fetch=fetch))
        return jobs

    def ask_extract_out(self, stdscr):
        """Blocking one-line prompt: --out for extract_touch_modules.py.
        Empty input = download-only. Esc aborts the start."""
        import curses
        curses.curs_set(1)
        curses.echo()
        h, w = stdscr.getmaxyx()
        prompt = "modules --out (extract_touch_modules.py; empty = download only): "
        buf = ""
        try:
            stdscr.addnstr(h - 1, 0, prompt, w - 1, curses.A_BOLD)
            stdscr.refresh()
            while True:
                ch = stdscr.getch()
                if ch in (10, 13, curses.KEY_ENTER):
                    break
                if ch == 27:  # Esc
                    buf = None
                    break
                if ch in (curses.KEY_BACKSPACE, 127, 8):
                    buf = buf[:-1]
                elif 32 <= ch < 127 and len(prompt) + len(buf) < w - 1:
                    buf += chr(ch)
                try:
                    stdscr.addnstr(h - 1, 0, prompt + buf + " ", w - 1, curses.A_BOLD)
                    stdscr.move(h - 1, min(len(prompt) + len(buf), w - 2))
                    stdscr.refresh()
                except curses.error:
                    pass
        finally:
            curses.noecho()
            curses.curs_set(0)
        return buf.strip() if buf is not None else None

    def _ask_line(self, stdscr, prompt, default=""):
        """Generic blocking prompt. Returns stripped str, '' allowed,
        None on Esc."""
        import curses
        curses.curs_set(1)
        curses.echo()
        h, w = stdscr.getmaxyx()
        buf = default
        try:
            stdscr.addnstr(h - 1, 0, prompt + buf, w - 1, curses.A_BOLD)
            stdscr.refresh()
            while True:
                ch = stdscr.getch()
                if ch in (10, 13, curses.KEY_ENTER):
                    break
                if ch == 27:  # Esc
                    return None
                if ch in (curses.KEY_BACKSPACE, 127, 8):
                    buf = buf[:-1]
                elif 32 <= ch < 127 and len(prompt) + len(buf) < w - 1:
                    buf += chr(ch)
                try:
                    stdscr.addnstr(h - 1, 0, prompt + buf + " ", w - 1, curses.A_BOLD)
                    stdscr.move(h - 1, min(len(prompt) + len(buf), w - 2))
                    stdscr.refresh()
                except curses.error:
                    pass
        finally:
            curses.noecho()
            curses.curs_set(0)
        return buf.strip()

    def ask_fetch_method(self, stdscr):
        """full = whole zip, surgical = OTA ranges only (ota kind)."""
        ans = self._ask_line(stdscr, "fetch method [full/surgical] (default full): ")
        if ans is None:
            return None
        ans = (ans or "full").lower()
        if ans.startswith("s"):
            return "surgical"
        return "full"

    def ask_workers(self, stdscr):
        ans = self._ask_line(stdscr,
                             f"parallel downloads [1-16] (default {DEFAULT_WORKERS}): ",
                             default=str(DEFAULT_WORKERS))
        if ans is None:
            return None
        try:
            n = int(ans or DEFAULT_WORKERS)
        except ValueError:
            n = DEFAULT_WORKERS
        return max(1, min(16, n))

    def run(self, stdscr, outdir, extract_out, extract_tmp, limit_rate):
        import curses
        curses.curs_set(0)
        stdscr.timeout(100)
        stdscr.keypad(True)
        while not self.stop:
            if self.phase == "browse":
                self.draw_browse(stdscr)
                self.handle_browse(stdscr, outdir, extract_out, extract_tmp, limit_rate)
            else:
                self.draw_running(stdscr, extract_out)
                k = stdscr.getch()
                if k in (ord("q"), ord("Q")):
                    self.stop = True
                if all(j.status in ("done", "failed", "extracted", "extract-failed")
                       for j in self.jobs):
                    self.draw_running(stdscr, extract_out)
                    stdscr.getch()  # hold final screen until a key
                    self.stop = True

    # ----- browse view -----
    def draw_browse(self, stdscr, rows=None):
        import curses
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        stdscr.addnstr(0, 0, HELP, w - 1, curses.A_DIM)
        nsel = sum(1 for v in self.sel.values() if v)
        flt = self.filter_kind or "all"
        if self.main_only:
            flt += " · main-only"
        stdscr.addnstr(1, 0, f"selected files: {nsel} · filter: {flt} "
                             "(s = download + extract)", w - 1,
                       curses.A_BOLD)
        vis = rows if rows is not None else self.visible()
        if self.cursor >= len(vis):
            self.cursor = max(0, len(vis) - 1)
        if self.cursor < self.top:
            self.top = self.cursor
        if self.cursor >= self.top + h - 4:
            self.top = self.cursor - (h - 5)
        for i in range(self.top, min(len(vis), self.top + h - 3)):
            typ, dev, it = vis[i]
            if typ == "dev":
                for d, model, fam, items in self.tree:
                    if d == dev:
                        break
                shown = self.shown_items(items)
                mark = self.dev_state(dev, shown)
                arrow = "▼" if dev in self.expanded else "▶"
                line = f"{arrow} {mark} {model} ({dev}) · {fam} · {len(shown)} firmwares"
            else:
                mark = "[x]" if self.sel.get((it["kind"], dev, it["url"])) else "[ ]"
                sha = (it["sha"][:12] + "…") if it["sha"] else "no-sha"
                line = (f"    {mark} {it['version'][:44]:44} "
                        f"[{it['kind']:7}] {sha}")
            attr = curses.A_REVERSE if i == self.cursor else curses.A_NORMAL
            try:
                stdscr.addnstr(i - self.top + 3, 0, line, w - 1, attr)
            except curses.error:
                pass
        stdscr.refresh()

    def handle_browse(self, stdscr, outdir, extract_out, extract_tmp, limit_rate):
        import curses
        k = stdscr.getch()
        vis = self.visible()
        if not vis:
            if k in (ord("q"), ord("Q")):
                self.stop = True
            return
        self.cursor = max(0, min(self.cursor, len(vis) - 1))
        typ, dev, it = vis[self.cursor]
        items = self.shown_items(next(items for d, _, _, items in self.tree if d == dev))
        if k == curses.KEY_UP:
            self.cursor = max(0, self.cursor - 1)
        elif k == curses.KEY_DOWN:
            self.cursor = min(len(vis) - 1, self.cursor + 1)
        elif k in (curses.KEY_RIGHT, curses.KEY_ENTER, 10, 13) and typ == "dev":
            self.expanded.add(dev)
        elif k == curses.KEY_LEFT and typ == "dev":
            self.expanded.discard(dev)
        elif k == ord(" "):
            if typ == "file":
                key = (it["kind"], dev, it["url"])
                self.sel[key] = not self.sel.get(key)
                self.sel = {kk: vv for kk, vv in self.sel.items() if vv}
            else:
                self.toggle_device_files(dev, items)
        elif k in (ord("a"), ord("A")):
            self.toggle_device_files(dev, items)
        elif k in (ord("f"), ord("F")):
            idx = FILTERS.index(self.filter_kind)
            self.filter_kind = FILTERS[(idx + 1) % len(FILTERS)]
        elif k in (ord("m"), ord("M")):
            self.main_only = not self.main_only
        elif k in (ord("s"), ord("S")):
            # Full pipeline: ask modules --out, then fetch method, then
            # threads, then queue. Empty --out downgrades to download-only.
            out = self.ask_extract_out(stdscr)
            if out is None:
                pass  # Esc: back to browsing, selection kept
            elif out:
                method = self.ask_fetch_method(stdscr)
                if method is None:
                    pass  # Esc
                else:
                    workers = self.ask_workers(stdscr)
                    if workers is None:
                        pass  # Esc
                    else:
                        jobs = self.selected_jobs(outdir, mode="full", fetch=method)
                        if jobs:
                            self.start_jobs(jobs, out, extract_tmp, limit_rate,
                                            workers=workers)
            else:
                jobs = self.selected_jobs(outdir, mode="dl")
                if jobs:
                    self.start_jobs(jobs, "", "", limit_rate,
                                    workers=DEFAULT_WORKERS)
        elif k in (ord("d"), ord("D")):
            jobs = self.selected_jobs(outdir, mode="dl")
            if jobs:
                self.start_jobs(jobs, "", "", limit_rate,
                                workers=DEFAULT_WORKERS)
        elif k in (ord("q"), ord("Q")):
            self.stop = True

    def start_jobs(self, jobs, extract_out, extract_tmp, limit_rate,
                     workers=4):
        for j in jobs:
            os.makedirs(os.path.dirname(j.dst), exist_ok=True)
        if extract_tmp:
            os.makedirs(extract_tmp, exist_ok=True)
        self.jobs = jobs
        self.workers = workers
        self.phase = "running"
        lock = threading.Lock()
        dl = Downloader(limit_rate=_parse_rate(limit_rate))
        for _ in range(min(max(1, workers), len(jobs))):
            threading.Thread(target=worker,
                             args=(jobs, lock, dl, extract_out, extract_tmp),
                             daemon=True).start()

    # ----- running view -----
    def draw_running(self, stdscr, extract_out):
        import curses
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        workers = getattr(self, "workers", DEFAULT_WORKERS)
        hdr = (f"queue ({workers} threads) — full: zip→extract→delete · "
               f"surgical: OTA ranges→extract→cleanup · q: quit"
               if extract_out else
               f"download-only queue ({workers} threads) — zips are kept · q: quit")
        stdscr.addnstr(0, 0, hdr, w - 1, curses.A_DIM)
        ok = sum(1 for j in self.jobs if j.status in ("done", "extracted", "extract-failed"))
        fail = sum(1 for j in self.jobs if j.status == "failed")
        xok = sum(1 for j in self.jobs if j.status == "extracted")
        stdscr.addnstr(1, 0, f"jobs: {ok}/{len(self.jobs)} done ({xok} extracted)"
                             f"{f' · {fail} FAILED' if fail else ''}", w - 1, curses.A_BOLD)
        row = 3
        for j in self.jobs:
            if row >= h - 1:
                break
            with j.lock:
                st, tot, dn, sp = j.status, j.total, j.done, j.speed
                note, err, xnote = j.note, j.err, j.extract_note
                mode = ("surgical" if j.fetch == "surgical" and j.kind == "ota"
                        else "full")
            name = f"[{j.kind}/{j.dev}:{mode}] {os.path.basename(j.dst)}"
            try:
                stdscr.addnstr(row, 0, name[:w - 1], w - 1)
                row += 1
                if row >= h - 1:
                    break
                bw = max(10, w - 42)
                tail = ""
                if st == "failed" and err:
                    tail = f" :: {err[:48]}"
                elif st == "extract-failed":
                    tail = f" :: extract {xnote}"
                elif note and st in ("fetching-parts", "extracting", "active"):
                    tail = f" :: {note[:48]}"
                if tot:
                    fill = int(bw * dn / tot)
                    bar = "█" * fill + "░" * (bw - fill)
                    pct = 100.0 * dn / tot
                    line = (f"{bar} {pct:5.1f}% {fmt_bytes(dn)}/{fmt_bytes(tot)} "
                            f"{fmt_speed(sp)} {st}{tail}")
                else:
                    line = (f"{'░' * bw} {fmt_bytes(dn)} {fmt_speed(sp)} {st}{tail}")
                attr = curses.A_DIM if st in ("done", "extracted") else curses.A_NORMAL
                stdscr.addnstr(row, 0, line[:w - 1], w - 1, attr)
                row += 1
            except curses.error:
                break
        stdscr.refresh()


def _parse_rate(s):
    if not s:
        return 0
    m = re.match(r"(?i)^\s*([\d.]+)\s*([kmg])?\s*$", s)
    if not m:
        return 0
    return int(float(m.group(1)) * {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30,
                                    None: 1}[m.group(2).lower() if m.group(2) else None])


# ---------------- headless queue (no TTY) ----------------

def run_headless(jobs, extract_out, extract_tmp, limit_rate, workers=DEFAULT_WORKERS):
    lock = threading.Lock()
    dl = Downloader(limit_rate=_parse_rate(limit_rate))
    for j in jobs:
        os.makedirs(os.path.dirname(j.dst), exist_ok=True)
    if extract_tmp:
        os.makedirs(extract_tmp, exist_ok=True)
    threads = [threading.Thread(target=worker, args=(jobs, lock, dl, extract_out, extract_tmp),
                                daemon=True)
               for _ in range(min(max(1, workers), len(jobs)))]
    for t in threads:
        t.start()
    shown = set()
    while any(t.is_alive() for t in threads):
        time.sleep(1.0)
        for j in jobs:
            with j.lock:
                key = (j.dst, j.status)
                line = (f"{os.path.basename(j.dst)}: {j.status} "
                        f"{fmt_bytes(j.done)}/{fmt_bytes(j.total) if j.total else 0} "
                        f"{fmt_speed(j.speed)}")
                note = j.note
            if j.status in ("failed", "extracted", "extract-failed") and key not in shown:
                shown.add(key)
                if note:
                    line += f" :: {note}"
                log(line + (f" :: {j.err}" if j.err else ""))
    for t in threads:
        t.join()
    ok = sum(1 for j in jobs if j.status in ("done", "extracted"))
    log(f"queue done: {ok}/{len(jobs)} ok, "
        f"{sum(1 for j in jobs if j.status == 'extracted')} extracted")


# ---------------- main ----------------

def main():
    ap = argparse.ArgumentParser(description="overnight fetcher for official Pixel firmware")
    ap.add_argument("--devices", default="tree",
                    help="'all', 'tree' (devices/ in this repo, default), or comma list")
    ap.add_argument("--kind", default="both", choices=("ota", "factory", "both"))
    ap.add_argument("--outdir", default="firmware",
                    help="root; files land in <outdir>/<kind>/<device>/")
    ap.add_argument("--extract-out", default="",
                    help="extract_touch_modules.py --out root (empty = skip extraction)")
    ap.add_argument("--extract-tmpdir", default="",
                    help="extractor scratch (default <outdir>/.extract-tmp)")
    ap.add_argument("--mode", default="full", choices=("full", "dl"),
                    help="headless queue mode: full = dl+extract+delete source, "
                         "dl = download only, keep files (default: full)")
    ap.add_argument("--fetch-method", default="full", choices=("full", "surgical"),
                    help="full = whole zip; surgical = OTA ranges only "
                         "(ota kind; factory always full)")
    ap.add_argument("--jobs", type=int, default=DEFAULT_WORKERS,
                    help=f"parallel downloads 1-16 (default {DEFAULT_WORKERS})")
    ap.add_argument("--latest-only", action="store_true",
                    help="only the newest row per device per kind")
    ap.add_argument("--dry-run", action="store_true", help="list only, download nothing")
    ap.add_argument("--no-tui", action="store_true", help="headless queue even on TTY")
    ap.add_argument("--limit-rate", default="", help="per-download cap, e.g. 5M")
    ap.add_argument("--new", action="store_true",
                    help="force index refresh (ignore cache, re-scrape pages)")
    ap.add_argument("--cache-file", default=DEFAULT_CACHE,
                    help=f"index cache path (default {DEFAULT_CACHE})")
    args = ap.parse_args()

    extract_out = args.extract_out or ""
    extract_tmp = args.extract_tmpdir or (os.path.join(args.outdir, ".extract-tmp")
                                          if extract_out else "")

    if args.devices == "all":
        wanted = None
    elif args.devices == "tree":
        wanted = tree_devices()
        if not wanted:
            raise SystemExit("no devices/ dir found and no --devices given")
    else:
        wanted = [d.strip() for d in args.devices.split(",") if d.strip()]
    kinds = ("ota", "factory") if args.kind == "both" else (args.kind,)

    from playwright.sync_api import sync_playwright
    per_dev = {}
    rows_by_kind, index_src = get_index(kinds, args.cache_file, args.new)
    log(f"index: {index_src}")
    for kind in kinds:
        rows = rows_by_kind.get(kind, [])
        if not rows:
            log(f"{kind}: no rows (not scraped and not cached)")
            continue
        log(f"{kind}: {len(rows)} rows with links")
        # Both index pages list builds oldest-first; display newest-first.
        for row in reversed(rows):
            dev = device_of_row(row["id"], wanted or [row["id"]])
            if wanted is not None and dev not in wanted:
                continue
            key = dev or row["id"]
            per_dev.setdefault(key, []).append({"kind": kind, **row})

    if not per_dev:
        raise SystemExit("nothing matched (devices? page layout changed?)")
    if args.latest_only:
        per_dev = {d: rs[:1] for d, rs in per_dev.items()}

    if args.dry_run:
        for dev in sorted(per_dev):
            for it in per_dev[dev]:
                print(f"{it['kind']:7} {dev:10} {it['version'][:42]:42} "
                      f"{os.path.basename(it['url'])}")
        return

    tree = [(d, device_model(d), device_family(d), per_dev[d]) for d in sorted(per_dev)]
    use_tui = sys.stdin.isatty() and not args.no_tui
    if use_tui:
        import curses
        app = Tui(tree)
        curses.wrapper(app.run, args.outdir, extract_out or "", extract_tmp,
                       args.limit_rate)
        ok = sum(1 for j in app.jobs if j.status in ("done", "extracted", "extract-failed"))
        log(f"session: {ok}/{len(app.jobs)} downloaded")
    else:
        workers = max(1, min(16, args.jobs))
        if args.fetch_method == "surgical" and not extract_out and args.mode == "full":
            raise SystemExit("surgical fetch without --extract-out is pointless "
                             "(nothing to keep) — use --mode dl or add --extract-out")
        jobs = []
        for dev, _, _, items in tree:
            for it in items:
                dst = os.path.join(args.outdir, it["kind"], dev,
                                   os.path.basename(it["url"]).split("?")[0])
                fetch = (args.fetch_method if it["kind"] == "ota" else "full")
                jobs.append(Job(it["kind"], dev, it["version"], it["url"], it["sha"], dst,
                                mode=args.mode, fetch=fetch))
        log(f"headless queue: {len(jobs)} files, {workers} workers, "
            f"mode={args.mode} fetch={args.fetch_method}")
        run_headless(jobs, extract_out or "", extract_tmp, args.limit_rate,
                     workers=workers)


if __name__ == "__main__":
    main()
