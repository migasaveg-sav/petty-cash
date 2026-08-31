"""Emparejamiento automático de gastos bancarios contra facturas (CFDI), y
resúmenes por estado. Lógica pura, sin Streamlit.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

# Umbral para sugerir una coincidencia "a revisar": diferencia absoluta menor a este
# monto O menor al 30% del gasto (lo que sea más laxo), para no inundar de sugerencias
# absurdas cuando el monto no se parece en nada.
UMBRAL_SUGERENCIA_ABS = 500.0
UMBRAL_SUGERENCIA_PCT = 0.30

ESTADOS = ("pendiente", "comprobado", "no_necesario")


def calcular_matches_automaticos(
    df: pd.DataFrame,
    pendientes_idx: list[int],
    pool: list[dict[str, Any]],
    facturas_por_gasto: dict[int, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """
    Para cada gasto pendiente (sin facturas ya asignadas manualmente), busca la factura
    disponible en el pool cuyo monto esté más cercano. Regresa una lista de sugerencias:
    {"idx", "gasto", "monto_gasto", "factura", "diferencia", "tipo": "exacto"|"revision"}
    Cada factura del pool se sugiere para un solo gasto (primero en llegar, primero en servir).

    `facturas_por_gasto` se recibe como parámetro (en vez de leerse de st.session_state)
    para que esta función se pueda probar sin Streamlit.
    """
    usados_pool_ids: set[Any] = set()
    sugerencias: list[dict[str, Any]] = []
    for idx in pendientes_idx:
        if facturas_por_gasto.get(idx):
            continue  # este gasto ya tiene facturas asignadas manualmente
        if idx not in df.index:
            continue
        gasto = df.loc[idx]
        monto_gasto = abs(float(gasto["Monto"]))
        disponibles = [f for f in pool if f["_id"] not in usados_pool_ids]
        if not disponibles:
            continue
        mejor = min(disponibles, key=lambda f: abs(monto_gasto - abs(f["Monto Total"])))
        diferencia = round(monto_gasto - abs(mejor["Monto Total"]), 2)
        umbral = max(UMBRAL_SUGERENCIA_ABS, monto_gasto * UMBRAL_SUGERENCIA_PCT)
        if abs(diferencia) <= umbral:
            tipo = "exacto" if abs(diferencia) <= 0.01 else "revision"
            sugerencias.append({
                "idx": idx, "gasto": gasto, "monto_gasto": monto_gasto,
                "factura": mejor, "diferencia": diferencia, "tipo": tipo,
            })
            usados_pool_ids.add(mejor["_id"])
    return sugerencias


def resumen_estados(df: pd.DataFrame, estados: dict[int, str]) -> dict[str, dict[str, Any]]:
    """Cuenta y suma movimientos por estado (pendiente/comprobado/no_necesario)."""
    resumen: dict[str, dict[str, Any]] = {}
    for estado in ESTADOS:
        idxs = [i for i, e in estados.items() if e == estado]
        sub = df.loc[df.index.intersection(idxs)]
        total = float(sub["Monto"].abs().sum()) if not sub.empty else 0.0
        resumen[estado] = {"count": len(idxs), "total": total}
    return resumen


def diferencia_gasto_facturas(monto_gasto: float, facturas: list[dict[str, Any]]) -> float:
    """Diferencia entre el monto del gasto bancario y la suma de montos de las facturas
    asignadas. La suma se toma con signo (no valor absoluto) para que una nota de
    crédito incluida en la lista (monto negativo) reste del total, en vez de sumarse
    como si fuera otra factura más. Redondeada a centavos."""
    suma_facturas = sum(f.get("Monto Total", 0.0) for f in facturas)
    return round(abs(monto_gasto) - suma_facturas, 2)


def checksum_reconciliacion(
    df: pd.DataFrame,
    estados: dict[int, str],
    concatenados: list[dict[str, Any]],
) -> dict[str, Any]:
    """Resumen de control: compara el total del estado de cuenta contra la suma de
    comprobados + no_necesarios + pendientes, y separa los comprobados cuya diferencia
    factura-vs-gasto no cuadra exactamente (por si se guardaron con una diferencia
    tolerada). Útil como último chequeo antes de cerrar el periodo.
    """
    resumen = resumen_estados(df, estados)
    total_general = float(df["Monto"].abs().sum()) if not df.empty else 0.0
    total_por_estados = sum(r["total"] for r in resumen.values())

    descuadrados = []
    for reg in concatenados:
        diff = diferencia_gasto_facturas(reg.get("Monto Estado", 0.0), reg.get("Facturas", []))
        if abs(diff) > 0.01:
            descuadrados.append({"idx": reg.get("idx"), "diferencia": diff})

    return {
        "total_estado_cuenta": total_general,
        "total_por_estados": total_por_estados,
        "cuadra": abs(total_general - total_por_estados) <= 0.01,
        "comprobados_descuadrados": descuadrados,
    }
