"""Pruebas del gasto tentativo de la bitácora de solicitudes (día / semana / acumulado)."""
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from matching import resumen_gasto_bitacora

D = datetime.date


def _sol(fecha, monto):
    return {"Fecha": fecha, "Monto": monto}


def test_dia_semana_y_acumulado():
    # 2026-10-08 es jueves -> la semana empieza el lunes 2026-10-05
    sols = [
        _sol("2026-09-30", 100),   # semana anterior: sólo acumulado
        _sol("2026-10-04", 50),    # domingo anterior: sólo acumulado
        _sol("2026-10-05", 200),   # lunes: semana
        _sol("2026-10-08", 300),   # el día de consulta
        _sol("2026-10-08", 25.5),  # el día de consulta
    ]
    r = resumen_gasto_bitacora(sols, D(2026, 10, 8))
    assert r["inicio_semana"] == D(2026, 10, 5)
    assert r["dia"] == 325.5 and r["n_dia"] == 2
    assert r["semana"] == 525.5 and r["n_semana"] == 3
    assert r["acumulado"] == 675.5 and r["n_acumulado"] == 5


def test_registros_posteriores_a_la_consulta_no_cuentan():
    r = resumen_gasto_bitacora([_sol("2026-10-09", 999), _sol("2026-10-07", 10)], D(2026, 10, 8))
    assert r["acumulado"] == 10 and r["dia"] == 0 and r["semana"] == 10


def test_solicitud_sin_fecha_o_sin_monto_no_truena():
    sols = [{"Applicant": "x"}, _sol(None, 40), _sol("no-es-fecha", "12.5"), _sol("2026-10-08", None), _sol("2026-10-08", "abc")]
    r = resumen_gasto_bitacora(sols, D(2026, 10, 8))
    # las 3 sin fecha válida cuentan en el acumulado (40 + 12.5) pero no en día/semana
    assert r["acumulado"] == 52.5 and r["sin_fecha"] == 3
    assert r["dia"] == 0 and r["semana"] == 0


def test_desglose_por_dia_mas_reciente_primero():
    r = resumen_gasto_bitacora([_sol("2026-10-06", 10), _sol("2026-10-08", 5), _sol("2026-10-06", 1)], D(2026, 10, 8))
    assert r["por_dia"] == [(D(2026, 10, 8), 5.0, 1), (D(2026, 10, 6), 11.0, 2)]


def test_semana_cuando_la_consulta_es_lunes():
    r = resumen_gasto_bitacora([_sol("2026-10-04", 7), _sol("2026-10-05", 3)], D(2026, 10, 5))
    assert r["inicio_semana"] == D(2026, 10, 5) and r["semana"] == 3 and r["dia"] == 3


# ---- Orden de las hojas de comprobados ----
from matching import ordenar_registros_por_comprobacion, ordenar_registros_por_fecha


def _reg(idx, fecha, comprobado=None):
    r = {"idx": idx, "Fecha Estado": fecha}
    if comprobado:
        r["Fecha Comprobación"] = comprobado
    return r


def test_orden_por_fecha_del_gasto_aunque_el_estado_de_cuenta_venga_al_reves():
    # estado de cuenta del más reciente al más antiguo (idx 0 = más nuevo)
    regs = [_reg(0, "2026-10-05"), _reg(1, "2026-10-01"), _reg(2, "2026-10-03"), _reg(3, "2026-10-03")]
    assert [r["idx"] for r in ordenar_registros_por_fecha(regs)] == [1, 2, 3, 0]


def test_orden_por_fecha_acepta_formatos_y_manda_ilegibles_al_final():
    regs = [_reg(0, ""), _reg(1, "03/10/2026"), _reg(2, pd_ts := __import__("pandas").Timestamp("2026-10-02")), _reg(3, "basura")]
    assert [r["idx"] for r in ordenar_registros_por_fecha(regs)] == [2, 1, 0, 3]


def test_orden_por_comprobacion_usa_la_hora_y_los_viejos_sin_marca_van_primero():
    regs = [
        _reg(5, "2026-10-01", "2026-10-08T12:00:00"),
        _reg(7, "2026-09-01"),                          # sin marca (guardado antes del campo)
        _reg(2, "2026-10-09", "2026-10-08T09:30:00"),
        _reg(1, "2026-09-02"),                          # sin marca: conserva su orden relativo
    ]
    assert [r["idx"] for r in ordenar_registros_por_comprobacion(regs)] == [7, 1, 2, 5]


def test_fecha_iso_no_se_voltea_como_dia_mes():
    # 2026-10-03 debe ser 3 de octubre (no 10 de marzo) y 2026-03-10 el 10 de marzo.
    regs = [_reg(0, "2026-10-03"), _reg(1, "2026-03-10"), _reg(2, "03/10/2026")]
    assert [r["idx"] for r in ordenar_registros_por_fecha(regs)] == [1, 0, 2]
