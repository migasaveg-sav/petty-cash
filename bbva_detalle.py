"""Lector general del Excel/CSV de BBVA «Detalle de movimientos» con columnas separadas (sirve para TODAS las cuentas BBVA).

Formato (normalmente viene del movimiento MÁS RECIENTE al más antiguo; también se acepta al revés, se detecta solo):

    (sin título) | Concepto / Referencia | cargo | Abono | Saldo
    26-10-2026   | TRANS TABU TAXI/...   | 850   |       | 245.85

 * La primera columna no trae encabezado: son fechas en texto (dd-mm-aaaa) o fechas de Excel.
 * Cada renglón trae cargo O abono (la otra celda va vacía).
 * Los encabezados pueden variar (Concepto / Descripción / Referencia, Cargo(s), Abono(s), Saldo) y puede haber columnas extra.
 * Se valida la cadena de saldos (saldo anterior ± movimiento = saldo del renglón); si algo no cuadra no se carga nada.

Devuelve las columnas que espera la app: Fecha, Descripción, Cargo, Abono, Saldo y Monto (abono − cargo), de más antiguo a más reciente.
Lógica pura, sin Streamlit.
"""
from __future__ import annotations

import datetime
import io
import re

import pandas as pd

TOLERANCIA_SALDO = 0.005


class FormatoNoReconocido(Exception):
    """El archivo no tiene la estructura de este formato (la app prueba entonces el lector general)."""


class ErrorEstadoBBVA(Exception):
    """El archivo tiene este formato pero los datos no son confiables; el mensaje es apto para el usuario."""


def _norm(x) -> str:
    return re.sub(r"\s+", " ", str(x or "")).strip().lower()


def _numero(v) -> float | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    t = str(v).replace("$", "").replace(",", "").replace(" ", "").strip()
    if not t:
        return None
    try:
        return round(float(t), 2)
    except ValueError:
        return None


def _fecha(v) -> datetime.date | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, (datetime.datetime, pd.Timestamp)):
        return v.date()
    if isinstance(v, datetime.date):
        return v
    m = re.match(r"^\s*(\d{1,2})[-/](\d{1,2})[-/](\d{4})\s*$", str(v))
    if not m:
        return None
    try:
        return datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def _leer_tabla(contenido: bytes, nombre: str) -> list[list]:
    if nombre.lower().endswith((".xlsx", ".xlsm", ".xls")):
        df = pd.read_excel(io.BytesIO(contenido), header=None, dtype=object)
    else:
        for enc in ("utf-8-sig", "latin-1"):
            try:
                df = pd.read_csv(io.StringIO(contenido.decode(enc)), header=None, dtype=object, sep=None, engine="python")
                break
            except Exception:
                df = None
        if df is None:
            raise FormatoNoReconocido("No se pudo leer el archivo.")
    return df.where(pd.notna(df), None).values.tolist()


def leer_bbva_prj(contenido: bytes, nombre: str) -> pd.DataFrame:
    try:
        filas = _leer_tabla(contenido, nombre)
    except FormatoNoReconocido:
        raise
    except Exception as e:
        raise FormatoNoReconocido(str(e))
    # Encabezado: renglón con (concepto/descripción/referencia) + cargo + abono + saldo.
    def _es(c, opciones):
        return any(c.startswith(o) for o in opciones)
    CON, CAR, ABO, SAL, FEC = ("concepto", "descripci", "referencia"), ("cargo", "retiro"), ("abono", "dep\u00f3sito", "deposito"), ("saldo",), ("fecha", "d\u00eda", "dia")
    h = None
    for i, r in enumerate(filas[:15]):
        e = [_norm(c) for c in r]
        if all(any(_es(c, o) for c in e) for o in (CON, CAR, ABO, SAL)):
            h = i
            break
    if h is None:
        raise FormatoNoReconocido("No encontré los encabezados Concepto, Cargo, Abono y Saldo.")
    enc = [_norm(c) for c in filas[h]]
    idx = lambda o: next(i for i, c in enumerate(enc) if _es(c, o))
    i_con, i_car, i_abo, i_sal = idx(CON), idx(CAR), idx(ABO), idx(SAL)
    i_fec = next((i for i, c in enumerate(enc) if _es(c, FEC)), None)
    if i_fec is None:   # la columna de fechas viene sin título: la que está antes del concepto
        i_fec = i_con - 1 if i_con > 0 else None
    if i_fec is None:
        raise FormatoNoReconocido("No encontré la columna de fechas.")

    movs: list[dict] = []
    for n, r in enumerate(filas[h + 1:], start=h + 2):
        if all(c is None or str(c).strip() == "" for c in r):
            continue
        f = _fecha(r[i_fec])
        if f is None:
            raise ErrorEstadoBBVA(f"Renglón {n} del archivo: la fecha «{r[i_fec]}» no se entiende (se espera dd-mm-aaaa).")
        cargo, abono, saldo = _numero(r[i_car]), _numero(r[i_abo]), _numero(r[i_sal])
        if (cargo is None) == (abono is None):
            raise ErrorEstadoBBVA(f"Renglón {n} del archivo ({f:%d-%m-%Y}): debe traer cargo o abono, no ambos ni ninguno.")
        if saldo is None:
            raise ErrorEstadoBBVA(f"Renglón {n} del archivo ({f:%d-%m-%Y}): falta el saldo.")
        movs.append({"Fecha": pd.Timestamp(f), "Descripción": str(r[i_con] or "").strip(), "Cargo": cargo, "Abono": abono, "Saldo": saldo,
                     "Monto": round((abono or 0.0) - (cargo or 0.0), 2), "_n": n})
    if not movs:
        raise ErrorEstadoBBVA("El archivo no trae movimientos.")

    def _malos(lista):
        out = []
        for x, y in zip(lista, lista[1:]):
            if abs(x["Saldo"] + y["Monto"] - y["Saldo"]) > TOLERANCIA_SALDO:
                out.append(f"renglón {y['_n']} ({y['Fecha']:%d-%m-%Y} {y['Descripción'][:35]}): saldo esperado "
                           f"{x['Saldo'] + y['Monto']:,.2f}, el archivo dice {y['Saldo']:,.2f}")
        return out

    # Normalmente el archivo va del más reciente al más antiguo; se prueba también al revés y se queda el orden que cuadra.
    candidatos = [list(reversed(movs)), list(movs)]
    validos = [c for c in candidatos if not _malos(c)]
    if not validos:
        malos = _malos(candidatos[0])
        raise ErrorEstadoBBVA("Los saldos no cuadran, así que no se cargó nada para evitar montos mal leídos. Revisa: " + "; ".join(malos[:5]))
    movs = validos[0]

    df = pd.DataFrame(movs).drop(columns="_n")
    df["Cargo"] = df["Cargo"].fillna(0.0)
    df["Abono"] = df["Abono"].fillna(0.0)
    df.attrs["validado"] = True
    return df[["Fecha", "Descripción", "Cargo", "Abono", "Saldo", "Monto"]].reset_index(drop=True)
