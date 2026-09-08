"""Resumable ranged download of ONE public HF GGUF, verified against LFS SHA256.

No tokens, remote code, repository clone or model execution. Final filenames are
published only after full hash verification. LM Studio can index the final GGUF.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import time
import threading
from urllib.parse import quote

import requests


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--workers", type=int, default=24)
    args = parser.parse_args()
    if len(args.repo.split("/")) != 2 or any(x in args.repo for x in ("..", "\\")):
        parser.error("Expected owner/repository")
    if Path(args.file).name != args.file or not args.file.lower().endswith(".gguf"):
        parser.error("Expected a single GGUF filename")
    model_root = Path(os.environ["USERPROFILE"]) / ".lmstudio" / "models"
    directory = model_root / args.repo
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.resolve().is_relative_to(model_root.resolve()):
        raise RuntimeError("Destination escaped model directory")
    target = directory / args.file
    if target.is_symlink():
        raise RuntimeError("Refusing a linked destination")
    api = requests.get(f"https://huggingface.co/api/models/{args.repo}/tree/main", timeout=30)
    api.raise_for_status()
    item = next(x for x in api.json() if x["path"] == args.file)
    size, sha = item["size"], item["lfs"]["oid"]
    if size > 4_000_000_000:
        raise RuntimeError("This weight file exceeds the user's 4 GB limit")
    if target.exists():
        if target.stat().st_size == size and digest(target) == sha:
            print("VERIFIED existing " + str(target), flush=True)
            return
        raise RuntimeError("Existing final file is not the expected model; not overwriting")
    parts = directory / (".jarvis-download-" + sha[:12])
    parts.mkdir(exist_ok=True)
    if parts.is_symlink():
        raise RuntimeError("Refusing a linked partial directory")
    resolve = f"https://huggingface.co/{args.repo}/resolve/main/{quote(args.file)}"
    url_state = {"url": None, "time": 0.0}
    url_lock = threading.Lock()

    def download_url(expired=None):
        # Large downloads on a slow connection can outlive the signed URL.
        # Refresh only the public HF redirect, never print signed credentials.
        with url_lock:
            stale = time.monotonic() - url_state["time"] > 1800
            if not url_state["url"] or stale or expired == url_state["url"]:
                head = requests.head(resolve, allow_redirects=True, timeout=30)
                if head.status_code >= 400:
                    raise RuntimeError(f"Download URL refresh failed: HTTP {head.status_code}")
                url_state.update(url=head.url, time=time.monotonic())
            return url_state["url"]

    download_url()
    chunk_size = 4 * 1024 * 1024
    count = (size + chunk_size - 1) // chunk_size
    local = threading.local()

    def fetch(index):
        if not hasattr(local, "session"):
            local.session = requests.Session()
        start = index * chunk_size
        end = min(size - 1, start + chunk_size - 1)
        path = parts / f"{index:06d}.part"
        checksum = path.with_suffix(".sha256")
        if path.is_symlink() or checksum.is_symlink():
            raise RuntimeError("Linked partial file")
        if (path.exists() and path.stat().st_size == end - start + 1 and checksum.exists()
                and digest(path) == checksum.read_text(encoding="ascii")):
            return path.stat().st_size, True
        for attempt in range(8):
            try:
                signed_url = download_url()
                with local.session.get(signed_url, headers={"Range": f"bytes={start}-{end}"},
                                  stream=True, timeout=(20, 30)) as response:
                    if response.status_code in (401, 403):
                        download_url(expired=signed_url)
                        continue
                    if response.status_code == 429:
                        time.sleep(min(30, int(response.headers.get("Retry-After", 10))))
                        continue
                    response.raise_for_status()
                    if response.status_code != 206 or response.headers.get("Content-Range") != f"bytes {start}-{end}/{size}":
                        raise RuntimeError("Server did not honor the exact byte range")
                    received, hasher = 0, hashlib.sha256()
                    with path.open("wb") as output:
                        for block in response.iter_content(256 * 1024):
                            received += len(block)
                            if received > end - start + 1:
                                raise RuntimeError("Range too large")
                            output.write(block)
                            hasher.update(block)
                    if received != end - start + 1:
                        raise RuntimeError("Incomplete range")
                    checksum.write_text(hasher.hexdigest(), encoding="ascii")
                    return received, False
            except requests.RequestException:
                if attempt == 7:
                    raise RuntimeError(f"Network retries exhausted for chunk {index}") from None
                time.sleep(min(8, attempt + 1))
        raise RuntimeError("Rate limit retries exhausted")

    done, transferred, started, last = 0, 0, time.monotonic(), 0
    print(f"DOWNLOAD {args.repo}/{args.file}: {size/1e9:.3f} GB; {count} ranges", flush=True)
    with ThreadPoolExecutor(max_workers=max(1, min(32, args.workers))) as pool:
        futures = [pool.submit(fetch, i) for i in range(count)]
        for future in as_completed(futures):
            amount, reused = future.result()
            done += amount
            if not reused:
                transferred += amount
            elapsed = time.monotonic() - started
            if elapsed - last > 10:
                print(f"PROGRESS {done/size:.1%} {done/1e6:.0f}/{size/1e6:.0f} MB new={transferred/max(1,elapsed)/1e6:.2f} MB/s", flush=True)
                last = elapsed
    assembled = parts / "verified-model.gguf.tmp"
    hasher = hashlib.sha256()
    with assembled.open("wb") as output:
        for i in range(count):
            with (parts / f"{i:06d}.part").open("rb") as source:
                for block in iter(lambda: source.read(chunk_size), b""):
                    hasher.update(block)
                    output.write(block)
    if assembled.stat().st_size != size or hasher.hexdigest() != sha:
        raise RuntimeError("Model SHA256 mismatch; partial files retained, no final model published")
    if target.exists():
        if digest(target) != sha:
            raise RuntimeError("Another download published a different file; not overwriting")
    else:
        assembled.rename(target)  # On Windows, fails rather than overwriting a raced target.
    (parts / "verified.json").write_text(json.dumps({"repo": args.repo, "file": args.file,
                                                  "size": size, "sha256": sha}), encoding="utf-8")
    print(f"VERIFIED {target} SHA256={sha}", flush=True)


if __name__ == "__main__":
    main()
