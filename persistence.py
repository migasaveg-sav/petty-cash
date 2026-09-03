"""Persistencia de la sesión de trabajo:

1. Serialización a JSON para descargar/subir un "avance" manualmente (igual que la
   versión original, pero corregido para incluir pool_facturas, catálogos y
   modo_trabajo, que antes se perdían al recargar un avance guardado).
2. Autoguardado en SQLite como red de seguridad: cada cierto número de acciones (o
   al cerrar un gasto) se guarda una copia local en disco, para poder recuperar el
   trabajo si el navegador se cierra sin que el usuario haya descargado el .json.

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
        "selected_idx": None,
    }


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
