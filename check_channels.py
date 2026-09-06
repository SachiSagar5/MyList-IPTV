#!/usr/bin/env python3
"""Validate all channels in .m3u files in the current folder.

Checks each stream URL for a valid HTTP response and optional live HLS
playlist content. Prints a status table and a summary.
"""

import argparse
import concurrent.futures
import pathlib
import socket
import sys
import urllib.error
import urllib.request

TIMEOUT = 10
MAX_WORKERS = 20


def parse_m3u(path):
    """Parse an m3u file into a list of (title, url) dicts.

    Entries with no URL (e.g. dead links commented out) are kept with
    url=None so they can be reported as NOURL.
    """
    channels = []
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    def flush():
        nonlocal title
        if title is not None:
            channels.append({"title": title, "url": None})
            title = None

    title = None
    for line in lines:
        if line.startswith("#EXTINF"):
            flush()
            # extract the title after the last comma
            title = line.split(",", 1)[1].strip() if "," in line else None
        elif line.startswith("#"):
            continue
        elif line.strip():
            channels.append({"title": title or "Untitled", "url": line.strip()})
            title = None
    flush()
    return channels


def check_channel(ch):
    url = ch["url"]
    if not url:
        return {**ch, "status": "NOURL", "detail": "missing stream URL"}
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        )
    }
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            status = resp.status
            ctype = resp.headers.get("Content-Type", "")
            body = resp.read(4096).decode("utf-8", errors="replace")

        if status >= 400:
            return {**ch, "status": "ERROR", "detail": f"HTTP {status}"}
        if "mpegurl" in ctype or body.lstrip().startswith("#EXTM3U"):
            # HLS playlist: acceptable if it produced a valid header
            if body.lstrip().startswith("#EXTM3U"):
                return {**ch, "status": "WORKING", "detail": f"HTTP {status} HLS"}
            return {**ch, "status": "DEAD", "detail": f"HTTP {status} no playlist"}
        if is_video_direct(status, ctype):
            return {**ch, "status": "WORKING", "detail": f"HTTP {status} {ctype}"}
        return {**ch, "status": "UNKNOWN", "detail": f"HTTP {status} {ctype}"}
    except urllib.error.HTTPError as e:
        return {**ch, "status": "ERROR", "detail": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        return {**ch, "status": "DEAD", "detail": f"{reason}"}
    except (TimeoutError, socket.timeout):
        return {**ch, "status": "TIMEOUT", "detail": "timed out"}
    except Exception as e:
        return {**ch, "status": "ERROR", "detail": f"{type(e).__name__}: {e}"}


def is_video_direct(status, ctype):
    return (
        status < 400
        and any(
            k in ctype
            for k in ("video", "mp4", "mpeg", "octet-stream", "x-matroska", "quicktime")
        )
    )


def print_table(results, only_errors=False):
    """Print a clean aligned table including the channel URL."""
    status_order = ["WORKING", "UNKNOWN", "TIMEOUT", "ERROR", "DEAD", "NOURL"]
    statuses = status_order if not only_errors else ["ERROR", "TIMEOUT", "DEAD", "NOURL"]
    rows = [r for st in statuses for r in results if r["status"] == st]

    cols = {"No": 4, "Status": 8, "Detail": 18, "Title": 30, "URL": 0}
    header = f"{'No':<4} {'Status':<8} {'Detail':<18} {'Title':<30} URL"
    print(header)
    print("-" * len(header))
    for i, r in enumerate(rows, 1):
        title = r["title"][:30] if r["title"] else ""
        url = r["url"] or ""
        print(f"{i:<4} {r['status']:<8} {r['detail']:<18} {title:<30} {url}")
    print("-" * len(header))
    return rows


def prune_m3u(path, results, remove_statuses):
    """Rewrite `path`, dropping every channel whose status is in remove_statuses."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    starts = [i for i, l in enumerate(lines) if l.startswith("#EXTINF")]

    blocks = []
    for n, start in enumerate(starts):
        end = (starts[n + 1] - 1) if n + 1 < len(starts) else len(lines) - 1
        blocks.append((start, end))

    if len(blocks) != len(results):
        raise SystemExit(
            f"Mismatch: {len(blocks)} playlist entries vs {len(results)} results in {path}"
        )

    kept, cursor = [], 0
    for (start, end), res in zip(blocks, results):
        kept.extend(lines[cursor:start])
        if res["status"] not in remove_statuses:
            kept.extend(lines[start:end + 1])
        cursor = end + 1
    kept.extend(lines[cursor:])

    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    removed = sum(1 for r in results if r["status"] in remove_statuses)
    print(f"Removed {removed} channels from {path.name} "
          f"({len(results)} -> {len(results) - removed})")


def validate_file(path, fmt="default", only_errors=False, remove_statuses=None):
    channels = parse_m3u(path)
    print(f"\nFound {len(channels)} channels in {path.name}\n")

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for res in ex.map(check_channel, channels):
            results.append(res)

    collapsed = {}
    for r in results:
        collapsed.setdefault(r["status"], []).append(r)

    if fmt == "table":
        print_table(results, only_errors=only_errors)
    else:
        status_order = ["WORKING", "UNKNOWN", "TIMEOUT", "ERROR", "DEAD", "NOURL"]
        for st in status_order:
            if only_errors and st not in ("ERROR", "TIMEOUT", "DEAD", "NOURL"):
                continue
            for r in collapsed.get(st, []):
                marker = {
                    "WORKING": "[OK]",
                    "UNKNOWN": "[?]",
                    "TIMEOUT": "[TO]",
                    "ERROR": "[ER]",
                    "DEAD": "[X]",
                    "NOURL": "[-]",
                }[st]
                print(f"{marker:>5} {r['status']:<8} {r['detail']:<35} {r['title'][:60]}")
                if r["url"]:
                    print(f"        {r['url']}")

    print("\n" + "=" * 70)
    status_order = ["WORKING", "UNKNOWN", "TIMEOUT", "ERROR", "DEAD", "NOURL"]
    for st in status_order:
        n = len(collapsed.get(st, []))
        print(f"{st:<8}: {n}")
    working = len(collapsed.get("WORKING", []))
    total = len(results)
    print("=" * 70)
    print(f"Working: {working}/{total} ({100*working//total if total else 0}%)")

    if remove_statuses:
        prune_m3u(path, results, remove_statuses)


def main():
    parser = argparse.ArgumentParser(description="Validate .m3u channel streams")
    parser.add_argument("files", nargs="*", help="Specific .m3u files to check")
    parser.add_argument("-t", "--timeout", type=int, default=10)
    parser.add_argument("-w", "--workers", type=int, default=20)
    parser.add_argument(
        "-f", "--format", choices=["default", "table"], default="default",
        help="Output format for results",
    )
    parser.add_argument(
        "--errors", action="store_true",
        help="Only show error/timeout/dead channels (with -f table)",
    )
    parser.add_argument(
        "--remove", action="store_true",
        help="Remove dead channels (ERROR/DEAD/NOURL) from the .m3u file",
    )
    parser.add_argument(
        "--remove-timeout", action="store_true",
        help="Also remove TIMEOUT channels (use with --remove)",
    )
    args = parser.parse_args()

    global TIMEOUT, MAX_WORKERS
    TIMEOUT = args.timeout
    MAX_WORKERS = args.workers

    remove_statuses = None
    if args.remove:
        remove_statuses = {"ERROR", "DEAD", "NOURL"}
        if args.remove_timeout:
            remove_statuses.add("TIMEOUT")

    if args.files:
        files = [pathlib.Path(f) for f in args.files]
    else:
        files = list(pathlib.Path(".").glob("*.m3u"))

    if not files:
        print("No .m3u files found in the current folder.")
        sys.exit(1)

    for f in files:
        if f.is_file():
            validate_file(
                f, fmt=args.format,
                only_errors=args.errors,
                remove_statuses=remove_statuses,
            )
        else:
            print(f"Skipping (not a file): {f}")


if __name__ == "__main__":
    main()
