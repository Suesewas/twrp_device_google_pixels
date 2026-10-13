#!/usr/bin/env python3
"""payload_range_fetch.py — pull ONE partition out of an OTA zip URL.

payload-dumper-go needs the whole local file. This mini analog does not:
it reads the zip central directory (tail), the payload.bin header and the
protobuf manifest (head) — ~300 KB over HTTP Range — then fetches ONLY the
data blobs of the requested partition and reassembles the image.

Works on FULL OTAs (REPLACE/BZ/XZ/ZERO/DISCARD ops). Delta-only ops
(SOURCE_COPY/BSDIFF/...) fail loudly: they need the old partition.

Needs nothing but stdlib (urllib + bz2 + lzma).

Example:
  ./tools/payload_range_fetch.py \
    --url "https://dl.google.com/dl/android/aosp/oriole-ota-sq1d.220205.004-36ba07e7.zip" \
    --partition vendor_boot --out /tmp/vb.img
"""
import argparse
import bz2
import lzma
import struct
import sys
import time
import urllib.request

BLOCK = 4096
OP_NAMES = {0: "REPLACE", 1: "REPLACE_BZ", 4: "SOURCE_COPY", 5: "SOURCE_BSDIFF",
            6: "ZERO", 7: "DISCARD", 8: "REPLACE_XZ", 9: "PUFFDIFF",
            10: "BROTLI_BSDIFF", 11: "ZUCCHINI", 12: "LZDIFF_BSDIFF",
            13: "LZDIFF_PUFFDIFF"}

import threading as _threading

_tls = _threading.local()


def set_log_sink(fn):
    """Redirect this thread's log() to fn(str). None restores stdout.

    Thread-local: worker threads under a curses TUI install a per-job
    collector so rangefetch chatter never lands on the shared screen;
    the CLI main thread keeps printing."""
    _tls.sink = fn


def log(msg):
    fn = getattr(_tls, "sink", None)
    if fn is not None:
        try:
            fn(f"[rangefetch] {msg}")
        except Exception:
            pass
        return
    print(f"[rangefetch] {msg}", flush=True)


def http_get(url, lo=None, hi=None, tries=4):
    """GET with optional Range. Returns bytes. Retries with backoff."""
    last = None
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(url)
            if lo is not None:
                req.add_header("Range", f"bytes={lo}-{hi}" if hi is not None else f"bytes={lo}-")
            with urllib.request.urlopen(req, timeout=60) as r:
                code = r.getcode()
                if lo is not None and code != 206:
                    raise IOError(f"server ignored Range (http {code})")
                return r.read()
        except Exception as e:
            last = e
            log(f"fetch {lo}-{hi} attempt {attempt}/{tries}: {e}")
            time.sleep(3 * attempt)
    raise SystemExit(f"fetch failed: {last}")


def head_size(url):
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=30) as r:
        ln = r.headers.get("Content-Length")
        rng = r.headers.get("Accept-Ranges")
    log(f"remote size: {int(ln)/1e9:.2f} GB, accept-ranges: {rng}")
    return int(ln)


# ---------------- zip central directory ----------------

def find_payload_offset(url, total):
    """Tail-read the zip EOCD + central directory, return payload.bin data offset."""
    tail_len = min(total, 1 << 20)
    tail = http_get(url, total - tail_len, total - 1)
    base = total - tail_len
    eocd = tail.rfind(b"PK\x05\x06")
    if eocd < 0:
        raise SystemExit("EOCD not found in last 1MB (not a zip?)")
    cd_count = struct.unpack("<H", tail[eocd + 10:eocd + 12])[0]
    cd_off = struct.unpack("<I", tail[eocd + 16:eocd + 20])[0]
    log(f"central directory: {cd_count} entries at {cd_off}")
    cd = http_get(url, cd_off, None)  # to EOF; urllib stops at content end
    # central directory ends where EOCD starts; trim
    pos, found = 0, None
    while pos + 46 <= len(cd):
        if cd[pos:pos + 4] != b"PK\x01\x02":
            break
        fnlen = struct.unpack("<H", cd[pos + 28:pos + 30])[0]
        eflen = struct.unpack("<H", cd[pos + 30:pos + 32])[0]
        fclen = struct.unpack("<H", cd[pos + 32:pos + 34])[0]
        lho = struct.unpack("<I", cd[pos + 42:pos + 46])[0]
        name = cd[pos + 46:pos + 46 + fnlen].decode(errors="replace")
        if name == "payload.bin" or name.endswith("/payload.bin"):
            # local header: magic(4) + 26 fixed + fnlen + extralen
            lh = http_get(url, lho, lho + 30)
            if lh[:4] != b"PK\x03\x04":
                raise SystemExit("payload.bin local header magic mismatch")
            lfn = struct.unpack("<H", lh[26:28])[0]
            lex = struct.unpack("<H", lh[28:30])[0]
            found = lho + 30 + lfn + lex
            log(f"payload.bin data starts at zip offset {found}")
            break
        pos += 46 + fnlen + eflen + fclen
    if found is None:
        raise SystemExit("payload.bin not found in central directory")
    return found


# ---------------- minimal protobuf walk ----------------

def read_varint(buf, pos):
    shift, val = 0, 0
    while True:
        b = buf[pos]
        pos += 1
        val |= (b & 0x7f) << shift
        if not (b & 0x80):
            break
        shift += 7
    return val, pos


def pb_fields(buf):
    """Yield (field_no, ('v', int) | ('b', off, len))."""
    pos, out = 0, []
    while pos < len(buf):
        key, pos = read_varint(buf, pos)
        f, w = key >> 3, key & 7
        if w == 0:
            v, pos = read_varint(buf, pos)
            out.append((f, ("v", v)))
        elif w == 2:
            ln, pos = read_varint(buf, pos)
            out.append((f, ("b", pos, ln)))
            pos += ln
        elif w == 5:
            out.append((f, ("i32", pos)))
            pos += 4
        elif w == 1:
            out.append((f, ("i64", pos)))
            pos += 8
        else:
            raise SystemExit(f"unsupported protobuf wire type {w}")
    return out


def sub_blob(buf, val):
    _, off, ln = val
    return buf[off:off + ln]


def find_partition(manifest, wanted):
    for f, v in pb_fields(manifest):
        if f != 13:  # DeltaArchiveManifest.partitions
            continue
        blob = sub_blob(manifest, v)
        name = ""
        for sf, sv in pb_fields(blob):
            if sf == 1:  # PartitionUpdate.partition_name
                name = sub_blob(blob, sv).decode()
        if name == wanted:
            return blob
    raise SystemExit(f"partition '{wanted}' not in manifest")


def parse_ops(part_blob):
    ops = []
    for sf, sv in pb_fields(part_blob):
        if sf != 8:  # PartitionUpdate.operations
            continue
        ob = sub_blob(part_blob, sv)
        d = {f: v for f, v in pb_fields(ob)}
        typ = d[1][1]
        do = d.get(2, ("v", 0))[1]
        dl = d.get(3, ("v", 0))[1]
        dst = []
        for f, v in pb_fields(ob):
            if f != 6:  # InstallOperation.dst_extents
                continue
            eb = sub_blob(ob, v)
            ed = {ef: ev for ef, ev in pb_fields(eb)}
            dst.append((ed[1][1], ed[2][1]))  # (start_block, num_blocks)
        ops.append({"type": typ, "off": do, "len": dl, "dst": dst})
    return ops


# ---------------- main ----------------

def fetch_manifest(url):
    """Discovery over HTTP Range (~300 KB): zip size, payload offset,
    manifest bytes, block size. Returns dict (no partition data)."""
    total = head_size(url)
    p_off = find_payload_offset(url, total)
    hdr = http_get(url, p_off, p_off + 24)
    if hdr[:4] != b"CrAU":
        raise SystemExit("payload magic mismatch (not CrAU)")
    ver = struct.unpack(">Q", hdr[4:12])[0]
    msize = struct.unpack(">Q", hdr[12:20])[0]
    ssize = struct.unpack(">I", hdr[20:24])[0]
    log(f"payload v{ver}, manifest {msize} bytes, sig {ssize} bytes")
    manifest = http_get(url, p_off + 24, p_off + 24 + msize - 1)
    if len(manifest) != msize:
        raise SystemExit("short manifest read")
    bs = 4096
    for f, v in pb_fields(manifest):
        if f == 3:
            bs = v[1]
    return {"total": total, "p_off": p_off, "msize": msize, "ssize": ssize,
            "manifest": manifest, "bs": bs,
            "data_start": p_off + 24 + msize + ssize}


def list_partitions(manifest):
    names = []
    for f, v in pb_fields(manifest):
        if f != 13:
            continue
        blob = sub_blob(manifest, v)
        for sf, sv in pb_fields(blob):
            if sf == 1:
                names.append(sub_blob(blob, sv).decode())
    return names


def fetch_partition(url, partition, out_path, progress=None, _meta=None):
    """Pull ONE partition via ranges. progress(downloaded, expected, note)
    is called after every op blob (may be None). Returns
    {"output_bytes": N, "downloaded_bytes": M}. Reuses discovery via _meta
    (fetch_manifest result) when the caller already has it."""
    meta = _meta or fetch_manifest(url)
    manifest, bs = meta["manifest"], meta["bs"]
    part = find_partition(manifest, partition)
    ops = parse_ops(part)
    from collections import Counter
    log(f"{partition}: {len(ops)} ops "
        f"{dict(Counter(OP_NAMES.get(o['type'], o['type']) for o in ops))}, block {bs}")
    total_out = sum(n for o in ops for (_, n) in o["dst"]) * bs
    total_dl = sum(o["len"] for o in ops if o["type"] not in (6, 7))
    log(f"output {total_out/1e6:.1f} MB from {total_dl/1e6:.1f} MB download "
        f"(vs {meta['total']/1e9:.2f} GB full zip)")
    data_start = meta["data_start"]
    with open(out_path, "wb") as f:
        f.truncate(total_out)
        got = 0
        for i, o in enumerate(ops):
            t = o["type"]
            if t == 7:  # DISCARD
                continue
            if t == 6:  # ZERO
                for (sb, nb) in o["dst"]:
                    f.seek(sb * bs)
                    f.write(b"\0" * nb * bs)
                continue
            if t not in (0, 1, 8):
                raise SystemExit(f"op {i}: {OP_NAMES.get(t, t)} needs source image "
                                 f"(delta OTA) — mini tool handles full OTAs only")
            blob = http_get(url, data_start + o["off"],
                            data_start + o["off"] + o["len"] - 1)
            if t == 1:
                plain = bz2.decompress(blob)
            elif t == 8:
                plain = lzma.decompress(blob)
            else:
                plain = blob
            pos = 0
            for (sb, nb) in o["dst"]:
                chunk = plain[pos:pos + nb * bs]
                f.seek(sb * bs)
                f.write(chunk)
                pos += nb * bs
            got += o["len"]
            if progress:
                progress(got, total_dl, f"op {i+1}/{len(ops)} {OP_NAMES[t]}")
            else:
                log(f"op {i+1}/{len(ops)} {OP_NAMES[t]} "
                    f"{o['len']/1e6:.1f}MB -> done {got/1e6:.1f}/{total_dl/1e6:.1f} MB")
    log(f"wrote {out_path} ({total_out} bytes)")
    return {"output_bytes": total_out, "downloaded_bytes": got}


def main():
    ap = argparse.ArgumentParser(description="fetch one partition from an OTA URL via ranges")
    ap.add_argument("--url", required=True, help="OTA zip URL (dl.google.com ...)")
    ap.add_argument("--partition", default="vendor_boot")
    ap.add_argument("--out", default="",
                    help="output image (not needed with --list)")
    ap.add_argument("--list", action="store_true", help="list partitions in manifest, exit")
    args = ap.parse_args()

    meta = fetch_manifest(args.url)
    if args.list:
        for name in list_partitions(meta["manifest"]):
            print(name)
        return
    if not args.out:
        raise SystemExit("--out required (or use --list)")
    fetch_partition(args.url, args.partition, args.out, _meta=meta)


if __name__ == "__main__":
    main()
