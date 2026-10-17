"""Fetch subscriptions with the administrator's fixed, authorized HWID."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from .common import MAX_BYTES, SubscriptionError, now

WORKER_COMMAND = (sys.executable, '-m', 'subscription_parser.fetch', '--fetch')


def bounded_fetch_subscription(url, hwid, user_agent, timeout):
    """Bound DNS, redirects, retries and slow response bodies by one deadline.

    Credentials travel over stdin, never command-line arguments or core logs.
    Killing the child also releases its sockets; timed-out threads cannot pile up.
    """
    payload = json.dumps([url, hwid, user_agent, min(timeout, 10)]).encode()
    try:
        response = subprocess.run(WORKER_COMMAND, input=payload, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, timeout=timeout,
                                  cwd=Path(__file__).resolve().parent.parent)
        if response.returncode:
            raise SubscriptionError('subscription download worker failed')
        value = json.loads(response.stdout)
        if 'error' in value:
            raise SubscriptionError(value['error'])
        body = base64.b64decode(value['body'], validate=True)
        if len(body) > MAX_BYTES:
            raise SubscriptionError('subscription exceeds the 12 MiB limit')
        return body, value['metadata']
    except subprocess.TimeoutExpired:
        raise SubscriptionError('subscription download exceeded its total time limit') from None
    except (OSError, ValueError, KeyError, TypeError):
        raise SubscriptionError('subscription download worker failed') from None


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


if __name__ == '__main__':
    if sys.argv[1:] != ['--fetch']:
        sys.exit(1)
    try:
        body, metadata = fetch_subscription(*json.loads(sys.stdin.buffer.read(16384)))
        print(json.dumps({'body': base64.b64encode(body).decode(), 'metadata': metadata}))
    except SubscriptionError as error:
        print(json.dumps({'error': str(error)}))
    except Exception:
        sys.exit(1)
