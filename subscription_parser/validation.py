"""Use installed client cores to prevent a bad node from breaking a whole feed."""
from __future__ import annotations

from pathlib import Path
import os
import subprocess
import tempfile

import yaml

from . import common, protocols


def validate(target, document, root: Path):
    directory = Path(os.environ.get('PARSER_TOOLS_DIR') or root / '.tools')
    executable = directory / {"hiddify": "hiddify-core", "mihomo": "mihomo", "xray": "xray"}[target]
    if not executable.is_file():
        return None
    with tempfile.TemporaryDirectory(prefix="check-", dir=root / ".parser-state") as temporary:
        folder = Path(temporary)
        path = folder / ("config.yaml" if target == "mihomo" else "config.json")
        common.atomic_write(path, yaml.safe_dump(document, allow_unicode=True) if target == "mihomo" else common.json_text(document))
        if target == "hiddify":
            command = [str(executable), "parse", str(path), "-o", str(folder / "checked.json")]
        elif target == "mihomo":
            command = [str(executable), "-t", "-d", str(folder), "-f", str(path)]
        else:
            command = [str(executable), "run", "-test", "-config", str(path)]
        try:
            result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            raise common.SubscriptionError("client validator unavailable or timed out") from None


def checked_combine(entries, root, checker=validate):
    combined = protocols.combine(entries)
    checks = {}
    for target in protocols.TARGETS:
        if not combined["counts"][target]:
            checks[target] = {"status": "empty", "rejected": 0}
            continue
        result = checker(target, combined[target], root)
        if result is None:
            checks[target] = {"status": "not_installed", "rejected": 0}
            continue
        if result:
            checks[target] = {"status": "passed", "rejected": 0}
            continue
        # Narrow failed groups recursively; one malformed native node must not
        # prevent the other providers' valid profiles from being published.
        unique = {}
        for entry in entries:
            if target in entry.outputs and (entry.identity not in unique or entry.group_size < unique[entry.identity].group_size):
                unique[entry.identity] = entry
        rejected = set()
        def inspect(group, known_failed=False):
            if not group:
                return
            if not known_failed and checker(target, protocols.combine(group)[target], root):
                return
            if len(group) == 1:
                rejected.add(group[0].identity)
            else:
                midpoint = len(group) // 2
                inspect(group[:midpoint])
                inspect(group[midpoint:])
        inspect(list(unique.values()), known_failed=True)
        for entry in entries:
            if entry.identity in rejected:
                entry.outputs.pop(target, None)
                entry.issues[target] = "client core rejected this configuration; original settings remain in the provider cache"
        combined = protocols.combine(entries)
        if combined["counts"][target] and not checker(target, combined[target], root):
            raise common.SubscriptionError("combined subscription validation failed")
        checks[target] = {"status": "passed" if combined["counts"][target] else "empty", "rejected": len(rejected)}
    combined["validation"] = checks
    return combined
