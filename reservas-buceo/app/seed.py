"""Carga datos de ejemplo: 4 centros, sus tours y horarios de los próximos 14 días.
Reemplazar por los productos reales de TRYTN (ver README, paso 2).

Uso: python -m app.seed
"""
from datetime import datetime, time, timedelta, timezone

from .config import get_settings
from .db import Database

CENTERS = [
    ("cancun", "Centro Cancún", "Cancún, Q. Roo"),
    ("playa", "Centro Playa del Carmen", "Playa del Carmen, Q. Roo"),
    ("cozumel", "Centro Cozumel", "Cozumel, Q. Roo"),
    ("tulum", "Centro Tulum", "Tulum, Q. Roo"),
]
TOURS = [  # (sufijo, nombre, tipo, precio MXN, requiere certificación, cupo, horas UTC)
    ("bautizo", "Bautizo de buceo", "bautizo", 1800, False, 6, [14]),
    ("2tanques", "Arrecife 2 tanques", "arrecife", 2400, True, 10, [13, 18]),
]


def seed(db: Database, days: int = 14) -> None:
    today = datetime.now(timezone.utc).date()
    with db.write() as conn:
        for cid, name, loc in CENTERS:
            conn.execute("INSERT OR IGNORE INTO centers (id, name, location) VALUES (?, ?, ?)",
                         (cid, name, loc))
            for suf, tname, kind, price, cert, cap, hours in TOURS:
                tid = f"{cid}-{suf}"
                conn.execute("INSERT OR IGNORE INTO tours VALUES (?, ?, ?, ?, ?, 'MXN', ?, ?)",
                             (tid, cid, tname, kind, price * 100, int(cert), f"PROD-{tid}"))
                for d in range(days):
                    for h in hours:
                        start = datetime.combine(today + timedelta(days=d), time(h), timezone.utc)
                        sid = f"{tid}-{start:%Y%m%d%H}"
                        conn.execute("INSERT OR IGNORE INTO slots (id, tour_id, start_at, capacity, "
                                     "trytn_slot_id) VALUES (?, ?, ?, ?, ?)",
                                     (sid, tid, start.isoformat(timespec="seconds"), cap, f"TS-{sid}"))


if __name__ == "__main__":
    seed(Database(get_settings().database_path))
    print("Datos de ejemplo cargados.")
