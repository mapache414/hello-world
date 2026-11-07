"""Lógica de reservas: disponibilidad unificada de los 4 centros, apartados
(holds) con caducidad, creación de la venta en TRYTN y conciliación de pagos."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from .db import Database
from .notifier import MirloNotifier
from .trytn import TrytnClient


class BookingError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# Lugares ocupados por este sistema (apartados vigentes + reservas vivas).
_TAKEN_SQL = """
    COALESCE((SELECT SUM(h.seats) FROM holds h
              WHERE h.slot_id = s.id AND h.status = 'active' AND h.expires_at > :now), 0)
  + COALESCE((SELECT SUM(b.seats) FROM bookings b
              WHERE b.slot_id = s.id AND (b.status = 'confirmed'
                 OR (b.status = 'pending_payment' AND b.payment_expires_at > :now))), 0)
"""


class BookingService:
    def __init__(self, db: Database, trytn: TrytnClient, notifier: MirloNotifier,
                 hold_minutes: int = 10, payment_minutes: int = 30, clock=utcnow):
        self.db, self.trytn, self.notifier = db, trytn, notifier
        self.hold_minutes, self.payment_minutes = hold_minutes, payment_minutes
        self.clock = clock

    # ------------------------------------------------------------------ lectura
    def _available(self, conn, slot_id: str, now: str) -> int:
        row = conn.execute(
            f"SELECT s.capacity - s.external_sold - ({_TAKEN_SQL}) AS available "
            "FROM slots s WHERE s.id = :slot_id", {"now": now, "slot_id": slot_id}).fetchone()
        if row is None:
            raise BookingError("slot_not_found", "Ese horario no existe.", 404)
        return row["available"]

    def search_availability(self, date_from: str, date_to: str, seats: int = 1,
                            center_id: str | None = None, kind: str | None = None) -> list[dict]:
        now = iso(self.clock())
        sql = f"""
            SELECT s.id AS slot_id, s.start_at, t.id AS tour_id, t.name AS tour_name, t.kind,
                   t.price_cents, t.currency, t.requires_certification,
                   c.id AS center_id, c.name AS center_name, c.location,
                   s.capacity - s.external_sold - ({_TAKEN_SQL}) AS available
            FROM slots s JOIN tours t ON t.id = s.tour_id JOIN centers c ON c.id = t.center_id
            WHERE s.start_at >= :from AND s.start_at < :to AND s.start_at > :now
              AND (:center IS NULL OR c.id = :center)
              AND (:kind IS NULL OR t.kind = :kind)
            ORDER BY s.start_at, c.name"""
        with self.db.read() as conn:
            rows = conn.execute(sql, {"now": now, "from": date_from, "to": date_to,
                                      "center": center_id, "kind": kind}).fetchall()
        return [dict(r, requires_certification=bool(r["requires_certification"]))
                for r in rows if r["available"] >= seats]

    def get_booking(self, booking_id: str) -> dict:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        if row is None:
            raise BookingError("booking_not_found", "No encontré esa reserva.", 404)
        return dict(row)

    # ----------------------------------------------------------------- apartado
    def hold(self, slot_id: str, seats: int, customer_phone: str, channel: str) -> dict:
        if seats < 1:
            raise BookingError("invalid_seats", "Debe ser al menos 1 lugar.", 422)
        now = self.clock()
        with self.db.write() as conn:
            slot = conn.execute("SELECT start_at FROM slots WHERE id = ?", (slot_id,)).fetchone()
            if slot is None:
                raise BookingError("slot_not_found", "Ese horario no existe.", 404)
            if slot["start_at"] <= iso(now):
                raise BookingError("slot_in_past", "Ese horario ya pasó.")
            available = self._available(conn, slot_id, iso(now))
            if available < seats:
                raise BookingError("not_enough_seats",
                                   f"Solo quedan {max(available, 0)} lugares en ese horario.")
            hold = {"id": _new_id("hold"), "slot_id": slot_id, "seats": seats,
                    "customer_phone": customer_phone, "channel": channel, "status": "active",
                    "expires_at": iso(now + timedelta(minutes=self.hold_minutes)),
                    "created_at": iso(now)}
            conn.execute("INSERT INTO holds VALUES (:id, :slot_id, :seats, :customer_phone, "
                         ":channel, :status, :expires_at, :created_at)", hold)
        return hold

    def release_hold(self, hold_id: str) -> None:
        with self.db.write() as conn:
            conn.execute("UPDATE holds SET status = 'released' WHERE id = ? AND status = 'active'",
                         (hold_id,))

    # ------------------------------------------------------------------- venta
    def create_booking(self, hold_id: str, customer_name: str, customer_phone: str,
                       customer_email: str | None = None,
                       idempotency_key: str | None = None) -> dict:
        """Convierte un apartado en venta: crea la reserva en TRYTN y devuelve el
        link de pago para que el agente se lo mande al cliente."""
        if idempotency_key:
            with self.db.read() as conn:
                row = conn.execute("SELECT * FROM bookings WHERE idempotency_key = ?",
                                   (idempotency_key,)).fetchone()
            if row:
                return dict(row)

        now = self.clock()
        with self.db.read() as conn:
            h = conn.execute("""
                SELECT h.*, s.trytn_slot_id, t.price_cents, t.currency
                FROM holds h JOIN slots s ON s.id = h.slot_id JOIN tours t ON t.id = s.tour_id
                WHERE h.id = ?""", (hold_id,)).fetchone()
        self._check_hold(h, now)

        booking_id = _new_id("bk")
        amount = h["price_cents"] * h["seats"]
        # La llamada externa va fuera de la transacción para no bloquear a otros agentes;
        # el apartado ya protege el lugar mientras tanto.
        tr = self.trytn.create_booking(
            trytn_slot_id=h["trytn_slot_id"], seats=h["seats"], amount_cents=amount,
            currency=h["currency"], reference=booking_id,
            customer={"name": customer_name, "phone": customer_phone, "email": customer_email})

        now = self.clock()
        try:
            with self.db.write() as conn:
                h2 = conn.execute("SELECT * FROM holds WHERE id = ?", (hold_id,)).fetchone()
                self._check_hold(h2, now)
                booking = {
                    "id": booking_id, "hold_id": hold_id, "slot_id": h["slot_id"],
                    "seats": h["seats"], "customer_name": customer_name,
                    "customer_phone": customer_phone, "customer_email": customer_email,
                    "channel": h["channel"], "status": "pending_payment", "amount_cents": amount,
                    "currency": h["currency"], "trytn_booking_id": tr.booking_id,
                    "payment_url": tr.payment_url,
                    "payment_expires_at": iso(now + timedelta(minutes=self.payment_minutes)),
                    "idempotency_key": idempotency_key,
                    "created_at": iso(now), "updated_at": iso(now)}
                conn.execute("UPDATE holds SET status = 'converted' WHERE id = ?", (hold_id,))
                conn.execute(f"INSERT INTO bookings ({', '.join(booking)}) VALUES "
                             f"({', '.join(':' + k for k in booking)})", booking)
        except BookingError:
            self.trytn.cancel_booking(tr.booking_id)  # compensación
            raise
        return booking

    def _check_hold(self, h, now: datetime) -> None:
        if h is None:
            raise BookingError("hold_not_found", "No encontré ese apartado.", 404)
        if h["status"] != "active" or h["expires_at"] <= iso(now):
            raise BookingError("hold_expired",
                               "El apartado ya venció; vuelve a consultar disponibilidad.")

    def cancel_booking(self, booking_id: str, reason: str = "customer") -> dict:
        b = self.get_booking(booking_id)
        if b["status"] in ("cancelled", "expired"):
            return b
        if b["status"] == "confirmed":
            # Las devoluciones dependen de la política del negocio: se pasan a una persona.
            raise BookingError("needs_human",
                               "La reserva ya está pagada; un asesor gestionará el cambio o reembolso.")
        self.trytn.cancel_booking(b["trytn_booking_id"])
        return self._set_status(booking_id, "cancelled", only_from=("pending_payment",))

    def _set_status(self, booking_id: str, status: str, only_from: tuple[str, ...]) -> dict:
        with self.db.write() as conn:
            q = ",".join("?" * len(only_from))
            conn.execute(f"UPDATE bookings SET status = ?, updated_at = ? "
                         f"WHERE id = ? AND status IN ({q})",
                         (status, iso(self.clock()), booking_id, *only_from))
        return self.get_booking(booking_id)

    # --------------------------------------------------------- eventos de TRYTN
    def handle_trytn_event(self, event: dict) -> str:
        """Procesa un webhook de TRYTN. Devuelve qué se hizo (para logs)."""
        with self.db.write() as conn:
            seen = conn.execute("SELECT 1 FROM webhook_events WHERE id = ?",
                                (event["id"],)).fetchone()
            if seen:
                return "duplicate"
            conn.execute("INSERT INTO webhook_events VALUES (?, ?)",
                         (event["id"], iso(self.clock())))

        etype, data = event["type"], event.get("data", {})
        if etype in ("payment.succeeded", "payment.failed", "booking.cancelled"):
            with self.db.read() as conn:
                row = conn.execute("SELECT id FROM bookings WHERE trytn_booking_id = ?",
                                   (data["booking_id"],)).fetchone()
            if row is None:
                return "ignored_unknown_booking"  # venta hecha fuera de los agentes
            if etype == "payment.succeeded":
                # Se confirma aunque el link haya vencido: el cliente ya pagó.
                b = self._set_status(row["id"], "confirmed", ("pending_payment", "expired"))
                self.notifier.booking_event("booking.confirmed", b)
                return "confirmed"
            if etype == "booking.cancelled":
                b = self._set_status(row["id"], "cancelled", ("pending_payment", "confirmed"))
                self.notifier.booking_event("booking.cancelled", b)
                return "cancelled"
            self.notifier.booking_event("payment.failed", self.get_booking(row["id"]))
            return "payment_failed"

        if etype == "availability.updated":
            self._apply_trytn_slot(data["slot_id"], data["capacity"], data["sold"])
            return "availability_updated"
        return "ignored"

    def _apply_trytn_slot(self, trytn_slot_id: str, capacity: int, sold: int) -> None:
        """TRYTN reporta lo vendido en total (web, mostrador y agentes). Lo que no
        vendieron los agentes se guarda en external_sold."""
        with self.db.write() as conn:
            slot = conn.execute("SELECT id FROM slots WHERE trytn_slot_id = ?",
                                (trytn_slot_id,)).fetchone()
            if slot is None:
                return
            ours = conn.execute("""SELECT COALESCE(SUM(seats), 0) FROM bookings
                                   WHERE slot_id = ? AND status IN ('pending_payment', 'confirmed')""",
                                (slot["id"],)).fetchone()[0]
            conn.execute("UPDATE slots SET capacity = ?, external_sold = ? WHERE id = ?",
                         (capacity, max(sold - ours, 0), slot["id"]))

    def sync_from_trytn(self, date_from: str, date_to: str) -> int:
        """Respaldo del webhook: trae la disponibilidad de TRYTN de todos los tours."""
        with self.db.read() as conn:
            products = [r[0] for r in conn.execute(
                "SELECT DISTINCT trytn_product_id FROM tours WHERE trytn_product_id IS NOT NULL")]
        n = 0
        for pid in products:
            for s in self.trytn.list_slots(pid, date_from, date_to):
                self._apply_trytn_slot(s.trytn_slot_id, s.capacity, s.sold)
                n += 1
        return n

    # --------------------------------------------------------------- limpieza
    def expire_stale(self) -> int:
        """Libera apartados vencidos y cancela en TRYTN las reservas no pagadas a tiempo."""
        now = iso(self.clock())
        with self.db.write() as conn:
            conn.execute("UPDATE holds SET status = 'expired' "
                         "WHERE status = 'active' AND expires_at <= ?", (now,))
            stale = conn.execute("SELECT id, trytn_booking_id FROM bookings "
                                 "WHERE status = 'pending_payment' AND payment_expires_at <= ?",
                                 (now,)).fetchall()
            conn.execute("UPDATE bookings SET status = 'expired', updated_at = ? "
                         "WHERE status = 'pending_payment' AND payment_expires_at <= ?", (now, now))
        for row in stale:
            self.trytn.cancel_booking(row["trytn_booking_id"])
        return len(stale)
