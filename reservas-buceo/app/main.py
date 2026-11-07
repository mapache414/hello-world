"""API HTTP. Las rutas /tools/* son las herramientas que llaman los agentes de Mirlo
(WhatsApp y voz); /webhooks/trytn recibe pagos y cambios de cupo desde TRYTN."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import get_settings
from .db import Database
from .notifier import MirloNotifier
from .service import BookingError, BookingService
from .trytn import build_trytn


class HoldIn(BaseModel):
    slot_id: str
    seats: int = Field(ge=1, le=20)
    customer_phone: str
    channel: Literal["whatsapp", "voice"]

class BookingIn(BaseModel):
    hold_id: str
    customer_name: str
    customer_phone: str
    customer_email: str | None = None

def build_service(settings=None) -> BookingService:
    settings = settings or get_settings()
    return BookingService(Database(settings.database_path), build_trytn(settings),
                          MirloNotifier(settings.mirlo_notify_url, settings.mirlo_api_key),
                          settings.hold_minutes, settings.payment_minutes)


def create_app(service: BookingService | None = None, settings=None) -> FastAPI:
    settings = settings or get_settings()
    service = service or build_service(settings)

    @asynccontextmanager
    async def lifespan(app):
        async def sweeper():
            while True:
                await asyncio.to_thread(service.expire_stale)
                await asyncio.sleep(60)
        task = asyncio.create_task(sweeper())
        yield
        task.cancel()

    app = FastAPI(title="Reservas de buceo — Mirlo ↔ TRYTN", lifespan=lifespan)
    app.state.service = service

    @app.exception_handler(BookingError)
    async def booking_error(_, exc: BookingError):
        # Mensaje en lenguaje natural para que el agente lo pueda repetir al cliente.
        return JSONResponse(status_code=exc.status,
                            content={"error": exc.code, "message": exc.message})

    def agent_auth(x_api_key: str = Header(...)):
        if not hmac.compare_digest(x_api_key, settings.agent_api_key):
            raise HTTPException(401, "API key inválida")

    tools = [Depends(agent_auth)]

    @app.get("/tools/availability", dependencies=tools)
    def availability(date_from: date, date_to: date | None = None, seats: int = 1,
                     center_id: str | None = None, kind: str | None = None):
        date_to = date_to or date_from
        slots = service.search_availability(date_from.isoformat(),
                                            (date_to + timedelta(days=1)).isoformat(),
                                            seats, center_id, kind)
        return {"slots": slots}

    @app.post("/tools/holds", dependencies=tools, status_code=201)
    def create_hold(body: HoldIn):
        return service.hold(body.slot_id, body.seats, body.customer_phone, body.channel)

    @app.delete("/tools/holds/{hold_id}", dependencies=tools, status_code=204)
    def release_hold(hold_id: str):
        service.release_hold(hold_id)

    @app.post("/tools/bookings", dependencies=tools, status_code=201)
    def create_booking(body: BookingIn, idempotency_key: str | None = Header(None)):
        return service.create_booking(body.hold_id, body.customer_name, body.customer_phone,
                                      body.customer_email, idempotency_key)

    @app.get("/tools/bookings/{booking_id}", dependencies=tools)
    def get_booking(booking_id: str):
        return service.get_booking(booking_id)

    @app.post("/tools/bookings/{booking_id}/cancel", dependencies=tools)
    def cancel_booking(booking_id: str):
        return service.cancel_booking(booking_id)

    @app.post("/webhooks/trytn")
    async def trytn_webhook(request: Request, x_trytn_signature: str = Header("")):
        raw = await request.body()
        expected = hmac.new(settings.trytn_webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, x_trytn_signature.removeprefix("sha256=")):
            raise HTTPException(401, "Firma inválida")
        result = await asyncio.to_thread(service.handle_trytn_event, json.loads(raw))
        return {"result": result}

    @app.post("/admin/sync", dependencies=tools)
    def sync(days: int = 30):
        today = date.today()
        n = service.sync_from_trytn(today.isoformat(), (today + timedelta(days=days)).isoformat())
        return {"slots_synced": n}

    @app.get("/health")
    def health():
        return {"ok": True}

    return app
