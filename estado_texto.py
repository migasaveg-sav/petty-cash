"""Reformateador de estados de cuenta «ilegibles» (plan B cuando el lector normal no entiende el archivo).

Casos que resuelve, sin importar el banco:
  * Excel/CSV donde cada movimiento viene COMPLETO en una sola celda (lo que sale al copiar y pegar de la banca en línea):
        «18-11-2026 RETIRO CAJERO/******6532 NOV18 13:14 ...  2,100.00 9,788.00»
  * Excel/CSV con columnas sin encabezados reconocibles (se unen las celdas de cada renglón y se interpreta el texto).
  * Texto pegado directamente en la app (con tabuladores o espacios, un movimiento por renglón o partido en varios).

Cada movimiento debe traer: fecha (dd-mm-aaaa, dd/mm/aaaa o aaaa-mm-dd), descripción, monto y —idealmente— el saldo.
Con el saldo se decide si es cargo o abono (saldo anterior ± monto = saldo) y se valida toda la cadena; si algo no cuadra
NO se carga nada y se dice qué renglones revisar. Sin saldo se usan el signo / palabras como «abono», «depósito», «recibido».

Devuelve las columnas que espera la app: Fecha (aaaa-mm-dd), Descripción, Cargo, Abono, Saldo y Monto (abono − cargo),
de más antiguo a más reciente. Lógica pura, sin Streamlit.
"""
from __future__ import annotations

import datetime
import io
import re

import pandas as pd

TOLERANCIA_SALDO = 0.005
_NUM = r"[-(]?\$?\s?\d{1,3}(?:,\d{3})*\.\d{2}\)?|[-(]?\$?\s?\d+\.\d{2}\)?"
_RE_FECHA = re.compile(r"^\s*(?:(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})|(\d{4})[-/.](\d{1,2})[-/.](\d{1,2}))(?=\s|$)")
_RE_FINAL = re.compile(rf"(?P<monto>{_NUM})(?:\s+(?P<saldo>{_NUM}))?\s*$")
_PALABRAS_ABONO = ("abono", "deposito", "depósito", "recibido", "devolucion", "devolución", "reembolso", "nomina", "nómina", "spei recibido")


class ErrorFormato(Exception):
    """No se pudo reformatear el archivo o los saldos no cuadran; el mensaje es apto para el usuario."""


def _limpiar(t) -> str:
    return re.sub(r"\s+", " ", str(t if t is not None else "").replace("\xa0", " ").replace("\t", " ")).strip()


def _numero(t: str) -> float:
    s = t.replace("$", "").replace(",", "").replace(" ", "")
    neg = s.startswith("-") or (s.startswith("(") and s.endswith(")"))
    return round(float(s.strip("()-")), 2) * (-1 if neg else 1)


def _fecha(m: re.Match) -> datetime.date | None:
    try:
        if m.group(1):
            return datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        return datetime.date(int(m.group(4)), int(m.group(5)), int(m.group(6)))
    except ValueError:
        return None


def _celda_a_texto(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if isinstance(v, (datetime.datetime, pd.Timestamp)):
        return v.strftime("%d-%m-%Y")
    if isinstance(v, datetime.date):
        return v.strftime("%d-%m-%Y")
    if isinstance(v, float):
        return f"{v:,.2f}"          # los montos numéricos vuelven a tener 2 decimales para reconocerlos
    if isinstance(v, int):
        return str(v)      # (en reformatear(), los enteros de las últimas celdas del renglón se tratan como dinero)
    return _limpiar(v)


def _tabla(contenido: bytes, nombre: str) -> list[list]:
    try:
        if nombre.lower().endswith((".xlsx", ".xlsm", ".xls")):
            df = pd.read_excel(io.BytesIO(contenido), header=None, dtype=object)
        else:
            df = None
            for enc in ("utf-8-sig", "latin-1"):
                try:
                    df = pd.read_csv(io.StringIO(contenido.decode(enc)), header=None, dtype=object, sep=None, engine="python")
                    break
                except Exception:
                    continue
            if df is None:
                raise ValueError("no se pudo leer como CSV")
    except Exception as e:
        raise ErrorFormato(f"No se pudo abrir el archivo: {e}") from e
    return df.where(pd.notna(df), None).values.tolist()


def parece_texto_en_una_celda(contenido: bytes, nombre: str) -> bool:
    """True si casi todos los renglones traen el movimiento completo en una sola celda (el lector normal no sirve)."""
    try:
        filas = _tabla(contenido, nombre)
    except ErrorFormato:
        return False
    con_datos = [[c for c in r if c is not None and _limpiar(c)] for r in filas]
    con_datos = [r for r in con_datos if r]
    if not con_datos:
        return False
    unicas = [r for r in con_datos if len(r) == 1 and _RE_FECHA.match(_limpiar(r[0]))]
    return len(unicas) >= max(1, int(0.8 * len(con_datos)))


# ----------------------------------------------------------------------
# Texto → movimientos
# ----------------------------------------------------------------------
def _lineas_a_movimientos(lineas: list[str]) -> list[dict]:
    """Une renglones que no empiezan con fecha al movimiento anterior y extrae fecha, descripción, monto y saldo."""
    bloques: list[tuple[int, str]] = []
    for n, l in enumerate(lineas, start=1):
        t = _limpiar(l)
        if not t:
            continue
        if _RE_FECHA.match(t) or not bloques:
            bloques.append((n, t))
        else:
            bloques[-1] = (bloques[-1][0], bloques[-1][1] + " " + t)
    movs: list[dict] = []
    for n, t in bloques:
        mf = _RE_FECHA.match(t)
        if not mf:
            continue          # encabezados, títulos y textos sueltos antes del primer movimiento
        fecha = _fecha(mf)
        if fecha is None:
            raise ErrorFormato(f"Renglón {n}: la fecha «{t[:12]}» no es válida.")
        resto = t[mf.end():].strip()
        mm = _RE_FINAL.search(resto)
        if not mm:
            raise ErrorFormato(f"Renglón {n} ({fecha:%d-%m-%Y}): no encontré el monto al final del renglón «{resto[:60]}».")
        monto = _numero(mm.group("monto"))
        saldo = _numero(mm.group("saldo")) if mm.group("saldo") else None
        desc = resto[:mm.start()].strip()
        movs.append({"_n": n, "fecha": fecha, "texto": desc, "monto": monto, "saldo": saldo})
    return movs


def _concepto(texto: str) -> str:
    """Concepto sin la referencia: «CONCESIONARIA AUT MTY/******6532 RFC: …» → «CONCESIONARIA AUT MTY/******6532».
    (Es el mismo texto que usa el lector de BBVA, para que un mismo movimiento no se duplique al cargarlo por otra vía.)"""
    m = re.match(r"(\S.*?/\S+)\s*(.*)$", texto)
    return m.group(1) if m else texto


def _con_palabra_de_abono(texto: str) -> bool:
    t = texto.lower()
    return any(p in t for p in _PALABRAS_ABONO)


def _clasificar(movs: list[dict]) -> tuple[list[dict], list[str]]:
    """Ordena cronológicamente (probando ambos sentidos del archivo) y decide cargo/abono con la cadena de saldos."""
    if not movs:
        raise ErrorFormato("No encontré movimientos (renglones que empiecen con una fecha y terminen con monto y saldo).")
    avisos: list[str] = []
    todos_con_saldo = all(m["saldo"] is not None for m in movs)

    if not todos_con_saldo:
        out = []
        for m in movs:
            neg = m["monto"] < 0
            abono = (not neg) and _con_palabra_de_abono(m["texto"])
            out.append({**m, "cargo": abs(m["monto"]) if not abono else None, "abono": abs(m["monto"]) if abono else None})
        out.sort(key=lambda x: (x["fecha"], x["_n"]))
        avisos.append("El archivo no trae saldo en todos los renglones: no se pudo validar la cadena de saldos. "
                      "Cargo/abono se decidió por el signo y palabras como «abono», «depósito» o «recibido»; revisa la vista previa.")
        return out, avisos

    def evaluar(cron: list[dict]) -> tuple[list[dict], list[str]]:
        res, malos = [], []
        for i, m in enumerate(cron):
            mon = abs(m["monto"])
            if i == 0:
                abono = _con_palabra_de_abono(m["texto"]) and m["monto"] >= 0
                res.append({**m, "cargo": None if abono else mon, "abono": mon if abono else None, "_sin_previo": True})
                continue
            dif = round(m["saldo"] - cron[i - 1]["saldo"], 2)
            if abs(dif - mon) <= TOLERANCIA_SALDO:
                res.append({**m, "cargo": None, "abono": mon})
            elif abs(dif + mon) <= TOLERANCIA_SALDO:
                res.append({**m, "cargo": mon, "abono": None})
            else:
                malos.append(f"renglón {m['_n']} ({m['fecha']:%d-%m-%Y} {m['texto'][:35]}): saldo anterior {cron[i - 1]['saldo']:,.2f}, "
                             f"monto {mon:,.2f}, saldo del renglón {m['saldo']:,.2f}")
                res.append({**m, "cargo": mon, "abono": None})
        return res, malos

    # El banco lista normalmente del más reciente al más antiguo; se prueban los dos sentidos y se queda el que cuadra.
    cand = [list(reversed(movs)), list(movs)]
    evals = [evaluar(c) for c in cand]
    orden = sorted(range(2), key=lambda i: (len(evals[i][1]), 0 if all(a["fecha"] <= b["fecha"] for a, b in zip(cand[i], cand[i][1:])) else 1, i))
    res, malos = evals[orden[0]]
    if malos:
        raise ErrorFormato("Los saldos no cuadran, así que no se cargó nada para evitar montos mal leídos. Revisa estos renglones:\n- "
                           + "\n- ".join(malos[:6]))
    if res and res[0].get("_sin_previo") and len(res) > 1:
        tipo = "abono" if res[0]["abono"] else "cargo"
        avisos.append(f"El movimiento más antiguo ({res[0]['fecha']:%d-%m-%Y}) no tiene saldo anterior contra el cual comprobarlo; "
                      f"se tomó como {tipo}. Los demás sí cuadran con la cadena de saldos.")
    return res, avisos


def _a_df(movs: list[dict], avisos: list[str], origen: str) -> pd.DataFrame:
    filas = [{"Fecha": m["fecha"].strftime("%Y-%m-%d"), "Descripción": _concepto(m["texto"]), "Cargo": m["cargo"] or 0.0,
              "Abono": m["abono"] or 0.0, "Saldo": m["saldo"], "Monto": round((m["abono"] or 0.0) - (m["cargo"] or 0.0), 2),
              "Referencia": m["texto"][len(_concepto(m["texto"])):].strip()} for m in movs]
    df = pd.DataFrame(filas)
    df.attrs["avisos"] = avisos
    df.attrs["validado"] = not any("no se pudo validar" in a for a in avisos)
    df.attrs["origen"] = origen
    return df


def reformatear_texto(texto: str) -> pd.DataFrame:
    """Movimientos desde texto pegado (un movimiento por renglón; columnas separadas por tabuladores o espacios)."""
    movs = _lineas_a_movimientos(str(texto).splitlines())
    res, avisos = _clasificar(movs)
    return _a_df(res, avisos, "texto pegado")


def reformatear(contenido: bytes, nombre: str) -> pd.DataFrame:
    """Movimientos desde un Excel/CSV cuyo formato no reconoce el lector normal."""
    filas = _tabla(contenido, nombre)
    lineas = []
    for r in filas:
        celdas = [c for c in r if c is not None and _celda_a_texto(c)]
        n = len(celdas)
        partes = []
        for i, c in enumerate(celdas):
            esnum = isinstance(c, (int, float)) and not isinstance(c, bool)
            # Los números de las últimas 3 celdas son cargo / abono / saldo (los enteros se vuelven 1,200.00); los anteriores son texto.
            partes.append(f"{float(c):,.2f}" if esnum and i >= n - 3 and i > 0 else _celda_a_texto(c))
        lineas.append(" ".join(partes))
    movs = _lineas_a_movimientos(lineas)
    res, avisos = _clasificar(movs)
    return _a_df(res, avisos, "archivo")


def a_excel(df: pd.DataFrame) -> bytes:
    """El estado de cuenta ya ordenado en columnas (Fecha, Concepto, Referencia, Cargo, Abono, Saldo), para guardarlo o reusarlo."""
    out = io.BytesIO()
    x = pd.DataFrame({"Fecha": pd.to_datetime(df["Fecha"]).dt.strftime("%d-%m-%Y"), "Concepto": df["Descripción"],
                      "Referencia": df.get("Referencia", ""), "Cargo": df["Cargo"].replace(0.0, None),
                      "Abono": df["Abono"].replace(0.0, None), "Saldo": df["Saldo"]})
    # Mismo orden que entrega el banco (más reciente primero), por si se vuelve a subir con el lector normal.
    x = x.iloc[::-1].reset_index(drop=True)
    with pd.ExcelWriter(out, engine="xlsxwriter") as w:
        x.to_excel(w, index=False, sheet_name="Sheet1")
        h = w.sheets["Sheet1"]
        for i, ancho in enumerate((12, 44, 60, 12, 12, 12)):
            h.set_column(i, i, ancho)
    return out.getvalue()
