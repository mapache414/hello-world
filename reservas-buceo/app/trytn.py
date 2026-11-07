"""Adaptador hacia TRYTN.

TRYTN no publica documentación de API abierta, así que toda la comunicación
queda detrás de esta interfaz. Hay dos implementaciones:

* MockTrytn: para desarrollo y pruebas, simula reservas y links de pago.
* HttpTrytn: cliente HTTP. Las rutas están marcadas con TODO(TRYTN) y deben
  ajustarse con la documentación/credenciales que entregue el soporte de TRYTN.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

import httpx


@dataclass
class TrytnBooking:
    booking_id: str
    payment_url: str


@dataclass
class TrytnSlot:
    trytn_slot_id: str
    capacity: int
    sold: int  # lugares vendidos en TRYTN (incluye los de este sistema)


class TrytnClient(Protocol):
    def create_booking(self, *, trytn_slot_id: str, seats: int, customer: dict,
                       amount_cents: int, currency: str, reference: str) -> TrytnBooking: ...

    def cancel_booking(self, booking_id: str) -> None: ...

    def list_slots(self, trytn_product_id: str, date_from: str, date_to: str) -> list[TrytnSlot]: ...


class MockTrytn:
    def __init__(self):
        self.bookings: dict[str, dict] = {}
        self.cancelled: set[str] = set()

    def create_booking(self, *, trytn_slot_id, seats, customer, amount_cents, currency, reference):
        booking_id = f"TR-{uuid.uuid4().hex[:8].upper()}"
        self.bookings[booking_id] = {"slot": trytn_slot_id, "seats": seats, "customer": customer,
                                     "amount_cents": amount_cents, "reference": reference}
        return TrytnBooking(booking_id, f"https://pay.example.com/{booking_id}")

    def cancel_booking(self, booking_id):
        self.cancelled.add(booking_id)

    def list_slots(self, trytn_product_id, date_from, date_to):
        return []


class HttpTrytn:
    def __init__(self, base_url: str, api_key: str, timeout: float = 15.0):
        self.http = httpx.Client(base_url=base_url, timeout=timeout,
                                 headers={"Authorization": f"Bearer {api_key}"})

    def create_booking(self, *, trytn_slot_id, seats, customer, amount_cents, currency, reference):
        # TODO(TRYTN): confirmar ruta y campos con la documentación de TRYTN.
        r = self.http.post("/v1/bookings", json={
            "slot_id": trytn_slot_id,
            "quantity": seats,
            "customer": customer,
            "status": "pending_payment",
            "external_reference": reference,
            "send_payment_link": False,
        }, headers={"Idempotency-Key": reference})
        r.raise_for_status()
        data = r.json()
        return TrytnBooking(data["id"], data["payment_url"])

    def cancel_booking(self, booking_id):
        # TODO(TRYTN): confirmar ruta.
        self.http.post(f"/v1/bookings/{booking_id}/cancel").raise_for_status()

    def list_slots(self, trytn_product_id, date_from, date_to):
        # TODO(TRYTN): confirmar ruta y campos.
        r = self.http.get(f"/v1/products/{trytn_product_id}/availability",
                          params={"from": date_from, "to": date_to})
        r.raise_for_status()
        return [TrytnSlot(s["id"], s["capacity"], s["booked"]) for s in r.json()["slots"]]


def build_trytn(settings) -> TrytnClient:
    if settings.trytn_mode == "http":
        return HttpTrytn(settings.trytn_base_url, settings.trytn_api_key)
    return MockTrytn()
