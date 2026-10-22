"""Importador del avance (.json) de la app local hacia la nube.

Qué migra:
  * el estado de cuenta (solo agrega movimientos nuevos; cada uno al mes de su fecha),
  * los gastos ya comprobados: una solicitud (en borrador) por gasto, con sus facturas y PDF
    comprimido; los gastos que compartían facturas entre varios cargos se juntan en un solo gasto,
  * los movimientos marcados como «no necesarios»,
  * las solicitudes de la bitácora que siguen «pendientes» (aún sin gasto vinculado): quedan como
    solicitudes en borrador para comprobarlas después. El archivo puede no traer estado de cuenta.
Qué NO migra: facturas «en proceso» sin guardar, ni los catálogos de la app local.

La lógica pura (leer_avance / planear) no toca la base; ejecutar() es la que escribe.
"""
from __future__ import annotations

import base64
import json
import re
from collections import OrderedDict
from typing import Any, Callable

import pandas as pd

import adjuntos as adj
import nube_db as db

CAMPOS_DINERO = ("Monto Total", "IVA", "ISR Retenido", "IVA Retenido", "ISH")


_RE_UUID = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")


def _normalizar_uuid(f: dict) -> dict:
    """Las facturas capturadas a mano pueden traer texto libre en UUID («sin factura», «cargos de
    facebook»). En la nube solo un UUID real identifica un CFDI; lo demás se guarda como manual (sin UUID)."""
    u = (f.get("UUID") or "").strip()
    if u and not _RE_UUID.match(u):
        return {**f, "UUID": "SIN-UUID"}
    return f


class ErrorImportacion(Exception):
    pass


def leer_avance(data: bytes) -> dict[str, Any]:
    try:
        av = json.loads(data.decode("utf-8-sig"))
    except Exception as e:
        raise ErrorImportacion(f"El archivo no es un JSON válido: {e}") from e
    if not isinstance(av, dict) or not ("bank_df" in av or "solicitudes" in av):
        raise ErrorImportacion("El archivo no parece un avance de la app de caja chica.")
    bank = av.get("bank_df")
    if bank:
        if "columns" not in bank or "data" not in bank:
            raise ErrorImportacion("El estado de cuenta del avance está incompleto.")
        faltan = {"Fecha", "Descripción", "Monto"} - set(bank["columns"])
        if faltan:
            raise ErrorImportacion(f"El estado de cuenta del avance no trae las columnas: {', '.join(sorted(faltan))}.")
    elif not [x for x in (av.get("solicitudes") or []) if isinstance(x, dict)]:
        raise ErrorImportacion("El archivo no trae estado de cuenta ni solicitudes: no hay nada que importar.")
    return av


def _df_estado(av: dict[str, Any]) -> pd.DataFrame:
    b = av.get("bank_df")
    if not b:
        return pd.DataFrame(columns=["Fecha", "Descripción", "Monto"])
    return pd.DataFrame(data=b["data"], columns=b["columns"], index=b["index"])


def _llaves(df: pd.DataFrame) -> dict[int, tuple]:
    """{idx_original: (fecha, descripcion, monto, ocurrencia)} con la misma lógica con que
    nube_db.movimientos_desde_df numera los movimientos idénticos."""
    llaves: dict[int, tuple] = {}
    vistos: dict[tuple, int] = {}
    for idx, r in df.iterrows():
        fecha = pd.to_datetime(r.get("Fecha"), errors="coerce")
        monto = r.get("Monto")
        if pd.isna(fecha) or pd.isna(monto):
            continue
        desc = "" if pd.isna(r.get("Descripción")) else str(r.get("Descripción")).strip()
        base = (fecha.date().isoformat(), desc or "(sin descripción)", round(float(monto), 2))
        vistos[base] = vistos.get(base, 0) + 1
        llaves[int(idx)] = (*base, vistos[base])
    return llaves


def _numero(x: Any) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def _factura_unificada(entradas: list[dict]) -> dict:
    """Une las porciones de una misma factura (prorrateo entre varios gastos) en la factura completa."""
    f = {k: v for k, v in entradas[0].items() if k not in ("Proporcion", "GrupoCompartido", "GastosDelGrupo")}
    for campo in CAMPOS_DINERO:
        f[campo] = round(sum(_numero(e.get(campo)) for e in entradas), 2)
    if not f.get("Adjunto"):
        f["Adjunto"] = next((e["Adjunto"] for e in entradas if e.get("Adjunto")), None)
    return f


def planear(av: dict[str, Any]) -> dict[str, Any]:
    df = _df_estado(av)
    llaves = _llaves(df)
    avisos: list[str] = []

    grupos: "OrderedDict[str, list[dict]]" = OrderedDict()
    for g in av.get("concatenados") or []:
        clave = f"grupo:{g['GrupoCompartido']}" if g.get("GrupoCompartido") else f"idx:{g['idx']}"
        grupos.setdefault(clave, []).append(g)

    gastos: list[dict] = []
    for clave, items in grupos.items():
        idxs = sorted({int(i["idx"]) for i in items})
        if any(i not in llaves for i in idxs):
            avisos.append(f"Gasto de los movimientos {idxs}: no se encontró el movimiento en el estado de cuenta; se omite.")
            continue
        por_uuid: "OrderedDict[str, list[dict]]" = OrderedDict()
        for n, it in enumerate(items):
            for f in it.get("Facturas") or []:
                f = _normalizar_uuid(f)
                u = (f.get("UUID") or "").strip()
                k = u if u and u != "SIN-UUID" else f"sin-uuid:{clave}:{n}:{f.get('_id')}"
                por_uuid.setdefault(k, []).append(f)
        facturas = [_factura_unificada(v) for v in por_uuid.values()]
        if not facturas:
            avisos.append(f"Gasto de los movimientos {idxs}: no tiene facturas; se omite.")
            continue
        p = items[0]
        fechas = sorted(str(i["Fecha Estado"])[:10] for i in items)
        gastos.append({
            "idxs": idxs,
            "llaves": [llaves[i] for i in idxs],
            "fecha": fechas[0],
            "monto": round(sum(_numero(i.get("Monto Estado")) for i in items), 2),
            "category": p.get("Categoria") or None,
            "material": p.get("Material") or None,
            "propina": round(sum(_numero(i.get("Propina")) for i in items), 2),
            "employee_name": p.get("Employee Name") or None,
            "description": (p.get("Description") or "").strip() or str(p.get("Descripción Estado") or "").strip() or None,
            "request_number": (p.get("Request Number") or "").strip() or None,
            "number_of_days": int(_numero(p.get("Number of Days"))),
            "number_of_people": int(_numero(p.get("Number of People"))),
            "facturas": facturas,
            "compartido": len(items) > 1,
        })

    no_nec = {int(k) for k, v in (av.get("estados") or {}).items() if v == "no_necesario"}
    for x in av.get("no_necesarios") or []:
        no_nec.add(int(x["idx"] if isinstance(x, dict) else x))
    no_nec = {i for i in no_nec if i in llaves}

    en_proceso = sorted({int(k) for k in (av.get("facturas_por_gasto") or {}) if (av["facturas_por_gasto"][k])})
    if en_proceso:
        avisos.append(f"Hay facturas «en proceso» (sin guardar) en los movimientos {en_proceso}; no se importan, quedan pendientes.")

    solicitudes, vinculadas, sin_fecha = [], 0, 0
    for x in av.get("solicitudes") or []:
        if not isinstance(x, dict):
            continue
        if x.get("idx_vinculado") not in (None, []) or x.get("estado") not in (None, "pendiente"):
            vinculadas += 1          # ya tienen gasto: llegan con los comprobados
            continue
        fecha = pd.to_datetime(x.get("Fecha"), errors="coerce")
        if pd.isna(fecha):
            # Avances viejos no guardaban fecha ni monto de la solicitud: se usa el día en que se guardó el avance.
            fecha = pd.to_datetime(str(av.get("guardado_en") or "")[:10], errors="coerce")
            if pd.isna(fecha):
                avisos.append(f"Solicitud #{x.get('id', x.get('No', '?'))} sin fecha válida; se omite.")
                continue
            sin_fecha += 1
        solicitudes.append({
            "origen": x.get("id", x.get("No")),
            "fecha": fecha.date().isoformat(),
            "monto": round(_numero(x.get("Monto")), 2),
            "category": (x.get("Category") or "").strip() or None,
            "material": (x.get("Material") or "").strip() or None,
            "employee_name": (x.get("Employee Name") or "").strip() or None,
            "description": (x.get("Description") or "").strip() or None,
            "request_number": (x.get("Request Number") or "").strip() or None,
            "number_of_days": int(_numero(x.get("Number of Days"))),
            "number_of_people": int(_numero(x.get("Number of People"))),
            "applicant_origen": x.get("Applicant"),
        })
    if sin_fecha:
        avisos.append(f"{sin_fecha} solicitud(es) del archivo no traen fecha ni monto (versión anterior de la app): se les pone la fecha en que se guardó el avance ({str(av.get('guardado_en'))[:10]}) y monto $0; ajústalos al comprobar.")
    if vinculadas:
        avisos.append(f"{vinculadas} solicitud(es) de la bitácora ya estaban vinculadas a un gasto; se importan junto con los gastos comprobados, no por separado.") if gastos else \
            avisos.append(f"{vinculadas} solicitud(es) de la bitácora estaban vinculadas a gastos que este archivo no trae; se omiten.")

    pdfs = sum(1 for g in gastos for f in g["facturas"] if f.get("Adjunto"))
    total_f = sum(len(g["facturas"]) for g in gastos)
    return {
        "banco": av.get("banco"), "guardado_en": av.get("guardado_en"), "df": df, "llaves": llaves,
        "gastos": gastos, "no_necesarios": sorted(no_nec), "avisos": avisos,
        "solicitudes": solicitudes,
        "n_movimientos": len(llaves), "n_facturas": total_f, "n_pdfs": pdfs, "n_sin_pdf": total_f - pdfs,
        "fechas": (min(k[0] for k in llaves.values()), max(k[0] for k in llaves.values())) if llaves else None,
        "fechas_solicitudes": (min(x["fecha"] for x in solicitudes), max(x["fecha"] for x in solicitudes)) if solicitudes else None,
    }


def _firma_solicitud(x: dict) -> tuple:
    return (str(x.get("fecha"))[:10], round(float(x.get("monto") or x.get("monto_tentativo") or 0), 2),
            (x.get("category") or "").strip(), (x.get("employee_name") or "").strip(),
            (x.get("description") or "").strip(), (x.get("request_number") or "").strip())


def _solicitudes_por_crear(sb, cuenta_id: str, plan: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """Separa las solicitudes de la bitácora en nuevas y ya existentes en la nube (misma fecha,
    monto, categoría, empleado, descripción y número de request). Si el archivo trae dos
    iguales, se crean ambas; solo se compara contra lo que ya estaba."""
    if not plan.get("solicitudes"):
        return [], []
    hay: dict[tuple, int] = {}
    for x in db.listar_solicitudes(sb, cuenta_id):
        k = _firma_solicitud(x)
        hay[k] = hay.get(k, 0) + 1
    nuevas, ya = [], []
    for x in plan["solicitudes"]:
        k = _firma_solicitud(x)
        if hay.get(k, 0) > 0:
            hay[k] -= 1
            ya.append(x)
        else:
            nuevas.append(x)
    return nuevas, ya


def diagnostico(sb, cuenta_id: str, plan: dict[str, Any]) -> dict[str, Any]:
    """Compara el plan contra lo que ya hay en la nube (sin escribir nada)."""
    if plan["llaves"]:
        existentes = db.listar_movimientos(sb, cuenta_id)
        por_llave = {(r.fecha, r.descripcion, round(float(r.monto), 2), int(r.ocurrencia)): r.estado for r in existentes.itertuples()}
    else:
        por_llave = {}
    nuevos = sum(1 for k in plan["llaves"].values() if k not in por_llave)
    pendientes_sol, ya_sol = _solicitudes_por_crear(sb, cuenta_id, plan)
    uuids = [f.get("UUID") for g in plan["gastos"] for f in g["facturas"] if f.get("UUID") and f["UUID"] != "SIN-UUID"]
    usados = db.uuids_usados(sb, uuids)
    listos, ya_hechos, conflicto = 0, [], []
    for g in plan["gastos"]:
        estados = [por_llave.get(k) for k in g["llaves"]]
        if any(e is not None and e != "pendiente" for e in estados):
            ya_hechos.append(g["idxs"])
        elif any((f.get("UUID") in usados) for f in g["facturas"]):
            conflicto.append(g["idxs"])
        else:
            listos += 1
    return {"movimientos_nuevos": nuevos, "movimientos_existentes": plan["n_movimientos"] - nuevos,
            "gastos_listos": listos, "gastos_ya_comprobados": ya_hechos, "gastos_uuid_usado": conflicto,
            "solicitudes_nuevas": len(pendientes_sol), "solicitudes_existentes": len(ya_sol)}


def ejecutar(sb, cuenta: dict, plan: dict[str, Any], progreso: Callable[[float, str], None] | None = None) -> dict[str, Any]:
    def avanzar(p: float, texto: str) -> None:
        if progreso:
            progreso(min(max(p, 0.0), 1.0), texto)

    rep: dict[str, Any] = {"movimientos_nuevos": 0, "gastos_importados": 0, "pdfs": 0, "omitidos": [], "errores": [], "no_necesarios": 0, "solicitudes": 0}
    por_llave: dict[tuple, tuple] = {}
    if plan["llaves"]:
        avanzar(0.02, "Cargando el estado de cuenta…")
        r = db.subir_estado_cuenta(sb, cuenta["id"], plan["df"])
        rep["movimientos_nuevos"] = r["nuevos"]
        mov = db.listar_movimientos(sb, cuenta["id"])
        por_llave = {(m.fecha, m.descripcion, round(float(m.monto), 2), int(m.ocurrencia)): (m.id, m.estado) for m in mov.itertuples()}

    pendientes_nn = [por_llave[plan["llaves"][i]][0] for i in plan["no_necesarios"]
                     if plan["llaves"][i] in por_llave and por_llave[plan["llaves"][i]][1] == "pendiente"]
    if pendientes_nn:
        db.cambiar_estado_movimientos(sb, pendientes_nn, "no_necesario")
        rep["no_necesarios"] = len(pendientes_nn)

    # Catálogos: los valores del avance que no existan se agregan (se guardan al usarlos).
    existentes = {t: set(db.cargar_catalogo(sb, t)) for t in ("categoria", "material", "empleado")}
    for g in [*plan["gastos"], *plan.get("solicitudes", [])]:
        for tipo, clave in (("categoria", "category"), ("material", "material"), ("empleado", "employee_name")):
            v = (g.get(clave) or "").strip()
            if v and v not in existentes[tipo]:
                db.agregar_catalogo(sb, tipo, v)
                existentes[tipo].add(v)

    total = max(len(plan["gastos"]), 1)
    for n, g in enumerate(plan["gastos"]):
        etiqueta = f"movimiento(s) {', '.join(str(i) for i in g['idxs'])}"
        avanzar(0.1 + 0.9 * n / total, f"Importando gasto {n + 1} de {total}…")
        ids, estados = [], []
        for k in g["llaves"]:
            if k not in por_llave:
                estados.append("falta")
                continue
            ids.append(por_llave[k][0]); estados.append(por_llave[k][1])
        if any(e != "pendiente" for e in estados):
            rep["omitidos"].append(f"{etiqueta}: ya estaba comprobado en la nube.")
            continue
        subidos: list[str] = []
        solicitud_id = None
        try:
            fecha = pd.to_datetime(g["fecha"]).date()
            periodo_id = db.obtener_o_crear_periodo(sb, fecha.year, fecha.month)
            solicitud_id = db.crear_solicitud(sb, cuenta["id"], periodo_id, {
                "fecha": fecha, "monto": g["monto"], "category": g["category"], "material": g["material"],
                "employee_name": g["employee_name"], "description": g["description"], "request_number": g["request_number"],
                "number_of_days": g["number_of_days"], "number_of_people": g["number_of_people"]})["id"]
            payload, avisos_pdf = [], 0
            for f in g["facturas"]:
                adjunto = None
                a = f.get("Adjunto")
                if a and a.get("contenido_b64"):
                    try:
                        pdf, info = adj.comprimir_a_pdf(base64.b64decode(a["contenido_b64"]), a.get("nombre") or "")
                        ruta = db.subir_adjunto(sb, cuenta["id"], f"{fecha.year}-{fecha.month:02d}", pdf)
                        subidos.append(ruta)
                        adjunto = {"ruta": ruta, **{k: info[k] for k in ("nombre_original", "bytes_original", "bytes_final", "sha256")}}
                    except (adj.AdjuntoError, ValueError) as e:
                        avisos_pdf += 1
                        rep["errores"].append(f"{etiqueta}: PDF «{a.get('nombre')}» no se pudo migrar ({e}).")
                payload.append(db.factura_a_json(f, adjunto))
            db.guardar_gasto(sb, solicitud_id, ids, g["category"], g["material"], g["propina"], payload)
            rep["gastos_importados"] += 1
            rep["pdfs"] += len(subidos)
        except db.ErrorNube as e:
            db.borrar_adjuntos_huerfanos(sb, subidos)
            if solicitud_id:
                try:
                    db.eliminar_solicitud(sb, solicitud_id)
                except db.ErrorNube:
                    pass
            rep["errores"].append(f"{etiqueta}: {e}")

    # Solicitudes de la bitácora que aún no tienen gasto: quedan en borrador.
    nuevas, _ya = _solicitudes_por_crear(sb, cuenta["id"], plan)
    for n, x in enumerate(nuevas):
        avanzar(0.95 + 0.05 * n / max(len(nuevas), 1), f"Creando solicitud {n + 1} de {len(nuevas)}…")
        try:
            fecha = pd.to_datetime(x["fecha"]).date()
            periodo_id = db.obtener_o_crear_periodo(sb, fecha.year, fecha.month)
            db.crear_solicitud(sb, cuenta["id"], periodo_id, {**x, "fecha": fecha})
            rep["solicitudes"] += 1
        except db.ErrorNube as e:
            rep["errores"].append(f"Solicitud «{(x.get('description') or x.get('category') or x.get('origen'))}»: {e}")
    avanzar(1.0, "Listo.")
    return rep
