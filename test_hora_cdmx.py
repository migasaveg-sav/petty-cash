"""La app corre en servidores con otra zona horaria (UTC, etc.); la hora que se muestra y
se guarda debe ser siempre la de la CDMX (UTC-6)."""
import datetime
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import persistence
from persistence import ahora_cdmx, hoy_cdmx


def test_ahora_cdmx_es_utc_menos_6_sin_importar_la_zona_del_servidor(monkeypatch):
    for tz in ("UTC", "Asia/Tokyo", "America/New_York"):
        monkeypatch.setenv("TZ", tz)
        time.tzset()
        esperado = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(hours=6)
        assert abs((ahora_cdmx() - esperado).total_seconds()) < 5, tz
    monkeypatch.undo()
    time.tzset()


def test_hoy_cdmx_no_salta_al_dia_siguiente_a_las_6pm_cdmx(monkeypatch):
    # 2026-10-09 02:30 UTC = 2026-10-08 20:30 en la CDMX -> "hoy" debe ser el 8
    class FakeDT(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 9, 2, 30, tzinfo=datetime.timezone.utc).astimezone(tz)

    monkeypatch.setattr(persistence.datetime, "datetime", FakeDT)
    assert hoy_cdmx() == datetime.date(2026, 10, 8)
    assert ahora_cdmx().strftime("%Y-%m-%d %H-%M") == "2026-10-08 20-30"
