"""Persistencia de la sesión de trabajo:

1. Serialización a JSON para descargar/subir un "avance" manualmente (igual que la
   versión original, pero corregido para incluir pool_facturas, catálogos y
   modo_trabajo, que antes se perdían al recargar un avance guardado).
2. Autoguardado en SQLite como red de seguridad: cada cierto número de acciones (o
   al cerrar un gasto) se guarda una copia local en disco, para poder recuperar el
   trabajo si el navegador se cierra sin que el usuario haya descargado el .json.
3. Traslado de progreso entre dos estados de cuenta distintos (`emparejar_filas_por_contenido`
   / `trasladar_progreso`): un estado de cuenta nuevo normalmente es una versión más
   amplia del mismo periodo (vuelve a traer lo de antes más movimientos nuevos hasta
   una fecha más reciente), no un archivo sin relación con el anterior. Antes, tanto
   subir un estado de cuenta nuevo como restaurar un avance (.json) viejo reemplazaban
   TODO -incluido el propio estado de cuenta- sin ningún emparejamiento, así que el
   progreso ya trabajado (qué gastos están comprobados, sus facturas, la bitácora) se
   perdía o quedaba apuntando a las filas equivocadas en cuanto cambiaba el archivo.
   Estas dos funciones permiten trasladar ese progreso de un estado de cuenta a otro
   emparejando cada movimiento por Fecha+Descripción+Monto, en vez de por posición de
   fila (que no se puede asumir estable entre dos exportes distintos del banco).

Todo aquí es independiente de Streamlit para poder probarse con pytest.
"""
from __future__ import annotations

import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

CAMPOS_SESION = [
    "banco",
    "bank_file_id",
    "estados",
    "facturas_por_gasto",
    "facturas_por_gasto_grupo",
    "clasificacion_por_gasto",
    "pool_facturas",
    "modo_trabajo",
    "concatenados",
    "no_necesarios",
    "factura_counter",
    "categorias",
    "materiales",
    "solicitudes",
    "solicitud_counter",
    "categorias_solicitud",
    "empleados",
    "aplicantes",
]

DB_DEFAULT_PATH = "pettycash_autosave.db"


def _json_default(obj: Any) -> Any:
    """Convierte a tipos nativos cualquier valor que json.dumps no sepa serializar
    (Timestamps/fechas de pandas, numpy.int64/float64, NaN, etc.)."""
    if isinstance(obj, (pd.Timestamp, datetime.date, datetime.datetime)):
        return obj.isoformat()
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return None if np.isnan(obj) else float(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    try:
        if pd.isna(obj):
            return None
    except (TypeError, ValueError):
        pass
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def construir_sesion_dict(state: dict[str, Any]) -> dict[str, Any]:
    """Empaqueta todo el estado de trabajo (incluyendo el propio estado de cuenta) en
    un dict serializable. `state` es un dict plano equivalente a st.session_state
    (o el propio st.session_state, que soporta el mismo acceso por llave)."""
    df = state.get("bank_df")
    data: dict[str, Any] = {
        "version": 2,
        "guardado_en": datetime.datetime.now().isoformat(timespec="seconds"),
        "bank_df": df.to_dict(orient="split") if df is not None else None,
    }
    for campo in CAMPOS_SESION:
        valor = state.get(campo)
        if campo in ("estados", "facturas_por_gasto", "clasificacion_por_gasto"):
            valor = {str(k): v for k, v in (valor or {}).items()}
        data[campo] = valor
    return data


def sesion_a_json_bytes(state: dict[str, Any]) -> bytes:
    return json.dumps(
        construir_sesion_dict(state), ensure_ascii=False, indent=2, default=_json_default
    ).encode("utf-8")


def cargar_sesion_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Reconstruye un dict de estado (compatible con st.session_state.update(...))
    a partir de un dict previamente generado por construir_sesion_dict()."""
    bank_df_data = data.get("bank_df")
    if bank_df_data is not None:
        bank_df = pd.DataFrame(
            data=bank_df_data["data"],
            columns=bank_df_data["columns"],
            index=bank_df_data["index"],
        )
    else:
        bank_df = None

    resultado: dict[str, Any] = {"bank_df": bank_df}
    resultado["banco"] = data.get("banco")
    resultado["bank_file_id"] = tuple(data["bank_file_id"]) if data.get("bank_file_id") else None
    resultado["estados"] = {int(k): v for k, v in data.get("estados", {}).items()}
    resultado["facturas_por_gasto"] = {
        int(k): v for k, v in data.get("facturas_por_gasto", {}).items()
    }
    # Facturas en curso (todavía sin guardar) de un grupo de "comprobar varios
    # gastos con 1 factura" que se cerró a medias: la llave ya es un string
    # ("10,15,22", los idx del grupo ordenados y unidos con comas) tanto en
    # session_state como en el JSON, así que no necesita el mismo cast a int
    # que "estados"/"facturas_por_gasto"/"clasificacion_por_gasto".
    resultado["facturas_por_gasto_grupo"] = data.get("facturas_por_gasto_grupo", {})
    resultado["clasificacion_por_gasto"] = {
        int(k): v for k, v in data.get("clasificacion_por_gasto", {}).items()
    }
    resultado["pool_facturas"] = data.get("pool_facturas", [])
    resultado["modo_trabajo"] = data.get("modo_trabajo")
    resultado["concatenados"] = data.get("concatenados", [])
    resultado["no_necesarios"] = data.get("no_necesarios", [])
    resultado["factura_counter"] = data.get("factura_counter", 0)
    resultado["categorias"] = data.get("categorias")
    resultado["materiales"] = data.get("materiales")
    resultado["solicitudes"] = data.get("solicitudes", [])
    resultado["solicitud_counter"] = data.get("solicitud_counter", 0)
    resultado["categorias_solicitud"] = data.get("categorias_solicitud")
    resultado["empleados"] = data.get("empleados")
    resultado["aplicantes"] = data.get("aplicantes")
    resultado["selected_idx"] = None
    return resultado


def sesion_vacia() -> dict[str, Any]:
    """Estado inicial en blanco (equivalente a 'reiniciar todo')."""
    return {
        "bank_df": None,
        "bank_file_id": None,
        "banco": None,
        "estados": {},
        "facturas_por_gasto": {},
        "facturas_por_gasto_grupo": {},
        "clasificacion_por_gasto": {},
        "pool_facturas": [],
        "modo_trabajo": None,
        "concatenados": [],
        "no_necesarios": [],
        "factura_counter": 0,
        "categorias": None,
        "materiales": None,
        "solicitudes": [],
        "solicitud_counter": 0,
        "categorias_solicitud": None,
        "empleados": None,
        "aplicantes": None,
        "selected_idx": None,
    }


# ============================================================
# TRASLADO DE PROGRESO ENTRE DOS ESTADOS DE CUENTA DISTINTOS
# ============================================================
def emparejar_filas_por_contenido(df_viejo: pd.DataFrame, df_nuevo: pd.DataFrame) -> dict[int, int]:
    """Empareja cada fila de un estado de cuenta anterior (`df_viejo`) con la fila
    del estado de cuenta nuevo (`df_nuevo`) que represente el mismo movimiento
    bancario -misma Fecha, Descripción y Monto-, para poder trasladar el progreso
    ya trabajado (estados, facturas asignadas, comprobaciones) cuando se carga un
    estado de cuenta más reciente que amplía/reemplaza al anterior (típico: un
    exporte acumulado del banco que ahora llega hasta una fecha posterior, pero
    que no tiene por qué traer los movimientos en las mismas posiciones -o con el
    mismo número total de filas- que el exporte anterior).

    Si hay varias filas idénticas de un lado o del otro (montos/fechas/conceptos
    repetidos, común en comisiones o cargos genéricos), se emparejan en el mismo
    orden en que aparecen -la 1a fila vieja con esa clave con la 1a fila nueva con
    esa misma clave, la 2a con la 2a, etc.- en vez de que la primera coincidencia
    se quede con todas y deje las demás sin pareja.

    Regresa {idx_viejo: idx_nuevo} sólo para las filas que sí encontraron pareja.
    Una fila vieja sin pareja ya no aparece en el estado de cuenta nuevo (poco
    común: el banco corrigió o quitó un movimiento). Una fila nueva sin pareja es
    un movimiento que no estaba antes -lo normal, la razón de cargar un estado de
    cuenta más reciente-."""

    def _clave(fila: pd.Series) -> tuple:
        try:
            monto = round(float(fila.get("Monto", 0.0)), 2)
        except (TypeError, ValueError):
            monto = None
        return (
            str(fila.get("Fecha", "")).strip(),
            str(fila.get("Descripción", "")).strip(),
            monto,
        )

    disponibles: dict[tuple, list[int]] = {}
    for idx_nuevo, fila in df_nuevo.iterrows():
        disponibles.setdefault(_clave(fila), []).append(idx_nuevo)

    mapeo: dict[int, int] = {}
    for idx_viejo, fila in df_viejo.iterrows():
        candidatos = disponibles.get(_clave(fila))
        if candidatos:
            mapeo[idx_viejo] = candidatos.pop(0)
    return mapeo


def trasladar_progreso(
    df_viejo: pd.DataFrame,
    df_nuevo: pd.DataFrame,
    estados: dict[int, str],
    facturas_por_gasto: dict[int, list],
    facturas_por_gasto_grupo: dict[str, list],
    clasificacion_por_gasto: dict[int, dict],
    concatenados: list[dict],
    no_necesarios: list[dict],
    solicitudes: list[dict] | None = None,
) -> dict[str, Any]:
    """Traslada todo el progreso trabajado sobre `df_viejo` hacia `df_nuevo` (otro
    estado de cuenta, normalmente una versión más reciente/ampliada de la misma
    cuenta), emparejando movimientos por contenido en vez de por posición de fila
    -ver `emparejar_filas_por_contenido`-. Pensado para dos casos:

    1. Se sube un estado de cuenta nuevo encima de uno que ya tenía avance.
    2. Se restaura un avance (.json) guardado sobre un estado de cuenta viejo,
       mientras en la sesión actual ya hay cargado uno más nuevo -en vez de que
       restaurar el avance reemplace el estado de cuenta nuevo por el viejo-.

    Cada idx de `df_nuevo` queda en 'pendiente' salvo que su movimiento haya
    tenido pareja en `df_viejo` con un progreso distinto (comprobado, pendiente de
    detalles, no necesario). Los movimientos del estado de cuenta viejo que ya no
    aparecen en el nuevo (poco común) se excluyen de `concatenados`/`no_necesarios`
    -no hay una fila a la que asignarlos- y se reportan en "movimientos_sin_match_en_nuevo"
    para poder avisar al usuario en vez de desaparecer en silencio."""
    mapeo = emparejar_filas_por_contenido(df_viejo, df_nuevo)

    def _remap(i):
        return mapeo.get(i)

    nuevos_estados: dict[int, str] = {idx_nuevo: "pendiente" for idx_nuevo in df_nuevo.index}
    for idx_viejo, estado in (estados or {}).items():
        idx_nuevo = _remap(idx_viejo)
        if idx_nuevo is not None:
            nuevos_estados[idx_nuevo] = estado

    nuevas_facturas_por_gasto: dict[int, list] = {}
    for idx_viejo, facturas in (facturas_por_gasto or {}).items():
        idx_nuevo = _remap(idx_viejo)
        if idx_nuevo is not None:
            nuevas_facturas_por_gasto[idx_nuevo] = facturas

    nueva_clasificacion: dict[int, dict] = {}
    for idx_viejo, clasif in (clasificacion_por_gasto or {}).items():
        idx_nuevo = _remap(idx_viejo)
        if idx_nuevo is not None:
            nueva_clasificacion[idx_nuevo] = clasif

    def _remap_clave_grupo(clave: str) -> str | None:
        idxs_viejos = [int(x) for x in clave.split(",") if x != ""]
        idxs_nuevos = [_remap(i) for i in idxs_viejos]
        if not idxs_nuevos or any(i is None for i in idxs_nuevos):
            return None
        return ",".join(str(i) for i in sorted(idxs_nuevos))

    nueva_facturas_grupo: dict[str, list] = {}
    for clave, facturas in (facturas_por_gasto_grupo or {}).items():
        nueva_clave = _remap_clave_grupo(clave)
        if nueva_clave is not None:
            nueva_facturas_grupo[nueva_clave] = facturas

    nuevos_concatenados: list[dict] = []
    concatenados_sin_match = 0
    for reg in concatenados or []:
        reg = dict(reg)
        idx_nuevo = _remap(reg.get("idx"))
        if idx_nuevo is None:
            concatenados_sin_match += 1
            continue
        reg["idx"] = idx_nuevo
        if reg.get("GastosDelGrupo"):
            grupo_nuevo = [_remap(i) for i in reg["GastosDelGrupo"]]
            if all(i is not None for i in grupo_nuevo):
                reg["GastosDelGrupo"] = grupo_nuevo
            else:
                # alguno de los compañeros del grupo no tuvo pareja en el estado de
                # cuenta nuevo: este registro sigue siendo válido por sí solo (ya
                # cuadra contra su propia porción prorrateada), sólo se le quita la
                # referencia a un grupo que ya no se puede reconstruir completo.
                reg.pop("GastosDelGrupo", None)
                reg.pop("GrupoCompartido", None)
        nuevos_concatenados.append(reg)
        nuevos_estados[idx_nuevo] = "pendiente_detalles" if reg.get("DetallesPendientes") else "comprobado"

    nuevos_no_necesarios: list[dict] = []
    no_necesarios_sin_match = 0
    for reg in no_necesarios or []:
        reg = dict(reg)
        idx_nuevo = _remap(reg.get("idx"))
        if idx_nuevo is None:
            no_necesarios_sin_match += 1
            continue
        reg["idx"] = idx_nuevo
        nuevos_no_necesarios.append(reg)
        nuevos_estados[idx_nuevo] = "no_necesario"

    nuevas_solicitudes: list[dict] | None = None
    if solicitudes is not None:
        nuevas_solicitudes = []
        for sol in solicitudes:
            sol = dict(sol)
            idx_vinc = sol.get("idx_vinculado")
            if idx_vinc is not None:
                if isinstance(idx_vinc, (list, tuple)):
                    remapeados = [r for r in (_remap(i) for i in idx_vinc) if r is not None]
                    if remapeados:
                        sol["idx_vinculado"] = remapeados[0] if len(remapeados) == 1 else remapeados
                    else:
                        sol["idx_vinculado"] = None
                        if sol.get("estado") in ("comprobado", "pendiente_detalles"):
                            sol["estado"] = "pendiente"
                else:
                    nuevo = _remap(idx_vinc)
                    sol["idx_vinculado"] = nuevo
                    if nuevo is None and sol.get("estado") in ("comprobado", "pendiente_detalles"):
                        sol["estado"] = "pendiente"
            nuevas_solicitudes.append(sol)

    idxs_viejos_sin_match = [i for i in df_viejo.index if i not in mapeo]
    idxs_nuevos_sin_progreso_previo = [i for i in df_nuevo.index if i not in mapeo.values()]

    resultado: dict[str, Any] = {
        "estados": nuevos_estados,
        "facturas_por_gasto": nuevas_facturas_por_gasto,
        "facturas_por_gasto_grupo": nueva_facturas_grupo,
        "clasificacion_por_gasto": nueva_clasificacion,
        "concatenados": nuevos_concatenados,
        "no_necesarios": nuevos_no_necesarios,
        "movimientos_trasladados": len(mapeo),
        "movimientos_nuevos": len(idxs_nuevos_sin_progreso_previo),
        "movimientos_sin_match_en_nuevo": idxs_viejos_sin_match,
        "concatenados_sin_match": concatenados_sin_match,
        "no_necesarios_sin_match": no_necesarios_sin_match,
    }
    if nuevas_solicitudes is not None:
        resultado["solicitudes"] = nuevas_solicitudes
    return resultado


# ============================================================
# AUTOGUARDADO EN SQLITE (red de seguridad local)
# ============================================================
def _conectar(db_path: str = DB_DEFAULT_PATH) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True) if Path(db_path).parent != Path("") else None
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS autosave (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            payload TEXT NOT NULL,
            guardado_en TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def autoguardar(state: dict[str, Any], db_path: str = DB_DEFAULT_PATH) -> None:
    """Sobrescribe el único registro de autoguardado con el estado actual.
    Se usa una sola fila (id=1) porque es una red de seguridad de "última sesión
    de este navegador/servidor", no un historial de versiones."""
    payload = sesion_a_json_bytes(state).decode("utf-8")
    conn = _conectar(db_path)
    try:
        conn.execute(
            "INSERT INTO autosave (id, payload, guardado_en) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload, guardado_en = excluded.guardado_en",
            (payload, datetime.datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()


def hay_autoguardado(db_path: str = DB_DEFAULT_PATH) -> dict[str, Any] | None:
    """Regresa {"guardado_en": ...} si existe un autoguardado previo, o None."""
    if not Path(db_path).exists():
        return None
    conn = _conectar(db_path)
    try:
        row = conn.execute("SELECT guardado_en FROM autosave WHERE id = 1").fetchone()
        return {"guardado_en": row[0]} if row else None
    finally:
        conn.close()


def restaurar_autoguardado(db_path: str = DB_DEFAULT_PATH) -> dict[str, Any] | None:
    """Regresa el dict de estado reconstruido desde el autoguardado, o None si no hay."""
    if not Path(db_path).exists():
        return None
    conn = _conectar(db_path)
    try:
        row = conn.execute("SELECT payload FROM autosave WHERE id = 1").fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        return cargar_sesion_dict(data)
    finally:
        conn.close()


def borrar_autoguardado(db_path: str = DB_DEFAULT_PATH) -> None:
    if not Path(db_path).exists():
        return
    conn = _conectar(db_path)
    try:
        conn.execute("DELETE FROM autosave WHERE id = 1")
        conn.commit()
    finally:
        conn.close()
