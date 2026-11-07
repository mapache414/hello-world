"""Persistencia en SQLite.

Las escrituras usan BEGIN IMMEDIATE: SQLite serializa a los escritores, así que
dos agentes (WhatsApp y voz) nunca pueden vender el mismo último lugar.
"""
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS centers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    location TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'America/Mexico_City'
);
CREATE TABLE IF NOT EXISTS tours (
    id TEXT PRIMARY KEY,
    center_id TEXT NOT NULL REFERENCES centers(id),
    name TEXT NOT NULL,
    kind TEXT NOT NULL,                 -- p.ej. bautizo, arrecife, cenote, curso
    price_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'MXN',
    requires_certification INTEGER NOT NULL DEFAULT 0,
    trytn_product_id TEXT
);
CREATE TABLE IF NOT EXISTS slots (
    id TEXT PRIMARY KEY,
    tour_id TEXT NOT NULL REFERENCES tours(id),
    start_at TEXT NOT NULL,             -- ISO-8601 UTC
    capacity INTEGER NOT NULL,          -- cupo total según TRYTN
    external_sold INTEGER NOT NULL DEFAULT 0, -- vendidos fuera de este sistema (web, mostrador)
    trytn_slot_id TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS holds (
    id TEXT PRIMARY KEY,
    slot_id TEXT NOT NULL REFERENCES slots(id),
    seats INTEGER NOT NULL,
    customer_phone TEXT NOT NULL,
    channel TEXT NOT NULL,              -- whatsapp | voice
    status TEXT NOT NULL,               -- active | converted | released | expired
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bookings (
    id TEXT PRIMARY KEY,
    hold_id TEXT REFERENCES holds(id),
    slot_id TEXT NOT NULL REFERENCES slots(id),
    seats INTEGER NOT NULL,
    customer_name TEXT NOT NULL,
    customer_phone TEXT NOT NULL,
    customer_email TEXT,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,               -- pending_payment | confirmed | cancelled | expired
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL,
    trytn_booking_id TEXT UNIQUE,
    payment_url TEXT,
    payment_expires_at TEXT,
    idempotency_key TEXT UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS webhook_events (
    id TEXT PRIMARY KEY,                -- id del evento en TRYTN (deduplicación)
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_slots_start ON slots(start_at);
CREATE INDEX IF NOT EXISTS idx_holds_slot ON holds(slot_id, status);
CREATE INDEX IF NOT EXISTS idx_bookings_slot ON bookings(slot_id, status);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def read(self):
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write(self):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
