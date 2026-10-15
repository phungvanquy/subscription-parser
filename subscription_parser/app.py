"""ASGI dashboard and private client subscription endpoints."""
from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
import hmac
import os
from pathlib import Path
import threading
import time
from typing import Literal

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import common
from .config import admin_key
from .fetch import fetch_subscription
from .service import Manager, Busy

STATIC = Path(__file__).with_name("static")


class Login(BaseModel):
    token: str = Field(min_length=1, max_length=256)


class Provider(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(default="", max_length=4096)
    hwid_id: str | None = Field(default=None, min_length=1, max_length=100)
    user_agent: str = Field(default="Happ/2.3.1", max_length=200)
    enabled: bool = True
    test_token: str | None = Field(default=None, min_length=1, max_length=100)


class ProviderTest(Provider):
    provider_id: str | None = Field(default=None, min_length=1, max_length=100)


class HWID(BaseModel):
    name: str = Field(max_length=100)
    value: str = Field(max_length=64)


class DeviceName(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class DeviceReplacement(BaseModel):
    value: str = Field(min_length=10, max_length=64)
    revision: int = Field(ge=1)
    provider_ids: list[str] = Field(max_length=50)
    confirmed: bool = False


class Settings(BaseModel):
    refresh_seconds: int | None = Field(default=None, ge=300, le=86400)
    check_seconds: int | None = Field(default=None, ge=300, le=86400)


def create_app(root=None, start_scheduler=True, fetcher=fetch_subscription, checker=None):
    # Read once: recreating the container applies a changed deployment key.
    admin_token = admin_key()
    root = Path(root or os.environ.get('PARSER_DATA_DIR') or Path(__file__).resolve().parent.parent)
    public_url = os.environ.get('PARSER_PUBLIC_URL', '').strip().rstrip('/')
    if not (root / 'app.sqlite3').exists() and (root / '.env').exists():
        legacy = common.load_environment(root / '.env', inherit_process=False)
        if legacy.get('PARSER_PUBLIC_URL') and not public_url:
            raise common.SubscriptionError('set PARSER_PUBLIC_URL in deployment configuration before migrating')
    manager = Manager(root, fetcher, checker)
    attempts, attempt_lock = {}, threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        lock = common.process_lock(manager.store.directory / "app.lock")
        with lock:
            manager.logs.write('started')
            worker = None
            if start_scheduler:
                worker = threading.Thread(target=manager.scheduler, daemon=True, name="subscription-scheduler")
                worker.start()
            yield
            manager.stop.set()
            if worker:
                worker.join(timeout=3)
            if manager.worker:
                manager.worker.join(timeout=60)
            manager.logs.write('stopped')

    app = FastAPI(title="subscription_parser", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.manager = manager

    def secret():
        return admin_token

    def session_valid(request):
        bearer = request.headers.get("authorization", "")
        if bearer.startswith("Bearer ") and hmac.compare_digest(bearer[7:].encode(), secret().encode()):
            return True
        value = request.cookies.get("parser_session", "")
        stamp, _, signature = value.partition(".")
        if not stamp.isdigit() or not 0 <= time.time() - int(stamp) < 28800:
            return False
        expected = hmac.new(secret().encode(), stamp.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature.encode(), expected.encode())

    def admin(request: Request):
        if not session_valid(request):
            raise HTTPException(401, "sign in to manage subscriptions")

    @app.middleware("http")
    async def boundaries(request, call_next):
        try:
            if int(request.headers.get("content-length", "0")) > 16384:
                return JSONResponse({"detail": "request too large"}, status_code=413)
        except ValueError:
            return JSONResponse({"detail": "invalid request size"}, status_code=400)
        if request.method in {"POST", "PUT", "DELETE", "PATCH"}:
            body = b""
            async for chunk in request.stream():
                body += chunk
                if len(body) > 16384:
                    return JSONResponse({"detail": "request too large"}, status_code=413)
            request._body = body
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "cross-origin changes are not allowed"}, status_code=403)
        try:
            response = await call_next(request)
        except Exception as error:
            manager.logs.write('request_failed', error=error)
            response = JSONResponse({'detail': 'internal error; check the admin logs'}, status_code=500)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(common.SubscriptionError)
    async def subscription_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409 if isinstance(exc, Busy) else 400)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse({"detail": "invalid request fields; check names, URLs and limits"}, status_code=422)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/session")
    def session(request: Request):
        return {"authenticated": session_valid(request)}

    @app.post("/api/login")
    def login(body: Login, request: Request):
        client = request.client.host if request.client else "unknown"
        now = time.time()
        with attempt_lock:
            for key in list(attempts):
                attempts[key] = [stamp for stamp in attempts[key] if now - stamp < 300]
                if not attempts[key]:
                    del attempts[key]
            if len(attempts.get(client, [])) >= 10:
                raise HTTPException(429, "too many attempts; try again in five minutes")
            if not hmac.compare_digest(body.token.encode(), secret().encode()):
                attempts.setdefault(client, []).append(now)
                raise HTTPException(401, "incorrect admin key")
            attempts.pop(client, None)
        stamp = str(int(now))
        signature = hmac.new(secret().encode(), stamp.encode(), hashlib.sha256).hexdigest()
        response = JSONResponse({"authenticated": True})
        response.set_cookie("parser_session", stamp + "." + signature, httponly=True,
                            secure=request.url.scheme == "https", samesite="strict", max_age=28800)
        return response

    @app.post("/api/logout")
    def logout():
        response = JSONResponse({"authenticated": False})
        response.delete_cookie("parser_session")
        return response

    @app.get("/api/state", dependencies=[Depends(admin)])
    def state(request: Request):
        return manager.public_state(public_url or str(request.base_url))

    @app.get('/api/logs', dependencies=[Depends(admin)])
    def logs(level: Literal['all', 'warning', 'error'] = 'all'):
        return manager.logs.recent(level)

    @app.post("/api/providers", dependencies=[Depends(admin)], status_code=201)
    def add_provider(body: Provider):
        return manager.change_provider(body.model_dump(exclude_unset=True))

    @app.post('/api/providers/test', dependencies=[Depends(admin)])
    def test_provider(body: ProviderTest):
        return manager.test_provider(body.model_dump(exclude_unset=True), body.provider_id)

    @app.put("/api/providers/{identifier}", dependencies=[Depends(admin)])
    def edit_provider(identifier: str, body: Provider):
        return manager.change_provider(body.model_dump(exclude_unset=True), identifier)

    @app.delete("/api/providers/{identifier}", dependencies=[Depends(admin)])
    def delete_provider(identifier: str):
        manager.change_provider(provider_id=identifier, delete=True)
        return {"deleted": True}

    @app.post("/api/refresh", dependencies=[Depends(admin)], status_code=202)
    def refresh(provider_id: str | None = None):
        manager.begin_refresh(provider_id)
        return {"started": True}

    @app.post('/api/check-servers', dependencies=[Depends(admin)], status_code=202)
    def check_servers():
        manager.begin_check()
        return {'started': True}

    @app.post("/api/hwids", dependencies=[Depends(admin)], status_code=201)
    def add_hwid(body: HWID):
        return manager.store.add_hwid(body.name, body.value.strip())

    @app.post('/api/hwids/{identifier}/reveal', dependencies=[Depends(admin)])
    def reveal_hwid(identifier: str):
        return {'value': manager.store.hwid_value(identifier)}

    @app.patch('/api/hwids/{identifier}', dependencies=[Depends(admin)])
    def rename_hwid(identifier: str, body: DeviceName):
        manager.change_hwid(identifier, name=body.name)
        return {'saved': True}

    @app.delete('/api/hwids/{identifier}', dependencies=[Depends(admin)])
    def delete_hwid(identifier: str):
        manager.change_hwid(identifier, delete=True)
        return {'deleted': True}

    @app.post('/api/hwids/{identifier}/replace', dependencies=[Depends(admin)])
    def replace_hwid(identifier: str, body: DeviceReplacement):
        if not body.confirmed:
            raise common.SubscriptionError('confirm the affected subscriptions before replacing this device ID')
        manager.change_hwid(identifier, replacement=body.model_dump())
        return {'saved': True}

    @app.put("/api/settings", dependencies=[Depends(admin)])
    def settings(body: Settings):
        values = body.model_dump(exclude_none=True)
        if not values:
            raise common.SubscriptionError('provide a refresh or connection-check interval')
        manager.change_settings(values)
        return {"saved": True}

    @app.post("/api/rotate-link", dependencies=[Depends(admin)])
    def rotate_link():
        manager.store.rotate_subscription_token()
        return {"rotated": True}

    @app.get("/sub/{token}/{filename}")
    def subscription(token: str, filename: str):
        expected = manager.store.credential('subscription_token')
        if not hmac.compare_digest(token.encode(), expected.encode()) or filename not in {"hiddify.json", "mihomo.yaml", "xray.json", "v2board.txt", "combined.json"}:
            raise HTTPException(404, "subscription not found")
        with manager.guard:
            if not manager.publication_current(manager.snapshot):
                raise HTTPException(503, 'connection checks are pending or expired; check servers in the dashboard')
            content = manager.snapshot.get("exports", {}).get(filename)
            target = {"hiddify.json": "hiddify", "mihomo.yaml": "mihomo", "xray.json": "xray", "v2board.txt": "v2board"}.get(filename)
            if target and not manager.snapshot.get("counts", {}).get(target):
                raise HTTPException(503, "no recently passing servers available for this client; check the server library")
        if content is None:
            raise HTTPException(503, "subscription has not been refreshed yet")
        headers = {"Content-Disposition": f'inline; filename="{filename}"', "profile-update-interval": "1",
                   "profile-title": "subscription_parser"}
        media_type = "text/plain" if filename.endswith('.txt') else "application/yaml" if filename.endswith('.yaml') else "application/json"
        return Response(content, media_type=media_type, headers=headers)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get('/healthz')
    def health():
        return {'status': 'ok'}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    return app


def main():
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument('--data-dir', type=Path, help='directory containing the application database and cache')
    args = parser.parse_args()
    os.umask(0o077)
    uvicorn.run(create_app(root=args.data_dir), host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
