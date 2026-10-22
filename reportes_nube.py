"""Reportes de la nube: lote por enviar y Excel del mes (mismo formato que el de la app anterior).

Funciones puras (no tocan la base): reciben datos ya consultados y devuelven filas o bytes.
"""
from __future__ import annotations

import datetime
from io import BytesIO
from typing import Any

import pandas as pd
from xlsxwriter.utility import xl_col_to_name

C_HEADER = "#DC143C"          # rojo de la bitácora
C_AMARILLO = "#FFF2CC"        # fila con diferencia (dentro de tolerancia)
C_GRIS = "#E5E7EB"
FORMATO_DINERO = "$#,##0.00"

NOMBRE_RESULTADO = {"aprobado": "Aprobado para pago", "devuelto": "Devuelto", "en_revision": "En revisión"}
ELEGIBLES = ("borrador", "con_pendientes")


# ----------------------------------------------------------------------
# Lote por enviar
# ----------------------------------------------------------------------
def solicitudes_por_enviar(solicitudes: list[dict], gastos: list[dict], con_pendiente_abierto: set[str]) -> list[dict]:
    """Solicitudes que entrarían al próximo envío: borrador o con pendientes ya atendidos, con al menos
    un gasto comprobado y sin pendientes abiertos. Mismo criterio que la función enviar_estado_cuenta."""
    por_sol: dict[str, list[dict]] = {}
    for g in gastos:
        if not g.get("eliminado"):
            por_sol.setdefault(g["solicitud_id"], []).append(g)
    return [s for s in solicitudes
            if not s.get("eliminado") and s["estado"] in ELEGIBLES
            and por_sol.get(s["id"]) and s["id"] not in con_pendiente_abierto]


def filas_por_enviar(solicitudes: list[dict], gastos: list[dict], con_pendiente_abierto: set[str]) -> pd.DataFrame:
    """Un renglón por gasto del lote por enviar (para la tabla de revisión del aplicante)."""
    sols = {s["id"]: s for s in solicitudes_por_enviar(solicitudes, gastos, con_pendiente_abierto)}
    filas = []
    for g in gastos:
        s = sols.get(g["solicitud_id"])
        if not s or g.get("eliminado"):
            continue
        movs = g.get("movimientos") or []
        filas.append({
            "Solicitud": int(s["numero"]),
            "Fecha": min((m["fecha"] for m in movs), default=s["fecha"]),
            "Empleado": s.get("employee_name") or "",
            "Categoría": g.get("categoria") or s.get("category") or "",
            "Descripción": s.get("description") or "",
            "Movimientos": len(movs),
            "Facturas": len(g.get("facturas") or []),
            "Monto": round(float(g["monto_estado"]), 2),
            "Diferencia": round(float(g.get("diferencia") or 0), 2),
            "Devuelto": "Sí" if s["estado"] == "con_pendientes" else "",
        })
    df = pd.DataFrame(filas, columns=["Solicitud", "Fecha", "Empleado", "Categoría", "Descripción", "Movimientos",
                                      "Facturas", "Monto", "Diferencia", "Devuelto"])
    return df.sort_values(["Solicitud", "Fecha"]).reset_index(drop=True) if not df.empty else df


# ----------------------------------------------------------------------
# Bitácora del mes: un renglón por registro, con su etiqueta
# ----------------------------------------------------------------------
ETIQUETA_REGISTRO = {"comprobado": "Comprobado", "no_necesario": "No necesario",
                     "sin_comprobar": "Sin comprobar", "pendiente": "Pendiente"}
PRIORIDAD_REGISTRO = {"sin_comprobar": 0, "pendiente": 1, "comprobado": 2, "no_necesario": 3}
SITUACION_SOLICITUD = {"borrador": "Borrador", "enviado_revision": "En revisión", "con_pendientes": "Devuelto por el revisor",
                       "en_lote": "Aprobado (lote cerrado)", "pagado": "Pagado"}


def _conceptos(gastos: list[dict]) -> str:
    """Emisor / concepto de las facturas del gasto («FACEBOOK», «Aeroméxico»…), sin repetir."""
    vistos: list[str] = []
    for g in gastos:
        for f in g.get("facturas") or []:
            n = (f.get("nombre_emisor") or "").strip()
            if n and n not in vistos:
                vistos.append(n)
    return ", ".join(vistos[:3]) + (f" (+{len(vistos) - 3})" if len(vistos) > 3 else "")


def registros_bitacora(solicitudes: list[dict], gastos: list[dict], movs: pd.DataFrame) -> list[dict]:
    """Todo lo del mes en una sola lista:
      * cada solicitud: «comprobado» si ya tiene gasto, «sin comprobar» si todavía no;
      * cada movimiento del estado de cuenta que NO está comprobado: «no necesario», «sin comprobar» o «pendiente»
        (los comprobados ya salen dentro de su solicitud).
    Primero lo que falta por comprobar (rojo), luego lo comprobado (verde) y lo no necesario, por fecha."""
    por_sol: dict[str, list[dict]] = {}
    for g in gastos:
        if not g.get("eliminado"):
            por_sol.setdefault(g["solicitud_id"], []).append(g)
    filas: list[dict] = []
    for s in solicitudes:
        if s.get("eliminado"):
            continue
        gs = por_sol.get(s["id"], [])
        if gs:
            fechas = [m["fecha"] for g in gs for m in (g.get("movimientos") or [])]
            filas.append({
                "tipo": "solicitud", "id": s["id"], "estado": "comprobado", "fecha": min(fechas) if fechas else s["fecha"],
                "numero": int(s["numero"]), "descripcion": s.get("description") or "", "empleado": s.get("employee_name") or "",
                "concepto": _conceptos(gs), "movs": sum(len(g.get("movimientos") or []) for g in gs),
                "categoria": gs[0].get("categoria") or s.get("category") or "",
                "monto": round(sum(float(g["monto_estado"]) for g in gs), 2),
                "situacion": SITUACION_SOLICITUD.get(s["estado"], s["estado"])})
        else:
            filas.append({
                "tipo": "solicitud", "id": s["id"], "estado": "sin_comprobar", "fecha": s["fecha"], "numero": int(s["numero"]),
                "descripcion": s.get("description") or "", "empleado": s.get("employee_name") or "", "categoria": s.get("category") or "",
                "concepto": "", "movs": 0,
                "monto": round(float(s.get("monto_tentativo") or 0), 2), "situacion": "Solicitud sin movimiento ligado"})
    if len(movs):
        for r in movs.itertuples():
            if r.estado == "comprobado":
                continue
            estado = {"no_necesario": "no_necesario", "en_espera": "pendiente"}.get(r.estado, "sin_comprobar")
            motivo = getattr(r, "motivo_espera", None)
            filas.append({
                "tipo": "movimiento", "id": r.id, "estado": estado, "fecha": r.fecha, "numero": None,
                "descripcion": r.descripcion, "empleado": "", "categoria": "", "concepto": "", "movs": 1, "monto": round(abs(float(r.monto)), 2),
                "situacion": (str(motivo) if estado == "pendiente" and isinstance(motivo, str) and motivo
                              else "No requiere comprobación" if estado == "no_necesario" else "Movimiento del banco")})
    filas.sort(key=lambda f: (PRIORIDAD_REGISTRO[f["estado"]], str(f["fecha"]), f["numero"] or 0))
    return filas


# ----------------------------------------------------------------------
# Desglose del estado de cuenta, condensado por solicitud
# ----------------------------------------------------------------------
def filas_desglose(df: pd.DataFrame, gastos: list[dict]) -> list[dict]:
    """Una fila por solicitud comprobada (con el total de todos sus cargos) y una fila por cada movimiento que
    no está comprobado (no necesario, sin comprobar, pendiente). `df` es el estado de cuenta del mes con su
    solicitud/gasto/lote (v_movimientos_revision)."""
    gastos_por_id = {g["id"]: g for g in gastos}
    filas: list[dict] = []
    if df is None or df.empty:
        return filas
    comp = df[df["estado"] == "comprobado"]
    con_sol = comp[comp["solicitud_id"].notna()]
    for sid, sub in con_sol.groupby("solicitud_id", sort=False):
        movs = sub.drop_duplicates("id")
        gasto_ids = list(dict.fromkeys(sub["gasto_id"].dropna()))
        por_gasto = sub.drop_duplicates("gasto_id")
        r0 = sub.iloc[0]
        lote = ""
        if pd.notna(r0["lote_numero"]):
            lote = f"Lote de pago {int(r0['lote_numero'])}" + (f" · pagado el {r0['fecha_pago']}" if r0["lote_estado"] == "pagado" else " · por pagar")
        filas.append({
            "tipo": "solicitud", "id": sid, "estado": "comprobado", "fecha": min(str(x) for x in movs["fecha"]),
            "numero": int(r0["solicitud_numero"]),
            "descripcion": (r0.get("solicitud_descripcion") or movs.iloc[0]["descripcion"] or ""),
            "concepto": _conceptos([gastos_por_id[g] for g in gasto_ids if g in gastos_por_id]),
            "movs": int(len(movs)), "monto": round(float(movs["monto"].abs().sum()), 2),
            "diferencia": round(float(pd.to_numeric(por_gasto["diferencia"], errors="coerce").fillna(0).sum()), 2),
            "tiene_diferencia": bool(por_gasto["tiene_diferencia"].fillna(False).astype(bool).any()),
            "situacion": " · ".join(x for x in (SITUACION_SOLICITUD.get(r0["solicitud_estado"], str(r0["solicitud_estado"])), lote) if x)})
    resto = df[(df["estado"] != "comprobado") | (df["solicitud_id"].isna())].drop_duplicates("id")
    for r in resto.itertuples():
        estado = {"comprobado": "comprobado", "no_necesario": "no_necesario", "en_espera": "pendiente"}.get(r.estado, "sin_comprobar")
        motivo = getattr(r, "motivo_espera", None)
        filas.append({
            "tipo": "movimiento", "id": r.id, "estado": estado, "fecha": str(r.fecha), "numero": None, "descripcion": r.descripcion,
            "concepto": "", "movs": 1, "monto": round(abs(float(r.monto)), 2), "diferencia": 0.0, "tiene_diferencia": False,
            "situacion": (str(motivo) if estado == "pendiente" and isinstance(motivo, str) and motivo
                          else "No requiere comprobación" if estado == "no_necesario" else "Movimiento del banco")})
    filas.sort(key=lambda f: (f["fecha"], f["numero"] or 0, str(f["id"])))
    return filas


# ----------------------------------------------------------------------
# Cuadre del estado de cuenta
# ----------------------------------------------------------------------
ESTADOS_CUADRE = (("comprobado", "Comprobados"), ("no_necesario", "No necesarios"),
                  ("pendiente", "Sin comprobar"), ("en_espera", "Pendientes"))


def cuadre(movs: pd.DataFrame) -> dict[str, Any]:
    """comprobados + no necesarios + sin comprobar + pendientes debe ser igual al total del estado de cuenta."""
    n_tot = int(len(movs))
    m_tot = round(float(movs["monto"].abs().sum()), 2) if n_tot else 0.0
    por: dict[str, dict[str, float]] = {}
    for est, _ in ESTADOS_CUADRE:
        sub = movs[movs["estado"] == est] if n_tot else movs
        por[est] = {"n": int(len(sub)), "monto": round(float(sub["monto"].abs().sum()), 2) if len(sub) else 0.0}
    suma_n = sum(v["n"] for v in por.values())
    suma_m = round(sum(v["monto"] for v in por.values()), 2)
    return {"por_estado": por, "n_total": n_tot, "monto_total": m_tot, "n_suma": suma_n, "monto_suma": suma_m,
            "cuadra": suma_n == n_tot and abs(suma_m - m_tot) < 0.005,
            "dif_n": n_tot - suma_n, "dif_monto": round(m_tot - suma_m, 2)}


# ----------------------------------------------------------------------
# Excel del mes
# ----------------------------------------------------------------------
# La hoja tiene dos columnas «No.» (solicitud y banco); internamente la segunda se llama «No. (gasto)».
COLUMNAS_COMPROBADOS = [
    "No.", "Applicant", "Category", "Description", "Linked Request No.", "Number of Days", "Total Number of People",
    "Employee Name", "Material", "Payment Date", "Expense Outflow Amt", "Propina", "No. (gasto)", "Invoice date", "CFDI Folio",
    "IVA", "ISR Retenido", "IVA Retenido", "ISH", "Reimbursement Cap (With IVA)", "Status",
    "Lote enviado a revisión", "Resultado de la revisión", "Monto revisado y aprobado para pago", "Fecha de pago", "Lote de pago",
]
_DINERO = {"Expense Outflow Amt", "Propina", "IVA", "ISR Retenido", "IVA Retenido", "ISH",
           "Reimbursement Cap (With IVA)", "Monto revisado y aprobado para pago"}


def _encabezado(c: str) -> str:
    return "No." if c == "No. (gasto)" else c


def _fecha_corta(valor: Any) -> str:
    if not valor:
        return ""
    try:
        d = pd.to_datetime(valor)
        if d.tzinfo is not None:
            d = d.tz_convert("America/Mexico_City")
        return d.strftime("%d/%m/%Y")
    except Exception:
        return str(valor)[:10]


def _ultimo_envio_por_gasto(envio_gastos: list[dict]) -> dict[str, dict]:
    ultimo: dict[str, dict] = {}
    for r in sorted(envio_gastos, key=lambda x: (x["envio_numero"], str(x["enviado_en"]))):
        ultimo[r["gasto_id"]] = r
    return ultimo


def filas_comprobados(solicitudes: list[dict], gastos: list[dict], envio_gastos: list[dict],
                      applicant: str) -> tuple[list[dict], list[bool]]:
    """Filas de la hoja «Comprobados»: una por factura. Los datos de la solicitud van en la primera fila de la
    solicitud y los del gasto/pago en la primera fila de cada gasto. Ordenadas por lote enviado y solicitud.
    Regresa (filas, resaltar) donde resaltar marca los gastos con diferencia."""
    sols = {s["id"]: s for s in solicitudes if not s.get("eliminado")}
    ultimo = _ultimo_envio_por_gasto(envio_gastos)

    def clave(g: dict) -> tuple:
        e = ultimo.get(g["id"])
        s = sols[g["solicitud_id"]]
        return (e["envio_numero"] if e else 10_000, int(s["numero"]), min((m["fecha"] for m in g.get("movimientos") or []), default=""))

    gs = [g for g in gastos if not g.get("eliminado") and g["solicitud_id"] in sols]
    gs.sort(key=clave)
    filas: list[dict] = []
    resaltar: list[bool] = []
    vistas: set[str] = set()
    for g in gs:
        s = sols[g["solicitud_id"]]
        e = ultimo.get(g["id"])
        if e:
            lote_env = f"Lote #{e['envio_numero']} – {_fecha_corta(e['enviado_en'])}"
            resultado = NOMBRE_RESULTADO.get(e["resultado"], e["resultado"])
        else:
            lote_env, resultado = "Por enviar", ""
        aprobado = e.get("monto_aprobado") if e else None
        pagado = bool(e and e.get("lote_estado") == "pagado")
        lote_pago = ""
        if e and e.get("lote_numero") is not None:
            lote_pago = f"Lote {int(e['lote_numero'])} · " + ("pagado" if pagado else "por pagar") + (f" · ref. {e['referencia_pago']}" if pagado and e.get("referencia_pago") else "")
        movs = sorted(g.get("movimientos") or [], key=lambda m: m["fecha"])
        fecha_pago_mov = movs[0]["fecha"] if movs else s["fecha"]
        facturas = g.get("facturas") or [None]
        primera_sol = s["id"] not in vistas
        vistas.add(s["id"])
        for j, f in enumerate(facturas):
            primera_gasto = j == 0
            fila = {c: None for c in COLUMNAS_COMPROBADOS}
            if primera_sol and primera_gasto:
                fila.update({
                    "No.": int(s["numero"]), "Applicant": applicant, "Category": s.get("category") or g.get("categoria") or "",
                    "Description": s.get("description") or "", "Linked Request No.": s.get("request_number") or "",
                    "Number of Days": s.get("number_of_days") or 0, "Total Number of People": s.get("number_of_people") or 0,
                    "Employee Name": s.get("employee_name") or "", "Material": g.get("material") or s.get("material") or "",
                    "Status": "Comprobado"})
            if primera_gasto:
                fila.update({
                    "Payment Date": fecha_pago_mov, "Expense Outflow Amt": round(float(g["monto_estado"]), 2),
                    "Propina": round(float(g.get("propina") or 0), 2), "No. (gasto)": int(s["numero"]),
                    "Lote enviado a revisión": lote_env, "Resultado de la revisión": resultado,
                    "Monto revisado y aprobado para pago": round(float(aprobado), 2) if aprobado is not None else None,
                    "Fecha de pago": _fecha_corta(e.get("fecha_pago")) if pagado else "", "Lote de pago": lote_pago})
            if f is not None:
                fila.update({
                    "Invoice date": f.get("fecha_factura") or "", "CFDI Folio": f.get("uuid_cfdi") or "sin factura",
                    "IVA": round(float(f.get("iva_aplicado", f.get("iva") or 0)), 2),
                    "ISR Retenido": round(float(f.get("isr_aplicado", f.get("isr_retenido") or 0)), 2),
                    "IVA Retenido": round(float(f.get("iva_ret_aplicado", f.get("iva_retenido") or 0)), 2),
                    "ISH": round(float(f.get("ish_aplicado", f.get("ish") or 0)), 2),
                    "Reimbursement Cap (With IVA)": round(float(f.get("monto_aplicado") or 0), 2)})
            filas.append(fila)
            resaltar.append(bool(g.get("tiene_diferencia")))
    return filas, resaltar


def _tabla_movs(movs: pd.DataFrame, estado: str, con_motivo: bool = False) -> pd.DataFrame:
    sub = movs[movs["estado"] == estado].sort_values(["fecha", "descripcion"]) if len(movs) else movs
    if estado == "no_necesario":
        return pd.DataFrame({"Fecha Estado": sub["fecha"], "Descripción Estado": sub["descripcion"], "Monto Estado": sub["monto"].abs()})
    cols = {"Fecha": sub["fecha"], "Descripción": sub["descripcion"], "Monto": sub["monto"], "Saldo": sub["saldo"]}
    if con_motivo:
        cols["Motivo (por qué está pendiente)"] = sub["motivo_espera"]
        hoy = datetime.date.today()
        cols["Días pendiente"] = [
            (hoy - pd.to_datetime(d).date()).days if pd.notna(d) and d else None
            for d in sub["espera_desde"]]
    return pd.DataFrame(cols)


def excel_mes(cuenta_nombre: str, periodo: str, applicant: str, movs: pd.DataFrame, solicitudes: list[dict],
              gastos: list[dict], envio_gastos: list[dict], envios_resumen: list[dict]) -> bytes:
    """Excel del mes: Comprobados (con la columna del lote enviado a revisión), No necesarios, Sin comprobar,
    Pendientes, Solicitudes abiertas y Resumen (cuadre + lotes enviados vs. aprobados)."""
    filas, resaltar = filas_comprobados(solicitudes, gastos, envio_gastos, applicant)
    df = pd.DataFrame(filas, columns=COLUMNAS_COMPROBADOS)
    out = BytesIO()
    with pd.ExcelWriter(out, engine="xlsxwriter") as w:
        wb = w.book
        head = wb.add_format({"bold": True, "bg_color": C_HEADER, "font_color": "white", "border": 1,
                              "align": "center", "valign": "vcenter", "text_wrap": True})
        head_gris = wb.add_format({"bold": True, "bg_color": "#374151", "font_color": "white", "border": 1,
                                   "align": "center", "valign": "vcenter", "text_wrap": True})
        dinero = wb.add_format({"num_format": FORMATO_DINERO})
        dinero_am = wb.add_format({"num_format": FORMATO_DINERO, "bg_color": C_AMARILLO})
        texto_am = wb.add_format({"bg_color": C_AMARILLO})
        negrita = wb.add_format({"bold": True})
        titulo = wb.add_format({"bold": True, "font_size": 14})
        total_f = wb.add_format({"bold": True, "num_format": FORMATO_DINERO, "top": 1})
        total_t = wb.add_format({"bold": True, "top": 1})
        ok_f = wb.add_format({"bold": True, "font_color": "#15803D"})
        mal_f = wb.add_format({"bold": True, "font_color": "#DC2626"})

        # ---- Comprobados ----
        df.to_excel(w, index=False, sheet_name="Comprobados", header=False, startrow=1)
        ws = w.sheets["Comprobados"]
        for i, c in enumerate(COLUMNAS_COMPROBADOS):
            ws.write(0, i, _encabezado(c), head_gris if i >= COLUMNAS_COMPROBADOS.index("Lote enviado a revisión") else head)
        anchos = {"No.": 6, "No. (gasto)": 6, "Applicant": 12, "Category": 20, "Description": 26, "Linked Request No.": 18, "Number of Days": 10,
                  "Total Number of People": 12, "Employee Name": 28, "Material": 12, "Payment Date": 13, "Invoice date": 13,
                  "CFDI Folio": 38, "Status": 13, "Lote enviado a revisión": 24, "Resultado de la revisión": 20,
                  "Fecha de pago": 13, "Lote de pago": 30}
        for i, c in enumerate(COLUMNAS_COMPROBADOS):
            ws.set_column(i, i, anchos.get(c, 16), dinero if c in _DINERO else None)
        ws.set_row(0, 42)
        ws.freeze_panes(1, 0)
        ws.autofilter(0, 0, max(len(df), 1), len(COLUMNAS_COMPROBADOS) - 1)
        for r, marcar in enumerate(resaltar, start=1):     # diferencia dentro de tolerancia: fila en amarillo tenue
            if not marcar:
                continue
            for i, c in enumerate(COLUMNAS_COMPROBADOS):
                v = df.iloc[r - 1, i]
                v = v.item() if hasattr(v, "item") else v
                if pd.isna(v):
                    ws.write_blank(r, i, None, texto_am)
                elif c in _DINERO:
                    ws.write_number(r, i, float(v), dinero_am)
                else:
                    ws.write(r, i, v, texto_am)
        # Totales al pie
        fin = len(df) + 2
        ws.write(fin, COLUMNAS_COMPROBADOS.index("Expense Outflow Amt") - 1, "Total", total_t)
        for c in ("Expense Outflow Amt", "Propina", "Reimbursement Cap (With IVA)", "Monto revisado y aprobado para pago"):
            i = COLUMNAS_COMPROBADOS.index(c)
            col = xl_col_to_name(i)
            ws.write_formula(fin, i, f"=SUBTOTAL(109,{col}2:{col}{len(df) + 1})", total_f)

        # ---- No necesarios / Sin comprobar / Pendientes ----
        nn = _tabla_movs(movs, "no_necesario")
        nn.to_excel(w, index=False, sheet_name="No necesarios", header=False, startrow=1)
        sc = _tabla_movs(movs, "pendiente")
        sc.to_excel(w, index=False, sheet_name="Sin comprobar", header=False, startrow=1)
        pe = _tabla_movs(movs, "en_espera", con_motivo=True)
        pe.to_excel(w, index=False, sheet_name="Pendientes", header=False, startrow=1)
        for nombre, tabla, anchos_h in (("No necesarios", nn, [14, 46, 16]), ("Sin comprobar", sc, [14, 46, 16, 16]),
                                         ("Pendientes", pe, [14, 46, 16, 16, 48, 14])):
            h = w.sheets[nombre]
            for i, c in enumerate(tabla.columns):
                h.write(0, i, c, head)
                h.set_column(i, i, anchos_h[i], dinero if c in ("Monto", "Monto Estado", "Saldo") else None)
            h.freeze_panes(1, 0)
            h.autofilter(0, 0, max(len(tabla), 1), len(tabla.columns) - 1)

        # ---- Solicitudes abiertas (bitácora sin gasto) ----
        con_gasto = {g["solicitud_id"] for g in gastos if not g.get("eliminado")}
        abiertas = [{"No.": int(s["numero"]), "Fecha": s["fecha"], "Category": s.get("category") or "", "Description": s.get("description") or "",
                     "Employee Name": s.get("employee_name") or "", "Linked Request No.": s.get("request_number") or "",
                     "Monto tentativo": float(s.get("monto_tentativo") or 0)}
                    for s in sorted(solicitudes, key=lambda x: int(x["numero"])) if not s.get("eliminado") and s["id"] not in con_gasto]
        sa = pd.DataFrame(abiertas, columns=["No.", "Fecha", "Category", "Description", "Employee Name", "Linked Request No.", "Monto tentativo"])
        sa.to_excel(w, index=False, sheet_name="Solicitudes sin comprobar", header=False, startrow=1)
        h = w.sheets["Solicitudes sin comprobar"]
        for i, c in enumerate(sa.columns):
            h.write(0, i, c, head)
            h.set_column(i, i, [6, 13, 22, 34, 28, 20, 16][i], dinero if c == "Monto tentativo" else None)

        # ---- Resumen ----
        rs = wb.add_worksheet("Resumen")
        rs.set_column(0, 0, 30); rs.set_column(1, 8, 17)
        rs.write(0, 0, f"Comprobación de caja chica · {cuenta_nombre} · {periodo}", titulo)
        rs.write(1, 0, f"Generado el {datetime.date.today().strftime('%d/%m/%Y')}")
        c = cuadre(movs)
        rs.write(3, 0, "Cuadre del estado de cuenta", negrita)
        for i, t in enumerate(("Clasificación", "Movimientos", "Monto")):
            rs.write(4, i, t, head)
        fila = 5
        for est, nombre in ESTADOS_CUADRE:
            rs.write(fila, 0, nombre); rs.write_number(fila, 1, c["por_estado"][est]["n"]); rs.write_number(fila, 2, c["por_estado"][est]["monto"], dinero)
            fila += 1
        rs.write(fila, 0, "Suma de las 4 clasificaciones", total_t); rs.write_number(fila, 1, c["n_suma"], total_t); rs.write_number(fila, 2, c["monto_suma"], total_f)
        rs.write(fila + 1, 0, "Total del estado de cuenta", negrita); rs.write_number(fila + 1, 1, c["n_total"]); rs.write_number(fila + 1, 2, c["monto_total"], dinero)
        rs.write(fila + 2, 0, "Resultado", negrita)
        rs.write(fila + 2, 1, "✔ Cuadra" if c["cuadra"] else f"✘ No cuadra (dif. {c['dif_n']} mov. / {c['dif_monto']:,.2f})", ok_f if c["cuadra"] else mal_f)

        fila += 5
        rs.write(fila, 0, "Lotes enviados a revisión", negrita)
        enc = ["Lote enviado", "Fecha de envío", "Gastos", "Monto enviado", "Monto revisado y aprobado para pago",
               "Devuelto", "En revisión", "Pagado", "Fecha de pago"]
        for i, t in enumerate(enc):
            rs.write(fila + 1, i, t, head)
        rs.set_row(fila + 1, 44)
        r = fila + 2
        for e in envios_resumen:
            rs.write(r, 0, f"Lote enviado #{e['numero']}"); rs.write(r, 1, _fecha_corta(e["enviado_en"]))
            rs.write_number(r, 2, int(e["gastos_enviados"]))
            for i, k in enumerate(("monto_enviado", "monto_aprobado", "monto_devuelto", "monto_en_revision", "monto_pagado"), start=3):
                rs.write_number(r, i, float(e[k] or 0), dinero)
            if e.get("ultimo_pago"):
                p1, p2 = _fecha_corta(e.get("primer_pago")), _fecha_corta(e["ultimo_pago"])
                rs.write(r, 8, p1 if p1 == p2 else f"{p1} a {p2}")
            r += 1
        if envios_resumen:
            rs.write(r, 0, "Total", total_t); rs.write(r, 1, "", total_t)
            rs.write_number(r, 2, sum(int(e["gastos_enviados"]) for e in envios_resumen), total_t)
            for i, k in enumerate(("monto_enviado", "monto_aprobado", "monto_devuelto", "monto_en_revision", "monto_pagado"), start=3):
                rs.write_number(r, i, sum(float(e[k] or 0) for e in envios_resumen), total_f)
            rs.write(r + 2, 0, "Un gasto devuelto y reenviado aparece en el lote donde se envió por primera vez como «devuelto» y de nuevo en el lote en que se reenvía.")
        else:
            rs.write(r, 0, "Aún no hay lotes enviados a revisión este mes.")
    return out.getvalue()
