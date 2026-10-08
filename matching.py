"""Emparejamiento automático de gastos bancarios contra facturas (CFDI), y
resúmenes por estado. Lógica pura, sin Streamlit.
"""
from __future__ import annotations

import datetime
from typing import Any

import pandas as pd

# Umbral para sugerir una coincidencia "a revisar": diferencia absoluta menor a este
# monto O menor al 30% del gasto (lo que sea más laxo), para no inundar de sugerencias
# absurdas cuando el monto no se parece en nada.
UMBRAL_SUGERENCIA_ABS = 500.0
UMBRAL_SUGERENCIA_PCT = 0.30

# Diferencia máxima (en pesos) entre el monto de un gasto y la suma de sus facturas
# que todavía se considera "cuadrado" -tanto para clasificar una sugerencia del
# emparejamiento automático como "exacto" (en vez de "a revisar"), como para que el
# diálogo "Trabajar gasto" deje comprobar un gasto, como para el checksum de
# reconciliación (qué comprobados quedan "descuadrados"). Antes era 1 centavo (0.01);
# a petición del usuario ahora tolera hasta 5 centavos (0.05) de diferencia por
# redondeos menores, sin exigir que cuadre al centavo exacto.
DIFERENCIA_ACEPTABLE = 0.05

ESTADOS = ("pendiente", "pendiente_detalles", "comprobado", "no_necesario")


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
        tipo = "exacto" if abs(diferencia) <= DIFERENCIA_ACEPTABLE else "revision"
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


def _fecha_movimiento(registro: dict) -> pd.Timestamp:
    """Fecha del movimiento bancario de un registro (puede ser Timestamp, texto ISO o
    DD/MM/AAAA según venga de la sesión o de un .json). NaT si no se puede leer."""
    valor = registro.get("Fecha Estado")
    if valor is None or valor == "":
        return pd.NaT
    try:
        # Ojo: con dayfirst=True pandas también voltea fechas ISO (2026-10-03 -> 3 de
        # marzo), así que sólo se usa en texto que NO empieza con AAAA-: "03/10/2026"
        # es formato mexicano DD/MM/AAAA, no MM/DD/AAAA.
        es_texto_no_iso = isinstance(valor, str) and not valor.strip()[:4].isdigit()
        return pd.to_datetime(valor, errors="coerce", dayfirst=es_texto_no_iso)
    except (TypeError, ValueError):
        return pd.NaT


def ordenar_registros_por_fecha(registros: list[dict]) -> list[dict]:
    """Del gasto más antiguo al más reciente (fecha del movimiento en el estado de
    cuenta); a igual fecha, por la posición del movimiento en el estado de cuenta
    (`idx`). Los que no tienen una fecha legible van al final, también por `idx`."""
    def clave(r: dict):
        f = _fecha_movimiento(r)
        idx = r.get("idx")
        idx = idx if isinstance(idx, (int, float)) else 10**9
        return (pd.isna(f), f if not pd.isna(f) else pd.Timestamp.min, idx)
    return sorted(registros, key=clave)


def ordenar_registros_por_comprobacion(registros: list[dict]) -> list[dict]:
    """En el orden en que se fueron comprobando (campo `Fecha Comprobación`, texto ISO).
    Los registros que no lo traen -guardados antes de que existiera ese campo- se
    consideran los más antiguos y conservan su orden original en la lista; a igual
    fecha también se respeta el orden original (`sorted` es estable)."""
    def clave(r: dict):
        ts = r.get("Fecha Comprobación") or ""
        return (bool(ts), str(ts))
    return sorted(registros, key=clave)


# Monto acumulado de gasto a partir del cual se debe solicitar el reembolso (caja chica).
LIMITE_REEMBOLSO_DEFAULT = 50000.0


def _fecha_de_solicitud(sol: dict) -> datetime.date | None:
    """Fecha del gasto registrado en la bitácora (guardada como texto ISO AAAA-MM-DD,
    o ya como date). None si no la tiene o no se puede leer -p. ej. solicitudes
    registradas antes de que existiera este campo-."""
    valor = sol.get("Fecha")
    if isinstance(valor, datetime.datetime):
        return valor.date()
    if isinstance(valor, datetime.date):
        return valor
    if isinstance(valor, str) and valor.strip():
        try:
            return datetime.date.fromisoformat(valor.strip()[:10])
        except ValueError:
            return None
    return None


def _monto_de_solicitud(sol: dict) -> float:
    try:
        return float(sol.get("Monto") or 0)
    except (TypeError, ValueError):
        return 0.0


def resumen_gasto_bitacora(solicitudes: list[dict], fecha_consulta: datetime.date) -> dict[str, Any]:
    """Gasto TENTATIVO registrado en la bitácora de solicitudes hasta `fecha_consulta`:
    el del propio día, el de la semana (lunes a `fecha_consulta`) y el acumulado de
    todo lo registrado hasta esa fecha -es lo que se compara contra el límite de
    reembolso-. Las solicitudes sin fecha (anteriores a este campo) no se pueden ubicar
    en un día ni semana, pero sí cuentan en el acumulado (y se reportan en `sin_fecha`).
    Los registros con fecha posterior a la de consulta no se incluyen.
    Devuelve además `por_dia`: lista [(fecha, total, cantidad)] más reciente primero."""
    inicio_semana = fecha_consulta - datetime.timedelta(days=fecha_consulta.weekday())
    dia = semana = acumulado = 0.0
    n_dia = n_semana = n_acumulado = n_sin_fecha = 0
    por_dia: dict[datetime.date, list] = {}
    for sol in solicitudes:
        monto = _monto_de_solicitud(sol)
        fecha = _fecha_de_solicitud(sol)
        if fecha is None:
            acumulado += monto
            n_acumulado += 1
            n_sin_fecha += 1
            continue
        if fecha > fecha_consulta:
            continue
        acumulado += monto
        n_acumulado += 1
        acum_dia = por_dia.setdefault(fecha, [0.0, 0])
        acum_dia[0] += monto
        acum_dia[1] += 1
        if fecha >= inicio_semana:
            semana += monto
            n_semana += 1
        if fecha == fecha_consulta:
            dia += monto
            n_dia += 1
    return {
        "dia": round(dia, 2), "semana": round(semana, 2), "acumulado": round(acumulado, 2),
        "n_dia": n_dia, "n_semana": n_semana, "n_acumulado": n_acumulado, "sin_fecha": n_sin_fecha,
        "inicio_semana": inicio_semana,
        "por_dia": [(f, round(v[0], 2), v[1]) for f, v in sorted(por_dia.items(), reverse=True)],
    }


def diferencia_gasto_facturas(
    monto_gasto: float, facturas: list[dict[str, Any]], propina: float = 0.0
) -> float:
    """Diferencia entre el monto del gasto bancario y la suma de montos de las facturas
    asignadas (más la propina, si la hay). La suma de facturas se toma con signo (no
    valor absoluto) para que una nota de crédito incluida en la lista (monto negativo)
    reste del total, en vez de sumarse como si fuera otra factura más. `propina` cubre
    el caso de categorías como "Client Entertainment"/"Travel Meal", donde el cargo
    bancario puede incluir una propina que no aparece en el CFDI -sin esto, ese gasto
    nunca cuadraría contra su(s) factura(s)-. Redondeada a centavos."""
    suma_facturas = sum(f.get("Monto Total", 0.0) for f in facturas) + (propina or 0.0)
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
        diff = diferencia_gasto_facturas(
            reg.get("Monto Estado", 0.0), reg.get("Facturas", []), reg.get("Propina", 0.0)
        )
        if abs(diff) > DIFERENCIA_ACEPTABLE:
            descuadrados.append({"idx": reg.get("idx"), "diferencia": diff})

    return {
        "total_estado_cuenta": total_general,
        "total_por_estados": total_por_estados,
        # Este "cuadra" es una identidad contable (cada movimiento del estado de
        # cuenta cae en exactamente un estado): debe dar igual salvo ruido de
        # redondeo de punto flotante, así que se queda en 1 centavo -no es la
        # misma tolerancia de negocio que DIFERENCIA_ACEPTABLE- para no esconder
        # un gasto que quedó sin clasificar o clasificado dos veces.
        "cuadra": abs(total_general - total_por_estados) <= 0.01,
        "comprobados_descuadrados": descuadrados,
    }
