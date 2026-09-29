import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from matching import (
    calcular_matches_automaticos,
    checksum_reconciliacion,
    diferencia_gasto_facturas,
    resumen_estados,
)


def _df():
    return pd.DataFrame(
        {
            "Fecha": ["2026-06-01", "2026-06-02", "2026-06-03"],
            "Descripción": ["Gasto A", "Gasto B", "Gasto C"],
            "Monto": [-150.00, -300.00, -75.25],
        }
    )


def _factura(fid, monto, uuid=None):
    return {"_id": fid, "UUID": uuid or f"uuid-{fid}", "Monto Total": monto, "RFC Emisor": "X", "Fecha Factura": "2026-06-01"}


def test_match_exacto():
    df = _df()
    pool = [_factura(1, 150.00), _factura(2, 999.00)]
    sugerencias = calcular_matches_automaticos(df, [0, 1, 2], pool, {})
    exactas = [s for s in sugerencias if s["tipo"] == "exacto"]
    assert len(exactas) == 1
    assert exactas[0]["idx"] == 0


def test_gasto_con_factura_manual_se_excluye():
    df = _df()
    pool = [_factura(1, 150.00)]
    facturas_por_gasto = {0: [_factura(99, 150.00)]}
    sugerencias = calcular_matches_automaticos(df, [0, 1, 2], pool, facturas_por_gasto)
    assert all(s["idx"] != 0 for s in sugerencias)


def test_factura_no_se_reusa_dos_veces():
    df = pd.DataFrame({"Fecha": ["a", "b"], "Descripción": ["x", "y"], "Monto": [-100.0, -100.0]})
    pool = [_factura(1, 100.0)]
    sugerencias = calcular_matches_automaticos(df, [0, 1], pool, {})
    assert len(sugerencias) == 1


def test_resumen_estados():
    df = _df()
    estados = {0: "pendiente", 1: "comprobado", 2: "no_necesario"}
    resumen = resumen_estados(df, estados)
    assert resumen["pendiente"]["count"] == 1
    assert resumen["comprobado"]["total"] == 300.00
    assert resumen["no_necesario"]["total"] == 75.25


def test_checksum_reconciliacion_cuadra():
    df = _df()
    estados = {0: "comprobado", 1: "no_necesario", 2: "pendiente"}
    concatenados = [
        {"idx": 0, "Monto Estado": -150.00, "Facturas": [_factura(1, 150.00)]},
    ]
    resultado = checksum_reconciliacion(df, estados, concatenados)
    assert resultado["cuadra"] is True
    assert resultado["comprobados_descuadrados"] == []


def test_match_exacto_no_es_bloqueado_por_gasto_anterior_con_monto_parecido():
    """Regresión del reporte real: un gasto SIN factura real (o con una
    coincidencia mediocre) que aparece antes en la lista no debe "robarse" la
    factura que en realidad es un match exacto de un gasto posterior. Antes,
    el algoritmo recorría los gastos en orden de llegada y se quedaba con la
    factura más cercana todavía disponible en ese momento; con datos reales
    (estado de cuenta + CFDIs) eso dejaba sólo 2 de 15 facturas como "exacto"
    en vez de 13, porque gastos sin match real consumían facturas ajenas."""
    df = pd.DataFrame(
        {
            "Fecha": ["2026-06-01", "2026-06-02"],
            "Descripción": ["Gasto sin factura real", "Gasto B"],
            # 85.10 no tiene factura real; 89.95 sí la tiene exacta más abajo.
            "Monto": [-85.10, -89.95],
        }
    )
    pool = [_factura(1, 89.95)]
    sugerencias = calcular_matches_automaticos(df, [0, 1], pool, {})
    assert len(sugerencias) == 1
    assert sugerencias[0]["idx"] == 1
    assert sugerencias[0]["tipo"] == "exacto"


def test_matches_se_regresan_en_orden_de_pendientes_idx():
    df = pd.DataFrame(
        {
            "Fecha": ["2026-06-01", "2026-06-02"],
            "Descripción": ["Gasto A", "Gasto B"],
            "Monto": [-100.0, -50.0],
        }
    )
    pool = [_factura(1, 50.0), _factura(2, 100.0)]
    sugerencias = calcular_matches_automaticos(df, [0, 1], pool, {})
    assert [s["idx"] for s in sugerencias] == [0, 1]


def test_diferencia_gasto_facturas_sin_propina():
    # Comportamiento de siempre cuando no se pasa propina (default 0.0).
    assert diferencia_gasto_facturas(150.0, [_factura(1, 150.0)]) == 0.0


def test_diferencia_gasto_facturas_con_propina_cuadra():
    # Cargo bancario de 180 = factura de 150 + propina de 30 (ej. "Client
    # Entertainment"/"Travel Meal", donde la propina no aparece en el CFDI).
    diferencia = diferencia_gasto_facturas(180.0, [_factura(1, 150.0)], propina=30.0)
    assert diferencia == 0.0


def test_diferencia_gasto_facturas_con_propina_no_cuadra():
    diferencia = diferencia_gasto_facturas(180.0, [_factura(1, 150.0)], propina=10.0)
    assert diferencia == 20.0


def test_checksum_reconciliacion_usa_propina_del_registro():
    df = pd.DataFrame(
        {
            "Fecha": ["2026-06-01"],
            "Descripción": ["Cena con cliente"],
            "Monto": [-180.00],
        }
    )
    estados = {0: "comprobado"}
    concatenados = [
        {"idx": 0, "Monto Estado": -180.00, "Facturas": [_factura(1, 150.00)], "Propina": 30.00},
    ]
    resultado = checksum_reconciliacion(df, estados, concatenados)
    assert resultado["comprobados_descuadrados"] == []


def test_checksum_reconciliacion_sin_propina_detecta_descuadre():
    # Mismo caso, pero si "Propina" no se guarda, el registro queda descuadrado
    # -por eso dialog_trabajar_gasto debe guardar "Propina" en cada registro.
    df = pd.DataFrame(
        {
            "Fecha": ["2026-06-01"],
            "Descripción": ["Cena con cliente"],
            "Monto": [-180.00],
        }
    )
    estados = {0: "comprobado"}
    concatenados = [
        {"idx": 0, "Monto Estado": -180.00, "Facturas": [_factura(1, 150.00)]},
    ]
    resultado = checksum_reconciliacion(df, estados, concatenados)
    assert len(resultado["comprobados_descuadrados"]) == 1
