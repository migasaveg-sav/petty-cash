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
    Busca, entre todos los gastos pendientes (sin facturas ya asignadas manualmente) y
    todas las facturas disponibles en el pool, la mejor asignación global posible por
    monto. Regresa una lista de sugerencias:
    {"idx", "gasto", "monto_gasto", "factura", "diferencia", "tipo": "exacto"|"revision"}
    Cada factura del pool se sugiere para un solo gasto, y cada gasto recibe a lo más
    una sugerencia.

    La asignación se hace por "mejor par primero" (se ordenan TODAS las combinaciones
    gasto-factura candidatas por qué tan cerca está su monto, de menor a mayor
    diferencia, y se van tomando en ese orden mientras ambos lados sigan libres) en vez
    de recorrer los gastos en el orden en que vienen y quedarse con la factura más
    cercana todavía disponible en ese momento. La diferencia importa: con el orden de
    llegada, un gasto sin factura real (o con una coincidencia mediocre) puede "robarse"
    la factura que en realidad es un match exacto de un gasto que se procesa después,
    dejando ambos mal emparejados. Ordenar por calidad de coincidencia primero asegura
    que los matches exactos (diferencia 0) siempre se asignen antes que cualquier
    coincidencia "a revisar", sin importar en qué posición aparezca cada gasto.

    `facturas_por_gasto` se recibe como parámetro (en vez de leerse de st.session_state)
    para que esta función se pueda probar sin Streamlit.
    """
    candidatos: list[tuple[float, int, dict[str, Any], float]] = []
    for idx in pendientes_idx:
        if facturas_por_gasto.get(idx):
            continue  # este gasto ya tiene facturas asignadas manualmente
        if idx not in df.index:
            continue
        monto_gasto = abs(float(df.loc[idx, "Monto"]))
        umbral = max(UMBRAL_SUGERENCIA_ABS, monto_gasto * UMBRAL_SUGERENCIA_PCT)
        for factura in pool:
            diferencia_abs = abs(monto_gasto - abs(factura["Monto Total"]))
            if diferencia_abs <= umbral:
                candidatos.append((diferencia_abs, idx, factura, monto_gasto))

    # Orden estable: a igualdad de diferencia, respeta el orden original de
    # pendientes_idx/pool para que el resultado sea determinista.
    candidatos.sort(key=lambda c: c[0])

    usados_idx: set[int] = set()
    usados_pool_ids: set[Any] = set()
    sugerencias: list[dict[str, Any]] = []
    for diferencia_abs, idx, factura, monto_gasto in candidatos:
        if idx in usados_idx or factura["_id"] in usados_pool_ids:
            continue
        diferencia = round(monto_gasto - abs(factura["Monto Total"]), 2)
        tipo = "exacto" if abs(diferencia) <= 0.01 else "revision"
        sugerencias.append({
            "idx": idx, "gasto": df.loc[idx], "monto_gasto": monto_gasto,
            "factura": factura, "diferencia": diferencia, "tipo": tipo,
        })
        usados_idx.add(idx)
        usados_pool_ids.add(factura["_id"])

    sugerencias.sort(key=lambda s: pendientes_idx.index(s["idx"]))
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
