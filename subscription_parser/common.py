"""Shared validation, private file storage and environment handling."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

MAX_BYTES = 12 * 1024 * 1024


class SubscriptionError(Exception):
    """An actionable error that does not disclose subscription credentials."""


class NeedsFullConfig(SubscriptionError):
    """A connection has settings which a portable VLESS link cannot preserve."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_limited(path: Path, limit=MAX_BYTES) -> bytes:
    with path.open("rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise SubscriptionError("subscription exceeds the 12 MiB limit")
    return data


def validate_device_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9=-]{10,64}", value):
        raise SubscriptionError("device ID must contain 10–64 letters, digits, '=' or '-'")
    return value


def load_environment(path: Path, *, inherit_process: bool = True) -> dict[str, str]:
    """Read literal .env assignments without executing or expanding their values."""
    values = {}
    try:
        content = read_limited(path).decode("utf-8-sig")
    except FileNotFoundError:
        content = ""
    except UnicodeError:
        raise SubscriptionError(".env must be UTF-8 text") from None
    for number, line in enumerate(content.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(
            r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
            r"(?:'([^']*)'|\"([^\"]*)\"|([^'\"#]*?))\s*(?:#.*)?", line)
        if not match:
            raise SubscriptionError(f"invalid .env assignment on line {number}")
        values[match[1]] = next(value for value in match.groups()[1:] if value is not None).strip()
    # Explicit process environment settings take precedence over the local file.
    if inherit_process:
        values.update(os.environ)
    return values


def host_port(address, port) -> tuple[str, int]:
    if not isinstance(address, str) or not address.strip() or any(c.isspace() for c in address):
        raise SubscriptionError("missing or invalid server address")
    address = address.strip("[]")
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        try:
            address = address.encode("idna").decode("ascii")
        except UnicodeError:
            raise SubscriptionError("invalid internationalized server name") from None
        if any(c in address for c in "/?#@:"):
            raise SubscriptionError("invalid server address")
    else:
        if ip.is_unspecified:
            raise SubscriptionError("provider returned a dummy server address")
        address = str(ip)
    if isinstance(port, bool) or not str(port).isdigit() or not 1 <= int(port) <= 65535:
        raise SubscriptionError("invalid server port")
    return address, int(port)


def valid_uuid(value) -> str:
    try:
        parsed = uuid.UUID(str(value))
        if parsed.int == 0:
            raise ValueError
        return str(parsed)
    except (ValueError, AttributeError):
        raise SubscriptionError("missing, redacted or invalid VLESS UUID") from None


def reject_extra(settings: dict, allowed: set[str], description: str) -> None:
    extra = [k for k, v in settings.items() if k not in allowed and v not in (None, False, "", [], {})]
    if extra:
        raise NeedsFullConfig(f"{description} requires full configuration: {', '.join(sorted(extra))}")


@contextmanager
def process_lock(path: Path):
    """Keep one application instance in control of the saved state."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as lock:
        if os.name == "posix":
            import fcntl
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SubscriptionError("another subscription_parser instance is already running") from None
        yield
