"""Lectura de estados de cuenta en PDF con texto seleccionable.

Pensado para la pantalla «Detalle de movimientos» de BBVAnet impresa a PDF (columnas Fecha, Concepto / Referencia,
Cargo, Abono y Saldo), pero sirve para cualquier tabla con esos encabezados. Lógica pura, sin Streamlit.

Cómo funciona
  1. Ubica el encabezado (Fecha · Concepto · Cargo · Abono · Saldo) y de ahí saca las columnas por posición.
  2. Cada renglón de la tabla empieza con una fecha dd-mm (el PDF no trae el año: se toma de la fecha de impresión).
  3. Valida la cadena de saldos: saldo anterior ± movimiento = saldo del renglón. Si algo no cuadra NO carga nada
     y dice cuáles renglones revisar (así un monto mal leído nunca se cuela).

Un PDF «impreso» como imagen o con la impresora «Microsoft Print to PDF» no trae texto; en ese caso se avisa cómo
volver a generarlo (Chrome → Imprimir → Destino: «Guardar como PDF») o que se use el Excel/CSV del banco.
"""
from __future__ import annotations

import datetime
import re

import pandas as pd

TOLERANCIA_SALDO = 0.005

MSG_SIN_TEXTO = (
    "Este PDF no trae texto que se pueda leer (es una imagen o se imprimió con «Microsoft Print to PDF»). "
    "Vuelve a generarlo desde el navegador: en BBVAnet pulsa Ctrl+P y en «Destino» elige «Guardar como PDF» "
    "(Chrome o Edge, no «Microsoft Print to PDF»). También puedes descargar el movimiento en Excel o CSV desde el banco."
)


class ErrorPDF(Exception):
    """El PDF no se pudo leer o los saldos no cuadran; el mensaje es apto para el usuario."""


_RE_FECHA_FILA = re.compile(r"^(\d{1,2})[-/](\d{1,2})(?:[-/](\d{2,4}))?$")
_RE_FECHA_REPORTE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")


def _monto(texto: str) -> float | None:
    t = texto.replace("$", "").replace(" ", "")
    return float(t.replace(",", "")) if re.match(r"^(\d{1,3}(,\d{3})+|\d+)\.\d{2}$", t) else None


def _fecha_referencia(paginas_texto: list[str]) -> datetime.date:
    for t in paginas_texto:
        for d, m, a in _RE_FECHA_REPORTE.findall(t):
            try:
                return datetime.date(int(a), int(m), int(d))
            except ValueError:
                continue
    return datetime.date.today()


def _encabezado(palabras: list[tuple]) -> dict | None:
    """Busca el renglón de encabezado y devuelve las posiciones de las columnas."""
    por_texto: dict[str, list[tuple]] = {}
    for p in palabras:
        por_texto.setdefault(p[4].strip().lower(), []).append(p)
    for f in por_texto.get("fecha", []):
        y = (f[1] + f[3]) / 2

        def cerca(nombre):
            return [p for p in por_texto.get(nombre, []) if abs((p[1] + p[3]) / 2 - y) < 6]
        cargo, abono, saldo = cerca("cargo"), cerca("abono"), cerca("saldo")
        if not (cargo and abono and saldo):
            continue
        concepto = [p for p in palabras if abs((p[1] + p[3]) / 2 - y) < 6 and p[4].strip().lower().startswith(("concepto", "descripci", "referencia"))]
        centro = lambda p: (p[0] + p[2]) / 2
        cx = {"cargo": centro(cargo[0]), "abono": centro(abono[0]), "saldo": centro(saldo[0])}
        return {"y": f[3], "cx": cx, "fecha_x0": f[0], "fecha_x1": f[2] + 2,
                "desc_fin": cx["cargo"] - (cx["abono"] - cx["cargo"]) / 2}
    return None


def _renglones_pagina(palabras: list[tuple], enc: dict, anio_ref: int, mes_ref: int) -> list[dict]:
    fechas = []
    for p in palabras:
        # las fechas del renglón quedan alineadas bajo el encabezado «Fecha»
        if p[1] <= enc["y"] or abs(p[0] - enc["fecha_x0"]) > 20:
            continue
        m = _RE_FECHA_FILA.match(p[4].strip())
        if m:
            fechas.append((p, m))
    fechas.sort(key=lambda t: t[0][1])
    if not fechas:
        return []
    enc = dict(enc, fecha_x1=max(p[2] for p, _ in fechas) + 1.5)   # el concepto empieza después de la fecha
    ys = [(p[1] + p[3]) / 2 for p, _ in fechas]
    paso = (ys[-1] - ys[0]) / (len(ys) - 1) if len(ys) > 1 else 20.0
    limites = [enc["y"]] + [(a + b) / 2 for a, b in zip(ys, ys[1:])] + [ys[-1] + paso / 2]
    filas = []
    for i, (pf, m) in enumerate(fechas):
        y0, y1 = limites[i], limites[i + 1]
        banda = [p for p in palabras if y0 <= (p[1] + p[3]) / 2 < y1]
        desc = sorted((p for p in banda if enc["fecha_x1"] <= (p[0] + p[2]) / 2 < enc["desc_fin"]),
                      key=lambda p: (round((p[1] + p[3]) / 2 / 3), p[0]))
        valores: dict[str, float] = {}
        for p in banda:
            cx = (p[0] + p[2]) / 2
            texto = p[4].strip()
            if cx < enc["desc_fin"] or texto == "$":
                continue
            v = _monto(texto)
            if v is None:
                continue
            col = min(enc["cx"], key=lambda k: abs(enc["cx"][k] - cx))
            valores[col] = v
        dia, mes = int(m.group(1)), int(m.group(2))
        anio = int(m.group(3)) if m.group(3) else (anio_ref if mes <= mes_ref else anio_ref - 1)
        if anio < 100:
            anio += 2000
        try:
            fecha = datetime.date(anio, mes, dia)
        except ValueError:
            raise ErrorPDF(f"Fecha no válida en el PDF: «{pf[4]}».")
        filas.append({"Fecha": fecha, "Descripción": " ".join(p[4] for p in desc).strip(),
                      "Cargo": valores.get("cargo"), "Abono": valores.get("abono"), "Saldo": valores.get("saldo")})
    return filas


def leer_pdf_estado(file_bytes: bytes) -> pd.DataFrame:
    """Devuelve el estado de cuenta (de más antiguo a más reciente) con columnas Fecha, Descripción, Cargo, Abono, Saldo y Monto
    (abono positivo, cargo negativo). Lanza ErrorPDF si no se puede leer o si los saldos no cuadran."""
    try:
        import pymupdf
    except ImportError as e:   # pragma: no cover
        raise ErrorPDF("Falta la librería PyMuPDF (pymupdf) para leer PDF.") from e
    try:
        doc = pymupdf.open(stream=file_bytes, filetype="pdf")
    except Exception as e:
        raise ErrorPDF(f"No se pudo abrir el PDF: {e}") from e
    paginas = [p.get_text("words") for p in doc]
    if not any(paginas):
        raise ErrorPDF(MSG_SIN_TEXTO)
    ref = _fecha_referencia([p.get_text() for p in doc])
    filas: list[dict] = []
    enc_previo = None
    for palabras in paginas:
        enc = _encabezado(palabras) or (dict(enc_previo, y=-1) if enc_previo else None)
        if not enc:
            continue
        enc_previo = enc
        filas += _renglones_pagina(palabras, enc, ref.year, ref.month)
    if not filas:
        raise ErrorPDF("No encontré la tabla de movimientos (encabezados Fecha, Concepto, Cargo, Abono y Saldo). "
                       "¿Es el detalle de movimientos? Si es otro formato, mándame un ejemplo para agregarlo.")

    # BBVAnet lista del más reciente al más antiguo: se invierte para trabajar en orden cronológico.
    if filas[0]["Fecha"] > filas[-1]["Fecha"]:
        filas.reverse()

    problemas = []
    for i, f in enumerate(filas, start=1):
        if (f["Cargo"] is None) == (f["Abono"] is None):
            problemas.append(f"renglón {i} ({f['Fecha']:%d-%m} {f['Descripción'][:35]}): no se pudo leer un único cargo o abono")
    if problemas:
        raise ErrorPDF("No pude leer bien estos renglones:\n- " + "\n- ".join(problemas[:6]))
    for f in filas:
        f["Monto"] = round((f["Abono"] or 0.0) - (f["Cargo"] or 0.0), 2)

    con_saldo = [f for f in filas if f["Saldo"] is not None]
    avisos = []
    if len(con_saldo) == len(filas) and len(filas) > 1:
        malos = [(i, a, b) for i, (a, b) in enumerate(zip(filas, filas[1:]), start=2)
                 if abs(a["Saldo"] + b["Monto"] - b["Saldo"]) > TOLERANCIA_SALDO]
        if malos:
            det = [f"renglón {i} ({b['Fecha']:%d-%m} {b['Descripción'][:35]}): saldo esperado {a['Saldo'] + b['Monto']:,.2f}, el PDF dice {b['Saldo']:,.2f}"
                   for i, a, b in malos[:6]]
            raise ErrorPDF(f"Los saldos no cuadran en {len(malos)} renglón(es); no se cargó nada para no meter montos mal leídos:\n- " + "\n- ".join(det))
    else:
        avisos.append("El PDF no trae saldo en todos los renglones: no se pudo validar la cadena de saldos.")

    df = pd.DataFrame(filas)
    df["Fecha"] = df["Fecha"].map(lambda d: d.strftime("%Y-%m-%d"))
    df["Cargo"] = df["Cargo"].fillna(0.0)
    df["Abono"] = df["Abono"].fillna(0.0)
    df = df[["Fecha", "Descripción", "Cargo", "Abono", "Saldo", "Monto"]].reset_index(drop=True)
    df.attrs["avisos"] = avisos
    df.attrs["validado"] = not avisos
    df.attrs["fecha_impresion"] = ref.isoformat()
    return df
