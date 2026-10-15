#!/usr/bin/env python3
"""Install official client validators using checked-in versions and SHA256 pins."""
import argparse
import gzip
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import platform
import re
import tarfile
import time
import urllib.error
import urllib.request
import zipfile

LOCK_FILE = Path(__file__).with_name('client-cores.lock.json')
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
DOWNLOAD_ATTEMPTS = 3


def download(url):
    request = urllib.request.Request(url, headers={"User-Agent": "subscription-parser-setup"})
    for attempt in range(DOWNLOAD_ATTEMPTS):
        delay = 2 ** (attempt + 1)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read(MAX_DOWNLOAD_BYTES + 1)
            if len(data) > MAX_DOWNLOAD_BYTES:
                raise SystemExit('Client release asset exceeds the download size limit.')
            return data
        except urllib.error.HTTPError as error:
            # Ordinary 403/404 errors are not transient. An explicit Retry-After
            # can mark a 403 as throttling; never retry before that deadline.
            retry_after = (error.headers or {}).get('Retry-After')
            if error.code not in {429, 500, 502, 503, 504} and not (error.code == 403 and retry_after):
                raise SystemExit(f'Client release download failed: HTTP {error.code}.') from None
            if retry_after:
                if not retry_after.isdigit() or int(retry_after) > 60:
                    raise SystemExit('Release host requested a longer retry delay; retry the build later.') from None
                delay = max(delay, int(retry_after))
            elif error.code == 429:
                delay = 60
            reason = f'HTTP {error.code}'
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead):
            reason = 'network interruption'
        if attempt + 1 == DOWNLOAD_ATTEMPTS:
            raise SystemExit(f'Client release download failed after {DOWNLOAD_ATTEMPTS} attempts ({reason}).')
        print(f'Client release download interrupted ({reason}); retrying in {delay}s.', flush=True)
        time.sleep(delay)


def specifications(architecture):
    """Resolve immutable download inputs without querying GitHub's REST API."""
    lock = json.loads(LOCK_FILE.read_text())
    if lock.get('schema_version') != 1:
        raise SystemExit('Unsupported client core lock file version.')
    resolved = []
    for release in lock['releases']:
        asset = release['assets'][architecture]
        digest = asset.get('sha256')
        if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
            raise SystemExit('A valid pinned SHA256 is required for every client release asset.')
        url = f"https://github.com/{release['repository']}/releases/download/{release['version']}/{asset['name']}"
        resolved.append((release['binary'], release['version'], asset['name'], digest, url))
    return resolved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, default=Path(__file__).resolve().parent.parent / '.tools')
    args = parser.parse_args()
    if platform.system() != "Linux":
        raise SystemExit("This installer currently supports Linux arm64 and amd64.")
    architecture = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64"}.get(platform.machine())
    if not architecture:
        raise SystemExit("Unsupported architecture; install official validators in .tools manually.")
    destination = args.destination
    destination.mkdir(mode=0o700, exist_ok=True)
    for binary, version, asset_name, digest, url in specifications(architecture):
        data = download(url)
        if hashlib.sha256(data).hexdigest() != digest:
            raise SystemExit(f"Checksum mismatch for {asset_name}; installation stopped.")
        if asset_name.endswith(".tar.gz"):
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                member = next(m for m in archive.getmembers() if m.isfile() and Path(m.name).name == binary)
                content = archive.extractfile(member).read()
        elif asset_name.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                member = next(name for name in archive.namelist() if Path(name).name == binary)
                content = archive.read(member)
        else:
            content = gzip.decompress(data)
        temporary = destination / (binary + ".download")
        temporary.write_bytes(content)
        temporary.chmod(0o700)
        os.replace(temporary, destination / binary)
        print(f"Installed {binary} {version} (verified SHA256)", flush=True)


if __name__ == "__main__":
    main()
