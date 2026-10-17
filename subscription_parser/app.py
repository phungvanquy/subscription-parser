"""ASGI dashboard and private client subscription endpoints."""
from __future__ import annotations

from contextlib import asynccontextmanager
import os
from pathlib import Path
import threading
from typing import Literal
from urllib.parse import urlsplit

from starlette.concurrency import run_in_threadpool

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import common, publication
from .config import MAX_PROVIDERS, MAX_PROVIDER_SERVERS, admin_key
from .auth import Auth, AuthError, SESSION_SECONDS
from .fetch import bounded_fetch_subscription
from .service import Manager, Busy

STATIC = Path(__file__).with_name("static")


class Login(BaseModel):
    token: str = Field(min_length=1, max_length=256)
    code: str = Field(default='', max_length=80)


class Verification(BaseModel):
    code: str = Field(min_length=1, max_length=80)


class Provider(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(default="", max_length=4096)
    hwid_id: str | None = Field(default=None, min_length=1, max_length=100)
    user_agent: str = Field(default="Happ/2.3.1", max_length=200)
    enabled: bool = True
    server_limit: int | None = Field(default=None, ge=1, le=MAX_PROVIDER_SERVERS, strict=True)
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
    provider_ids: list[str] = Field(max_length=MAX_PROVIDERS)
    confirmed: bool = False


class Settings(BaseModel):
    refresh_seconds: int | None = Field(default=None, ge=300, le=86400)
    check_seconds: int | None = Field(default=None, ge=300, le=86400)


class Subscription(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    all_providers: bool = False
    provider_ids: list[str] = Field(default_factory=list, max_length=MAX_PROVIDERS)


def create_app(root=None, start_scheduler=True, fetcher=bounded_fetch_subscription, checker=None):
    # Read once: recreating the container applies a changed deployment key.
    admin_token = admin_key()
    root = Path(root or os.environ.get('PARSER_DATA_DIR') or Path(__file__).resolve().parent.parent)
    public_url = os.environ.get('PARSER_PUBLIC_URL', '').strip().rstrip('/')
    if not (root / 'app.sqlite3').exists() and (root / '.env').exists():
        legacy = common.load_environment(root / '.env', inherit_process=False)
        if legacy.get('PARSER_PUBLIC_URL') and not public_url:
            raise common.SubscriptionError('set PARSER_PUBLIC_URL in deployment configuration before migrating')
    manager = Manager(root, fetcher, checker)
    auth = Auth(manager.store, admin_token)

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
    app.state.auth = auth

    def session_cookie(request):
        return request.cookies.get('parser_session', '')

    def session_valid(request):
        return auth.valid_session(session_cookie(request))

    def session_response(request, data, cookie):
        response = JSONResponse(data)
        if cookie:
            response.set_cookie('parser_session', cookie, httponly=True,
                                secure=request.url.scheme == 'https' or public_url.startswith('https://'),
                                samesite='strict', max_age=SESSION_SECONDS)
        return response

    def auth_error_response(error):
        if error.status in {401, 403, 429}:
            manager.logs.write('auth_limited' if error.status == 429 else 'auth_failed')
        headers = {'Retry-After': str(error.retry_after)} if error.retry_after else {}
        return JSONResponse({'detail': error.detail}, status_code=error.status, headers=headers)

    def admin(request: Request):
        if not session_valid(request):
            raise HTTPException(401, "sign in to manage subscriptions")

    @app.middleware("http")
    async def boundaries(request, call_next):
        # Count even malformed authentication requests, before body validation.
        # SQLite work stays off the event loop. Forwarded IPs are accepted only
        # through Uvicorn's configured trusted proxies.
        if request.method == 'POST' and (request.url.path.rstrip('/') == '/api/login' or
                                        request.url.path.startswith('/api/security/')):
            try:
                await run_in_threadpool(auth.limit_request, request.client.host if request.client else 'unknown')
            except AuthError as error:
                response = auth_error_response(error)
                response.headers['Cache-Control'] = 'no-store'
                return response
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

    @app.exception_handler(AuthError)
    async def auth_error(request, exc):
        return auth_error_response(exc)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/session")
    def session(request: Request):
        return {"authenticated": session_valid(request)}

    @app.post("/api/login")
    def login(body: Login, request: Request):
        data, cookie = auth.login(body.token, body.code)
        if cookie:
            auth.logout(session_cookie(request))
            manager.logs.write('auth_recovery_used' if data['recovery_used'] else 'auth_login')
        return session_response(request, data, cookie)

    @app.post("/api/logout")
    def logout(request: Request):
        auth.logout(session_cookie(request))
        response = JSONResponse({"authenticated": False})
        response.delete_cookie("parser_session")
        return response

    @app.get('/api/security', dependencies=[Depends(admin)])
    def security():
        return auth.status()

    @app.post('/api/security/2fa/setup', dependencies=[Depends(admin)])
    def setup_two_factor(body: Login, request: Request):
        account = urlsplit(public_url).hostname or 'admin'
        return auth.begin_setup(session_cookie(request), body.token, account)

    @app.post('/api/security/2fa/confirm', dependencies=[Depends(admin)])
    def confirm_two_factor(body: Verification, request: Request):
        data, cookie = auth.confirm_setup(session_cookie(request), body.code)
        manager.logs.write('auth_enabled')
        return session_response(request, data, cookie)

    @app.post('/api/security/2fa/disable', dependencies=[Depends(admin)])
    def disable_two_factor(body: Login, request: Request):
        data, cookie = auth.change_factor(session_cookie(request), body.token, body.code, disable=True)
        manager.logs.write('auth_disabled')
        return session_response(request, data, cookie)

    @app.post('/api/security/2fa/recovery', dependencies=[Depends(admin)])
    def replace_recovery_codes(body: Login, request: Request):
        data, cookie = auth.change_factor(session_cookie(request), body.token, body.code)
        manager.logs.write('auth_recovery')
        return session_response(request, data, cookie)

    @app.get("/api/state", dependencies=[Depends(admin)])
    def state(request: Request, revision: str | None = None):
        return manager.public_state(public_url or str(request.base_url), revision)

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

    @app.get('/api/subscriptions', dependencies=[Depends(admin)])
    def subscriptions(request: Request):
        return manager.public_subscriptions(public_url or str(request.base_url))

    @app.post('/api/subscriptions', dependencies=[Depends(admin)], status_code=201)
    def add_subscription(body: Subscription):
        return {'id': manager.change_subscription(body.model_dump())}

    @app.put('/api/subscriptions/{identifier}', dependencies=[Depends(admin)])
    def edit_subscription(identifier: str, body: Subscription):
        return {'id': manager.change_subscription(body.model_dump(), identifier)}

    @app.delete('/api/subscriptions/{identifier}', dependencies=[Depends(admin)])
    def delete_subscription(identifier: str):
        manager.change_subscription(identifier=identifier, delete=True)
        return {'deleted': True}

    @app.post('/api/subscriptions/{identifier}/rotate-link', dependencies=[Depends(admin)])
    def rotate_subscription(identifier: str):
        manager.store.rotate_subscription_token(identifier)
        return {'rotated': True}

    @app.get("/sub/{token}/{filename}")
    def subscription(token: str, filename: str):
        if filename not in publication.FILES or manager.store.subscription_for_token(token) is None:
            raise HTTPException(404, 'subscription not found')
        manager.ensure_publications()
        with manager.guard:
            selected = manager.store.subscription_for_token(token)
            if selected is None or filename not in publication.FILES:
                raise HTTPException(404, "subscription not found")
            snapshot = manager.subscription_snapshot(selected)
            if not manager.publication_current(snapshot):
                raise HTTPException(503, 'connection checks are pending or expired; check servers in the dashboard')
            content = snapshot.get("exports", {}).get(filename)
            target = {"hiddify.json": "hiddify", "mihomo.yaml": "mihomo", "xray.json": "xray",
                      "v2board.json": "v2board", "v2board.txt": "v2board_uri"}.get(filename)
            if target and not snapshot.get("counts", {}).get(target):
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
