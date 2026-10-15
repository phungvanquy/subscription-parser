"""Fetch subscriptions with the administrator's fixed, authorized HWID."""
from __future__ import annotations

import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from .common import MAX_BYTES, SubscriptionError, now


class HTTPSRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise SubscriptionError("refusing a subscription redirect to non-HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_subscription(url: str, hwid: str | None, user_agent: str, timeout: float) -> tuple[bytes, dict]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise SubscriptionError("subscription URL must be HTTPS without embedded login credentials")
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json,text/plain,*/*",
        "x-device-os": "Linux" if sys.platform.startswith("linux") else sys.platform,
        "x-device-model": "Subscription converter",
    }
    if hwid is not None:
        from .common import validate_device_id
        headers['x-hwid'] = validate_device_id(hwid)
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(HTTPSRedirects())
    for attempt in range(2):
        try:
            with opener.open(request, timeout=timeout) as response:
                headers = {k.lower(): v for k, v in response.headers.items()}
                if headers.get("x-hwid-not-supported", "").lower() == "true":
                    raise SubscriptionError("provider rejected the device ID header")
                if any(headers.get(k, "").lower() == "true" for k in
                       ("x-hwid-max-devices-reached", "x-hwid-limit")):
                    raise SubscriptionError("device limit reached; reuse an authorized HWID or free a device slot")
                data = response.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise SubscriptionError("subscription exceeds the 12 MiB limit")
                return data, {"http_status": response.status,
                              "content_type": headers.get("content-type", ""),
                              "fetched_at": now(), "bytes": len(data)}
        except urllib.error.HTTPError as exc:
            if attempt == 0 and (exc.code == 429 or exc.code >= 500):
                time.sleep(1)
                continue
            raise SubscriptionError(f"provider returned HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            if attempt == 0:
                time.sleep(1)
                continue
            raise SubscriptionError("subscription download failed (network, timeout or TLS error)") from None
    raise SubscriptionError("subscription download failed")
