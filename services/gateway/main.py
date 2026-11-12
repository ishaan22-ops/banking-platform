from contextlib import asynccontextmanager
from datetime import datetime, timezone
import html
import os
from pathlib import Path
from time import perf_counter
from urllib.parse import urlparse
from xml.sax.saxutils import escape as xml_escape

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from services.common import ServiceBase, build_database

STATIC_DIR = Path(__file__).parents[2] / "app" / "static"
engine, session_factory = build_database("gateway")
ROUTES = (
    ("/api/auth", "AUTH_SERVICE_URL", "http://auth:8000"),
    ("/api/account", "ACCOUNTS_SERVICE_URL", "http://accounts:8000"),
    ("/api/demo-deposits", "ACCOUNTS_SERVICE_URL", "http://accounts:8000"),
    ("/api/transfers", "TRANSFERS_SERVICE_URL", "http://transfers:8000"),
    ("/api/transactions", "TRANSACTIONS_SERVICE_URL", "http://transactions:8000"),
    ("/api/chat", "ASSISTANT_SERVICE_URL", "http://assistant:8000"),
)


class RequestAudit(ServiceBase):
    __tablename__ = "gateway_request_audit"

    id: Mapped[int] = mapped_column(primary_key=True)
    method: Mapped[str] = mapped_column(String(12))
    path: Mapped[str] = mapped_column(String(200))
    status_code: Mapped[int] = mapped_column(Integer)
    duration_ms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ServiceBase.metadata.create_all(engine)
    yield
    engine.dispose()


app = FastAPI(
    title="Harbor API Gateway",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def public_base_url() -> str:
    value = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise RuntimeError("PUBLIC_BASE_URL must be an absolute http or https origin.")
    return value


@app.middleware("http")
async def security_headers(request: Request, call_next):
    started_at = perf_counter()
    response = await call_next(request)
    if request.url.path != "/health":
        with session_factory() as db:
            db.add(
                RequestAudit(
                    method=request.method,
                    path=request.url.path[:200],
                    status_code=response.status_code,
                    duration_ms=int((perf_counter() - started_at) * 1000),
                )
            )
            db.commit()
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    )
    return response


@app.get("/", include_in_schema=False)
def home() -> HTMLResponse:
    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(page.replace("__PUBLIC_BASE_URL__", html.escape(public_base_url(), quote=True)))


@app.get("/health", include_in_schema=False)
def health() -> dict[str, str]:
    return {"status": "ok", "service": "gateway"}


@app.get("/robots.txt", include_in_schema=False)
def robots() -> PlainTextResponse:
    return PlainTextResponse(
        f"User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /health\nSitemap: {public_base_url()}/sitemap.xml\n"
    )


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap() -> Response:
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>{xml_escape(public_base_url())}/</loc></url></urlset>"
    )
    return Response(content=body, media_type="application/xml")


@app.api_route(
    "/api/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
async def proxy(path: str, request: Request) -> Response:
    requested_path = f"/api/{path}"
    route = next(
        (
            (env_name, fallback)
            for prefix, env_name, fallback in ROUTES
            if requested_path == prefix or requested_path.startswith(f"{prefix}/")
        ),
        None,
    )
    if route is None:
        return Response(status_code=404, content='{"detail":"API route not found."}', media_type="application/json")
    env_name, fallback = route
    base_url = os.getenv(env_name, fallback)
    headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower() in {"authorization", "content-type", "idempotency-key"}
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            upstream = await client.request(
                request.method,
                f"{base_url.rstrip('/')}{requested_path}",
                params=request.query_params,
                content=await request.body(),
                headers=headers,
            )
    except httpx.HTTPError as exc:
        return Response(
            content='{"detail":"The requested service is temporarily unavailable."}',
            status_code=503,
            media_type="application/json",
        )
    response_headers = {}
    if "content-type" in upstream.headers:
        response_headers["content-type"] = upstream.headers["content-type"]
    if "www-authenticate" in upstream.headers:
        response_headers["www-authenticate"] = upstream.headers["www-authenticate"]
    return Response(content=upstream.content, status_code=upstream.status_code, headers=response_headers)
