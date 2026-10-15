#!/usr/bin/env python3
"""Install pinned official client validators; verify each release asset's SHA256."""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import tarfile
import urllib.request
import zipfile


def download(url):
    request = urllib.request.Request(url, headers={"User-Agent": "subscription-parser-setup"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read(256 * 1024 * 1024)


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
    specifications = [
        ("mihomo", "MetaCubeX/mihomo", "v1.19.32", f"mihomo-linux-{architecture}-v1.19.32.gz"),
        ("hiddify-core", "hiddify/hiddify-core", "v5.0.0", f"hiddify-core-linux-{architecture}-glibc.tar.gz"),
        ("xray", "XTLS/Xray-core", "v26.3.27", "Xray-linux-arm64-v8a.zip" if architecture == "arm64" else "Xray-linux-64.zip"),
    ]
    for binary, repository, version, asset_name in specifications:
        metadata = json.loads(download(f"https://api.github.com/repos/{repository}/releases/tags/{version}"))
        asset = next((asset for asset in metadata["assets"] if asset["name"] == asset_name), None)
        if not asset or not str(asset.get("digest", "")).startswith("sha256:"):
            raise SystemExit(f"Official SHA256 metadata unavailable for {asset_name}; installation stopped.")
        data = download(asset["browser_download_url"])
        if "sha256:" + hashlib.sha256(data).hexdigest() != asset["digest"]:
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
