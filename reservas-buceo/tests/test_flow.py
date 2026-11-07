import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.notifier import MirloNotifier
from app.seed import seed
from app.service import BookingService
from app.trytn import MockTrytn

KEY = {"X-API-Key": "k"}


class Clock:
    def __init__(self):
        self.now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path):
    settings = Settings(database_path=str(tmp_path / "t.db"), agent_api_key="k",
                        trytn_webhook_secret="s")
    db = Database(settings.database_path)
    seed(db, days=3)
    clock, trytn, notifier = Clock(), MockTrytn(), MirloNotifier("", "")
    svc = BookingService(db, trytn, notifier, hold_minutes=10, payment_minutes=30, clock=clock)
    client = TestClient(create_app(svc, settings))
    return client, svc, trytn, notifier, clock


def tomorrow_slots(client, center="cozumel", kind="bautizo", seats=1):
    d = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    r = client.get("/tools/availability", headers=KEY,
                   params={"date_from": d, "center_id": center, "kind": kind, "seats": seats})
    assert r.status_code == 200
    return r.json()["slots"]


def tomorrow_slot(client, **kw):
    return tomorrow_slots(client, **kw)[0]


def signed(event, secret="s"):
    raw = json.dumps(event).encode()
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"X-Trytn-Signature": f"sha256={sig}", "Content-Type": "application/json"}


def test_requires_api_key(env):
    client = env[0]
    assert client.get("/tools/availability", params={"date_from": "2030-01-01"}).status_code == 422
    assert client.get("/tools/availability", params={"date_from": "2030-01-01"},
                      headers={"X-API-Key": "x"}).status_code == 401


def test_availability_spans_four_centers(env):
    client = env[0]
    d = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    slots = client.get("/tools/availability", headers=KEY,
                       params={"date_from": d, "kind": "bautizo"}).json()["slots"]
    assert {s["center_id"] for s in slots} == {"cancun", "playa", "cozumel", "tulum"}


def test_full_sale_whatsapp_and_voice_share_inventory(env):
    client, svc, trytn, notifier, _ = env
    slot = tomorrow_slot(client)
    assert slot["available"] == 6

    # El agente de WhatsApp aparta 4 lugares...
    h = client.post("/tools/holds", headers=KEY, json={
        "slot_id": slot["slot_id"], "seats": 4, "customer_phone": "+5215511111111",
        "channel": "whatsapp"}).json()
    # ...y el agente de voz ya no puede vender 3 en el mismo horario.
    r = client.post("/tools/holds", headers=KEY, json={
        "slot_id": slot["slot_id"], "seats": 3, "customer_phone": "+5215522222222",
        "channel": "voice"})
    assert r.status_code == 409 and r.json()["error"] == "not_enough_seats"
    assert "2 lugares" in r.json()["message"]

    b = client.post("/tools/bookings", headers={**KEY, "Idempotency-Key": "conv-1"}, json={
        "hold_id": h["id"], "customer_name": "Ana", "customer_phone": "+5215511111111"}).json()
    assert b["status"] == "pending_payment" and b["payment_url"].startswith("https://")
    assert b["amount_cents"] == 4 * 1800 * 100
    assert trytn.bookings[b["trytn_booking_id"]]["seats"] == 4

    # Reintento del agente con la misma llave: no duplica la venta.
    again = client.post("/tools/bookings", headers={**KEY, "Idempotency-Key": "conv-1"}, json={
        "hold_id": h["id"], "customer_name": "Ana", "customer_phone": "+5215511111111"}).json()
    assert again["id"] == b["id"] and len(trytn.bookings) == 1

    # TRYTN avisa que se pagó.
    raw, hdr = signed({"id": "evt1", "type": "payment.succeeded",
                       "data": {"booking_id": b["trytn_booking_id"]}})
    assert client.post("/webhooks/trytn", content=raw, headers=hdr).json()["result"] == "confirmed"
    assert client.post("/webhooks/trytn", content=raw, headers=hdr).json()["result"] == "duplicate"
    assert svc.get_booking(b["id"])["status"] == "confirmed"
    assert notifier.sent[-1]["event"] == "booking.confirmed"
    assert tomorrow_slot(client)["available"] == 2


def test_hold_expires_and_frees_seats(env):
    client, svc, _, _, clock = env
    slot = tomorrow_slot(client)
    h = svc.hold(slot["slot_id"], 6, "+52", "voice")
    assert tomorrow_slots(client) == []  # lleno
    clock.now += timedelta(minutes=11)
    assert tomorrow_slot(client)["available"] == 6
    r = client.post("/tools/bookings", headers=KEY, json={
        "hold_id": h["id"], "customer_name": "X", "customer_phone": "+52"})
    assert r.status_code == 409 and r.json()["error"] == "hold_expired"


def test_unpaid_booking_expires_and_is_cancelled_in_trytn(env):
    client, svc, trytn, _, clock = env
    slot = tomorrow_slot(client)
    h = svc.hold(slot["slot_id"], 2, "+52", "whatsapp")
    b = svc.create_booking(h["id"], "Luis", "+52")
    clock.now += timedelta(minutes=31)
    assert svc.expire_stale() == 1
    assert svc.get_booking(b["id"])["status"] == "expired"
    assert b["trytn_booking_id"] in trytn.cancelled
    assert tomorrow_slot(client)["available"] == 6


def test_external_sales_in_trytn_reduce_availability(env):
    client, svc, *_ = env
    slot = tomorrow_slot(client)
    h = svc.hold(slot["slot_id"], 1, "+52", "whatsapp")
    svc.create_booking(h["id"], "Eva", "+52")
    # TRYTN reporta 4 vendidos en total: 1 de los agentes + 3 por la web/mostrador.
    raw, hdr = signed({"id": "evt2", "type": "availability.updated",
                       "data": {"slot_id": f"TS-{slot['slot_id']}", "capacity": 6, "sold": 4}})
    client.post("/webhooks/trytn", content=raw, headers=hdr)
    assert tomorrow_slot(client)["available"] == 2


def test_bad_webhook_signature_rejected(env):
    client = env[0]
    raw, hdr = signed({"id": "e", "type": "payment.succeeded", "data": {}}, secret="otro")
    assert client.post("/webhooks/trytn", content=raw, headers=hdr).status_code == 401


def test_cancel_pending_booking(env):
    client, svc, trytn, *_ = env
    slot = tomorrow_slot(client)
    b = svc.create_booking(svc.hold(slot["slot_id"], 2, "+52", "voice")["id"], "Leo", "+52")
    r = client.post(f"/tools/bookings/{b['id']}/cancel", headers=KEY)
    assert r.json()["status"] == "cancelled" and b["trytn_booking_id"] in trytn.cancelled
    assert tomorrow_slot(client)["available"] == 6
