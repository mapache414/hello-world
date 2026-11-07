"""Aviso a Mirlo cuando cambia el estado de una reserva (p.ej. pago confirmado),
para que el agente le escriba al cliente por WhatsApp."""
import logging

import httpx

log = logging.getLogger(__name__)


class MirloNotifier:
    def __init__(self, url: str, api_key: str):
        self.url, self.api_key = url, api_key
        self.sent: list[dict] = []  # útil en pruebas

    def booking_event(self, event: str, booking: dict) -> None:
        payload = {"event": event, "booking": booking}
        self.sent.append(payload)
        if not self.url:
            return
        try:
            httpx.post(self.url, json=payload, timeout=10,
                       headers={"Authorization": f"Bearer {self.api_key}"}).raise_for_status()
        except httpx.HTTPError:
            # No debe tumbar el webhook de TRYTN; queda registrado para reintentar.
            log.exception("No se pudo notificar a Mirlo: %s", payload)
