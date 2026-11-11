"""Capa de datos del Petty Cash en la nube (Supabase).

Todas las funciones reciben el cliente de Supabase `sb` ya autenticado con el
usuario (los permisos por cuenta los aplica la base de datos, no esta capa).
No importa Streamlit: se puede probar fuera de la interfaz.

Convención: los errores de la base de datos se convierten en `ErrorNube` con un
mensaje en español listo para mostrar al usuario.
"""
from __future__ import annotations

import datetime
import re
import uuid
from typing import Any

import pandas as pd
from postgrest.exceptions import APIError

BUCKET = "comprobantes"
_PAGINA = 1000


class ErrorNube(Exception):
    """Error de base de datos / red con mensaje amable para el usuario."""


# ----------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------
def _mensaje(e: Exception) -> str:
    if isinstance(e, APIError):
        msg = (getattr(e, "message", None) or str(e) or "").strip()
        if "facturas_uuid_unico" in msg or "facturas_uuid_unico" in str(e):
            return "Una de las facturas (UUID) ya fue usada en otro gasto."
        if "row-level security" in msg:
            return "No tienes permiso para hacer este cambio (o el registro está bloqueado)."
        if "permission denied" in msg:
            return "No tienes permiso para esta operación."
        return msg or "Error de base de datos."
    texto = str(e)
    if "JWT" in texto or "expired" in texto.lower():
        return "Tu sesión expiró. Vuelve a iniciar sesión."
    return texto or e.__class__.__name__


def _ejecutar(consulta) -> Any:
    try:
        return consulta.execute().data
    except APIError as e:
        raise ErrorNube(_mensaje(e)) from e
    except ErrorNube:
        raise
    except Exception as e:  # red, timeout, token...
        raise ErrorNube(_mensaje(e)) from e


def _paginado(construir) -> list[dict]:
    """`construir()` debe regresar un query builder NUEVO (select ya aplicado).
    Pagina de 1000 en 1000 para no truncar resultados grandes."""
    filas: list[dict] = []
    inicio = 0
    while True:
        lote = _ejecutar(construir().range(inicio, inicio + _PAGINA - 1)) or []
        filas.extend(lote)
        if len(lote) < _PAGINA:
            return filas
        inicio += _PAGINA


# ----------------------------------------------------------------------
# Sesión y perfil
# ----------------------------------------------------------------------
def iniciar_sesion(sb, email: str, password: str) -> str:
    """Regresa el id del usuario. Lanza ErrorNube si las credenciales no sirven."""
    try:
        resp = sb.auth.sign_in_with_password({"email": email.strip(), "password": password})
    except Exception as e:
        texto = str(e)
        bajo = texto.lower()
        if "invalid login credentials" in bajo or "invalid_credentials" in bajo:
            raise ErrorNube("Correo o contraseña incorrectos.") from e
        if "not confirmed" in bajo:
            raise ErrorNube("El correo aún no está confirmado. En Supabase marca el usuario como confirmado.") from e
        raise ErrorNube("No se pudo iniciar sesión por un problema de conexión o configuración "
                        f"(revisa SUPABASE_URL y SUPABASE_KEY en Secrets). Detalle: {texto[:200]}") from e
    if not getattr(resp, "user", None):
        raise ErrorNube("Correo o contraseña incorrectos.")
    return str(resp.user.id)


def cerrar_sesion(sb) -> None:
    try:
        sb.auth.sign_out()
    except Exception:
        pass


def sesion_vigente(sb) -> bool:
    """True si hay sesión; refresca el token si ya venció."""
    try:
        return sb.auth.get_session() is not None
    except Exception:
        return False


def cargar_perfil(sb, user_id: str) -> dict | None:
    filas = _ejecutar(sb.table("perfiles").select("*").eq("id", user_id))
    return filas[0] if filas else None


def cargar_cuenta(sb, cuenta_id: str) -> dict | None:
    filas = _ejecutar(sb.table("cuentas").select("*").eq("id", cuenta_id))
    return filas[0] if filas else None


def listar_cuentas(sb) -> list[dict]:
    return _ejecutar(sb.table("cuentas").select("*").order("nombre")) or []


def tolerancia(sb, por_defecto: float = 0.50) -> float:
    filas = _ejecutar(sb.table("configuracion").select("valor").eq("clave", "diferencia_aceptable"))
    return float(filas[0]["valor"]) if filas else por_defecto


# ----------------------------------------------------------------------
# Periodos
# ----------------------------------------------------------------------
def obtener_o_crear_periodo(sb, anio: int, mes: int) -> str:
    return _ejecutar(sb.rpc("obtener_o_crear_periodo", {"p_anio": anio, "p_mes": mes}))


def listar_periodos(sb) -> list[dict]:
    return _ejecutar(sb.table("periodos").select("*").order("anio", desc=True).order("mes", desc=True)) or []


def etiqueta_periodo(p: dict) -> str:
    return f"{p['anio']}-{int(p['mes']):02d}"


def estado_cierre(sb, cuenta_id: str) -> dict[str, str]:
    """{periodo_id: 'abierto'|'cerrado'} para la cuenta."""
    filas = _ejecutar(sb.table("periodos_cuenta").select("periodo_id,estado").eq("cuenta_id", cuenta_id)) or []
    return {f["periodo_id"]: f["estado"] for f in filas}


# ----------------------------------------------------------------------
# Catálogos
# ----------------------------------------------------------------------
def asegurar_catalogos(sb, defaults: dict[str, list[str]]) -> None:
    """Si un catálogo está vacío, lo llena con los valores por defecto (idempotente)."""
    for tipo, valores in defaults.items():
        hay = _ejecutar(sb.table("catalogos").select("id").eq("tipo", tipo).limit(1))
        if hay:
            continue
        filas = [{"tipo": tipo, "valor": v} for v in dict.fromkeys(valores) if str(v).strip()]
        if filas:
            _ejecutar(sb.table("catalogos").upsert(filas, on_conflict="tipo,valor", ignore_duplicates=True))


def cargar_catalogo(sb, tipo: str) -> list[str]:
    filas = _ejecutar(
        sb.table("catalogos").select("valor").eq("tipo", tipo).eq("activo", True).order("valor")
    ) or []
    return [f["valor"] for f in filas]


def agregar_catalogo(sb, tipo: str, valor: str) -> str:
    valor = (valor or "").strip()
    if not valor:
        raise ErrorNube("El valor no puede estar vacío.")
    _ejecutar(sb.table("catalogos").upsert(
        [{"tipo": tipo, "valor": valor}], on_conflict="tipo,valor", ignore_duplicates=True))
    return valor


# ----------------------------------------------------------------------
# Estado de cuenta (movimientos)
# ----------------------------------------------------------------------
def movimientos_desde_df(df: pd.DataFrame) -> tuple[list[dict], int]:
    """Convierte el DataFrame de bank.cargar_estado_cuenta en filas para la base.
    Asigna `ocurrencia` (1,2,3...) a movimientos idénticos (misma fecha, descripción y
    monto) para que cada línea tenga una llave única y se pueda volver a cargar un
    estado de cuenta más reciente sin duplicar lo ya cargado.
    Regresa (filas, n_descartadas_sin_fecha)."""
    filas: list[dict] = []
    vistos: dict[tuple, int] = {}
    descartadas = 0
    for _, r in df.iterrows():
        fecha = pd.to_datetime(r.get("Fecha"), errors="coerce")
        monto = r.get("Monto")
        if pd.isna(fecha) or pd.isna(monto):
            descartadas += 1
            continue
        descripcion = "" if pd.isna(r.get("Descripción")) else str(r.get("Descripción")).strip()
        llave = (fecha.date().isoformat(), descripcion, round(float(monto), 2))
        vistos[llave] = vistos.get(llave, 0) + 1
        saldo = r.get("Saldo")
        filas.append({
            "fecha": llave[0],
            "descripcion": descripcion or "(sin descripción)",
            "monto": llave[2],
            "saldo": None if saldo is None or pd.isna(saldo) else round(float(saldo), 2),
            "ocurrencia": vistos[llave],
        })
    return filas, descartadas


def subir_estado_cuenta(sb, cuenta_id: str, df: pd.DataFrame) -> dict[str, int]:
    """Inserta solo los movimientos que todavía no existen (según fecha+descripción+monto
    +ocurrencia). Cada movimiento se liga al periodo (mes) de su propia fecha."""
    filas, descartadas = movimientos_desde_df(df)
    if not filas:
        raise ErrorNube("No se encontraron movimientos con fecha y monto válidos.")
    periodos: dict[tuple[int, int], str] = {}
    for f in filas:
        anio, mes = int(f["fecha"][:4]), int(f["fecha"][5:7])
        if (anio, mes) not in periodos:
            periodos[(anio, mes)] = obtener_o_crear_periodo(sb, anio, mes)
        f["cuenta_id"] = cuenta_id
        f["periodo_id"] = periodos[(anio, mes)]
    nuevos = 0
    for i in range(0, len(filas), 500):
        lote = filas[i:i + 500]
        insertados = _ejecutar(sb.table("movimientos").upsert(
            lote, on_conflict="cuenta_id,fecha,descripcion,monto,ocurrencia", ignore_duplicates=True))
        nuevos += len(insertados or [])
    return {"nuevos": nuevos, "ya_existian": len(filas) - nuevos, "sin_fecha_o_monto": descartadas}


def listar_movimientos(sb, cuenta_id: str, estado: "str | list[str] | None" = None,
                       periodo_id: str | None = None) -> pd.DataFrame:
    def construir():
        q = sb.table("movimientos").select("*").eq("cuenta_id", cuenta_id)
        if isinstance(estado, (list, tuple)):
            q = q.in_("estado", list(estado))
        elif estado:
            q = q.eq("estado", estado)
        if periodo_id:
            q = q.eq("periodo_id", periodo_id)
        # `creado_en` es igual para todos los movimientos cargados en un mismo archivo: `id` desempata para que el orden sea siempre el mismo
        return q.order("fecha", desc=False).order("creado_en", desc=False).order("id", desc=False)
    filas = _paginado(construir)
    cols = ["id", "fecha", "descripcion", "monto", "saldo", "estado", "periodo_id", "ocurrencia", "motivo_espera", "espera_desde", "nota_revisor"]
    df = pd.DataFrame(filas, columns=cols) if filas else pd.DataFrame(columns=cols)
    if not df.empty:
        df["monto"] = df["monto"].astype(float)
    return df


def cambiar_estado_movimientos(sb, ids: list[str], estado: str, motivo: str | None = None) -> None:
    """estado: 'pendiente' (sin comprobar), 'no_necesario' o 'en_espera' (la cuarta clasificación,
    «Pendiente»: exige un motivo que explique por qué)."""
    if estado not in ("pendiente", "no_necesario", "en_espera"):
        raise ErrorNube("Estado no permitido.")
    if estado == "en_espera" and not (motivo or "").strip():
        raise ErrorNube("Explica por qué el movimiento queda pendiente.")
    if ids:
        cambios = {"estado": estado, "motivo_espera": (motivo or "").strip() or None if estado == "en_espera" else None}
        _ejecutar(sb.table("movimientos").update(cambios).in_("id", ids))


# ----------------------------------------------------------------------
# Solicitudes (bitácora)
# ----------------------------------------------------------------------
def crear_solicitud(sb, cuenta_id: str, periodo_id: str, datos: dict) -> dict:
    fila = {
        "cuenta_id": cuenta_id,
        "periodo_id": periodo_id,
        "fecha": str(datos["fecha"]),
        "monto_tentativo": float(datos.get("monto") or 0),
        "category": datos.get("category") or None,
        "material": datos.get("material") or None,
        "employee_name": datos.get("employee_name") or None,
        "description": (datos.get("description") or "").strip() or None,
        "request_number": (datos.get("request_number") or "").strip() or None,
        "number_of_days": int(datos.get("number_of_days") or 0),
        "number_of_people": int(datos.get("number_of_people") or 0),
    }
    return _ejecutar(sb.table("solicitudes").insert(fila))[0]


def listar_solicitudes(sb, cuenta_id: str, periodo_id: str | None = None) -> list[dict]:
    def construir():
        q = sb.table("solicitudes").select("*").eq("cuenta_id", cuenta_id).eq("eliminado", False)
        if periodo_id:
            q = q.eq("periodo_id", periodo_id)
        return q.order("numero", desc=True)
    return _paginado(construir)


def solicitudes_acumulables(sb, cuenta_id: str) -> list[dict]:
    """Solicitudes aún sin lote cerrado (lo que cuenta para el límite de reembolso)."""
    def construir():
        return (sb.table("solicitudes").select("fecha,monto_tentativo,estado")
                .eq("cuenta_id", cuenta_id).eq("eliminado", False)
                .in_("estado", ["borrador", "enviado_revision", "con_pendientes"]))
    return _paginado(construir)


def actualizar_solicitud(sb, solicitud_id: str, cambios: dict) -> None:
    _ejecutar(sb.table("solicitudes").update(cambios).eq("id", solicitud_id))


def eliminar_solicitud(sb, solicitud_id: str) -> None:
    filas = _ejecutar(sb.table("solicitudes").update({"eliminado": True}).eq("id", solicitud_id))
    if not filas:
        raise ErrorNube("La solicitud no se pudo eliminar (¿ya está en revisión o en un lote?).")


def enviar_a_revision(sb, solicitud_id: str) -> None:
    filas = _ejecutar(sb.table("solicitudes").update({"estado": "enviado_revision"}).eq("id", solicitud_id))
    if not filas:
        raise ErrorNube("No se pudo enviar a revisión.")


# ----------------------------------------------------------------------
# Gastos
# ----------------------------------------------------------------------
def _en_trozos(sb, tabla: str, columnas: str, campo: str, valores: list[str], tam: int = 80) -> list[dict]:
    """select ... where campo in (valores), en trozos para no rebasar el largo de la URL."""
    filas: list[dict] = []
    for i in range(0, len(valores), tam):
        filas += _ejecutar(sb.table(tabla).select(columnas).in_(campo, valores[i:i + tam])) or []
    return filas


def listar_gastos(sb, solicitud_ids: list[str]) -> list[dict]:
    """Gastos (con suma de facturas y diferencia) de las solicitudes dadas, con los
    movimientos y facturas que los componen."""
    if not solicitud_ids:
        return []
    gastos = [g for g in _en_trozos(sb, "v_gastos", "*", "solicitud_id", solicitud_ids) if not g.get("eliminado")]
    if not gastos:
        return []
    ids = [g["id"] for g in gastos]
    movs = _en_trozos(sb, "gasto_movimientos", "gasto_id,movimiento_id", "gasto_id", ids)
    mov_ids = list({m["movimiento_id"] for m in movs})
    detalle_mov = {m["id"]: m for m in _en_trozos(sb, "movimientos", "id,fecha,descripcion,monto", "id", mov_ids)} if mov_ids else {}
    gf = _en_trozos(sb, "gasto_facturas", "*", "gasto_id", ids)
    fact_ids = list({x["factura_id"] for x in gf})
    detalle_fact = {f["id"]: f for f in _en_trozos(sb, "facturas", "*", "id", fact_ids)} if fact_ids else {}
    adj_ids = list({f["adjunto_id"] for f in detalle_fact.values() if f.get("adjunto_id")})
    adj = {a["id"]: a for a in _en_trozos(sb, "adjuntos", "id,ruta,bytes_original,bytes_final", "id", adj_ids)} if adj_ids else {}
    for f in detalle_fact.values():
        f["adjunto"] = adj.get(f.get("adjunto_id"))
    movs_por_gasto: dict[str, list[dict]] = {}
    for m in movs:
        if m["movimiento_id"] in detalle_mov:
            movs_por_gasto.setdefault(m["gasto_id"], []).append(detalle_mov[m["movimiento_id"]])
    fact_por_gasto: dict[str, list[dict]] = {}
    for x in gf:
        if x["factura_id"] in detalle_fact:
            fact_por_gasto.setdefault(x["gasto_id"], []).append(
                {**detalle_fact[x["factura_id"]], "monto_aplicado": x["monto_total"], "iva_aplicado": x.get("iva", 0),
                 "isr_aplicado": x.get("isr_retenido", 0), "iva_ret_aplicado": x.get("iva_retenido", 0), "ish_aplicado": x.get("ish", 0)})
    try:
        tc = {x["gasto_id"]: x.get("detalle") for x in _en_trozos(sb, "gasto_tc", "gasto_id,detalle", "gasto_id", ids)}
    except Exception:
        tc = {}   # todavía no se corrió el script 013
    try:
        devs = [d for d in _en_trozos(sb, "gasto_devoluciones", "*", "gasto_id", ids) if not d.get("anulada")]
        tips = _en_trozos(sb, "gasto_tip", "gasto_id,adjunto_id", "gasto_id", ids)
        adj_tip = {a["id"]: a for a in _en_trozos(sb, "adjuntos", "id,ruta", "id", list({t["adjunto_id"] for t in tips}))} if tips else {}
        tip_por_gasto = {t["gasto_id"]: adj_tip.get(t["adjunto_id"]) for t in tips}
    except Exception:
        devs, tip_por_gasto = [], {}   # todavía no se corrió el script 015
    devs_por_gasto: dict[str, list[dict]] = {}
    for d in devs:
        devs_por_gasto.setdefault(d["gasto_id"], []).append(d)
    for g in gastos:
        g["movimientos"] = movs_por_gasto.get(g["id"], [])
        g["facturas"] = fact_por_gasto.get(g["id"], [])
        g["dif_tc"] = g["id"] in tc
        g["detalle_tc"] = tc.get(g["id"])
        g["devoluciones"] = devs_por_gasto.get(g["id"], [])
        g["devolucion_total"] = round(sum(float(d["monto"]) for d in g["devoluciones"]), 2)
        g["tip_adjunto"] = tip_por_gasto.get(g["id"])
    return gastos


def guardar_gasto(sb, solicitud_id: str, movimiento_ids: list[str], categoria: str | None,
                  material: str | None, propina: float, facturas: list[dict],
                  dif_tc: bool = False, detalle_tc: str | None = None, propina_adjunto: dict | None = None) -> str:
    """Guarda un gasto. dif_tc=True marca «factura con diferencia de tipo de cambio» (exige detalle; ignora la tolerancia)."""
    if dif_tc and not (detalle_tc or "").strip():
        raise ErrorNube("Explica en «Detalles» por qué hay diferencia por tipo de cambio.")
    params = {
        "p_solicitud": solicitud_id,
        "p_movimientos": movimiento_ids,
        "p_categoria": categoria,
        "p_material": material,
        "p_propina": round(float(propina or 0), 2),
        "p_facturas": facturas,
    }
    if dif_tc:   # sin la marca se llama igual que antes (compatible aunque aún no se corra el script 013)
        params["p_dif_tc"] = True
        params["p_detalle_tc"] = detalle_tc.strip()
    if propina_adjunto:   # comprobante de la propina; sin él, la propina queda como «tip sin comprobación»
        params["p_propina_adjunto"] = propina_adjunto
    return _ejecutar(sb.rpc("guardar_gasto", params))


def revertir_gasto(sb, gasto_id: str) -> None:
    _ejecutar(sb.rpc("revertir_gasto", {"p_gasto": gasto_id}))


def factura_a_json(f: dict, adjunto: dict | None = None) -> dict:
    """Convierte la factura del parser (parse_cfdi / captura manual) al formato de guardar_gasto."""
    uuid_cfdi = f.get("UUID")
    if not uuid_cfdi or uuid_cfdi == "SIN-UUID":
        uuid_cfdi = None
    fecha = str(f.get("Fecha Factura") or "")[:10] or None
    return {
        "uuid_cfdi": uuid_cfdi,
        "rfc_emisor": f.get("RFC Emisor") or None,
        # Una factura manual (sin XML) no trae razón social: se guarda el concepto («FACEBOOK») para poder identificarla.
        "nombre_emisor": (f.get("Razón Social") or f.get("Razon Social") or f.get("Nombre Emisor")
                          or ((f.get("Concepto") or "").strip()[:80] if uuid_cfdi is None else None) or None),
        "fecha_factura": fecha,
        "monto_total": round(float(f.get("Monto Total") or 0), 2),
        "iva": round(float(f.get("IVA") or 0), 2),
        "isr_retenido": round(float(f.get("ISR Retenido") or 0), 2),
        "iva_retenido": round(float(f.get("IVA Retenido") or 0), 2),
        "ish": round(float(f.get("ISH") or 0), 2),
        "incompleta": bool(f.get("Incompleta")),
        "fuente": "manual" if str(f.get("Fuente") or "").strip().lower() == "manual" or uuid_cfdi is None else "xml",
        "adjunto": adjunto,
    }


# ----------------------------------------------------------------------
# Pendientes y comentarios
# ----------------------------------------------------------------------
def listar_pendientes_abiertos(sb, cuenta_id: str) -> list[dict]:
    pend = _ejecutar(sb.table("gastos_pendientes").select("*").eq("cuenta_id", cuenta_id)
                     .neq("estado", "resuelto").order("creado_en", desc=True).order("id", desc=False)) or []
    if not pend:
        return []
    gastos = {g["id"]: g for g in (_ejecutar(sb.table("gastos").select("id,solicitud_id,monto_estado")
                                              .in_("id", list({p["gasto_id"] for p in pend}))) or [])}
    sol_ids = list({g["solicitud_id"] for g in gastos.values()})
    sols = {s["id"]: s for s in (_ejecutar(sb.table("solicitudes").select("id,numero,description")
                                            .in_("id", sol_ids)) or [])} if sol_ids else {}
    coment = _ejecutar(sb.table("comentarios").select("*").in_("pendiente_id", [p["id"] for p in pend])
                       .order("creado_en")) or []
    autores = {}
    ids_autores = list({c["autor_id"] for c in coment})
    if ids_autores:
        autores = {a["id"]: a["nombre"] for a in (_ejecutar(sb.table("perfiles").select("id,nombre").in_("id", ids_autores)) or [])}
    for p in pend:
        g = gastos.get(p["gasto_id"], {})
        s = sols.get(g.get("solicitud_id"), {})
        p["solicitud_numero"] = s.get("numero")
        p["solicitud_descripcion"] = s.get("description")
        p["monto_estado"] = g.get("monto_estado")
        p["comentarios"] = [{**c, "autor": autores.get(c["autor_id"], "—")} for c in coment if c["pendiente_id"] == p["id"]]
    return pend


def agregar_comentario(sb, cuenta_id: str, pendiente_id: str, texto: str) -> None:
    if not (texto or "").strip():
        raise ErrorNube("Escribe el comentario.")
    _ejecutar(sb.table("comentarios").insert({"cuenta_id": cuenta_id, "pendiente_id": pendiente_id, "texto": texto.strip()}))


def resolver_pendiente(sb, pendiente_id: str, comentario: str | None = None) -> None:
    _ejecutar(sb.rpc("resolver_pendiente", {"p_pendiente": pendiente_id, "p_comentario": comentario or None}))


# ----------------------------------------------------------------------
# Lotes
# ----------------------------------------------------------------------
def listar_lotes(sb, cuenta_id: str | None = None) -> list[dict]:
    q = sb.table("lotes").select("*")
    if cuenta_id:
        q = q.eq("cuenta_id", cuenta_id)
    return _ejecutar(q.order("cerrado_en", desc=True).order("id", desc=False)) or []


# ----------------------------------------------------------------------
# Revisor: visto bueno, devolución, pagos y cierre de mes
# ----------------------------------------------------------------------
def cerrar_lote(sb, cuenta_id: str, periodo_id: str, solicitud_ids: list[str]) -> str:
    """Visto bueno: crea y cierra (bloquea) el lote de pago de una cuenta y un mes. Regresa el id del lote."""
    return _ejecutar(sb.rpc("cerrar_lote", {"p_cuenta": cuenta_id, "p_periodo": periodo_id, "p_solicitudes": solicitud_ids}))


def pagar_lote(sb, lote_id: str, fecha, referencia: str) -> None:
    _ejecutar(sb.rpc("pagar_lote", {"p_lote": lote_id, "p_fecha": str(fecha), "p_referencia": referencia}))


def registrar_pago_lote(sb, lote_id: str, fecha, monto: float, referencia: str, adjunto: dict | None = None) -> None:
    """Registra un pago (total o parcial) de un lote. El lote queda «pagado» cuando lo pagado alcanza su monto.
    `adjunto` (opcional, requiere el script 017): {ruta, nombre_original, bytes_original, bytes_final, sha256} del comprobante ya subido."""
    if not (referencia or "").strip():
        raise ErrorNube("Escribe la referencia / folio del pago.")
    if not monto or float(monto) <= 0:
        raise ErrorNube("El monto del pago debe ser mayor a cero.")
    params = {"p_lote": lote_id, "p_fecha": str(fecha), "p_monto": round(float(monto), 2), "p_referencia": referencia.strip()}
    if adjunto:
        params["p_adjunto"] = adjunto
    _ejecutar(sb.rpc("registrar_pago_lote", params))


def adjuntar_comprobante_pago(sb, pago_id: str, adjunto: dict) -> None:
    _ejecutar(sb.rpc("adjuntar_comprobante_pago", {"p_pago": pago_id, "p_adjunto": adjunto}))


def adjuntar_comprobante_devolucion(sb, dev_id: str, adjunto: dict) -> None:
    _ejecutar(sb.rpc("adjuntar_comprobante_devolucion", {"p_dev": dev_id, "p_adjunto": adjunto}))


def _adjuntos_por_id(sb, ids: list[str]) -> dict[str, dict]:
    ids = [i for i in dict.fromkeys(ids) if i]
    if not ids:
        return {}
    return {a["id"]: a for a in _en_trozos(sb, "adjuntos", "id,ruta,nombre_original,bytes_final", "id", ids)}


def listar_pagos_cuenta(sb, cuenta_id: str | None = None) -> list[dict]:
    """Todos los pagos de lotes (de una cuenta o de todas), del más reciente al más antiguo, con su lote y su comprobante."""
    def construir():
        q = sb.table("pagos_lote").select("*")
        if cuenta_id:
            q = q.eq("cuenta_id", cuenta_id)
        return q.order("fecha_pago", desc=True).order("creado_en", desc=True).order("id", desc=False)
    pagos = _paginado(construir)
    if not pagos:
        return []
    lotes = {l["id"]: l for l in _en_trozos(sb, "lotes", "id,numero,periodo_id,monto,monto_pagado,estado,num_gastos", "id", list({p["lote_id"] for p in pagos}))}
    adj = _adjuntos_por_id(sb, [p.get("adjunto_id") for p in pagos])
    return [{**p, "lote": lotes.get(p["lote_id"], {}), "adjunto": adj.get(p.get("adjunto_id"))} for p in pagos]


def listar_pagos_lote(sb, lote_id: str) -> list[dict]:
    return _ejecutar(sb.table("pagos_lote").select("*").eq("lote_id", lote_id).order("fecha_pago", desc=False)
                     .order("creado_en", desc=False)) or []


def reclasificar_fuente_facturas(sb, factura_ids: list[str], fuente: str, motivo: str) -> int:
    """El revisor corrige si una factura se comprobó con XML o a mano. Regresa cuántas cambió."""
    if fuente not in ("xml", "manual"):
        raise ErrorNube("Tipo de comprobación no válido.")
    if not (motivo or "").strip():
        raise ErrorNube("Escribe el motivo del cambio.")
    return int(_ejecutar(sb.rpc("reclasificar_fuente_facturas", {"p_facturas": factura_ids, "p_fuente": fuente, "p_motivo": motivo.strip()})) or 0)


def marcar_devolucion(sb, gasto_id: str, monto: float, nota: str) -> None:
    """El revisor cataloga un gasto (completo o parcial) como «sin comprobación, devolución»."""
    if not monto or float(monto) <= 0:
        raise ErrorNube("Captura el monto a devolver.")
    if not (nota or "").strip():
        raise ErrorNube("Explica el motivo de la devolución.")
    _ejecutar(sb.rpc("marcar_devolucion", {"p_gasto": gasto_id, "p_monto": round(float(monto), 2), "p_nota": nota.strip()}))


def anular_devolucion(sb, dev_id: str, motivo: str) -> None:
    if not (motivo or "").strip():
        raise ErrorNube("Escribe el motivo.")
    _ejecutar(sb.rpc("anular_devolucion", {"p_dev": dev_id, "p_motivo": motivo.strip()}))


def registrar_devolucion_recibida(sb, dev_id: str, fecha, referencia: str, adjunto: dict | None = None) -> None:
    if not (referencia or "").strip():
        raise ErrorNube("Escribe la referencia de la devolución.")
    params = {"p_dev": dev_id, "p_fecha": str(fecha), "p_referencia": referencia.strip()}
    if adjunto:
        params["p_adjunto"] = adjunto   # requiere el script 017
    _ejecutar(sb.rpc("registrar_devolucion_recibida", params))


def listar_devoluciones(sb, cuenta_id: str | None = None) -> list[dict]:
    """Devoluciones vigentes (no anuladas) con los datos de su gasto y solicitud. Una fila por devolución."""
    def construir():
        q = sb.table("gasto_devoluciones").select("*").eq("anulada", False)
        if cuenta_id:
            q = q.eq("cuenta_id", cuenta_id)
        return q.order("creado_en", desc=True).order("id", desc=False)
    devs = _paginado(construir)
    if not devs:
        return []
    gastos = {g["id"]: g for g in _en_trozos(sb, "gastos", "id,solicitud_id,monto_estado,categoria,eliminado", "id", list({d["gasto_id"] for d in devs}))}
    sols = {x["id"]: x for x in _en_trozos(sb, "solicitudes", "id,numero,description,employee_name,periodo_id", "id",
                                           list({g["solicitud_id"] for g in gastos.values()}))}
    adj = _adjuntos_por_id(sb, [d.get("devuelto_adjunto_id") for d in devs])
    out = []
    for d in devs:
        g = gastos.get(d["gasto_id"])
        if not g or g.get("eliminado"):
            continue
        out.append({**d, "gasto": g, "solicitud": sols.get(g["solicitud_id"], {}), "devuelto_adjunto": adj.get(d.get("devuelto_adjunto_id"))})
    return out


def desbloquear_lote(sb, lote_id: str, motivo: str) -> None:
    _ejecutar(sb.rpc("desbloquear_lote", {"p_lote": lote_id, "p_motivo": motivo}))


def marcar_pendiente(sb, gasto_id: str, motivo: str, comentario: str | None = None) -> str:
    """El revisor devuelve un gasto al aplicante con un pendiente (la solicitud regresa a «con pendientes»)."""
    return _ejecutar(sb.rpc("marcar_pendiente", {"p_gasto": gasto_id, "p_motivo": motivo, "p_comentario": comentario or None}))


def reclasificar_movimientos(sb, ids: list[str], estado: str, nota: str, motivo_espera: str | None = None) -> int:
    """El revisor cambia la clasificación de movimientos (también los ya comprobados: se revierte su gasto).
    estado: 'pendiente' (sin comprobar) | 'no_necesario' | 'en_espera' (pendiente, exige motivo_espera).
    Regresa cuántos gastos comprobados se revirtieron."""
    if estado not in ("pendiente", "no_necesario", "en_espera"):
        raise ErrorNube("Clasificación no permitida.")
    if not (nota or "").strip():
        raise ErrorNube("Escribe la nota para el aplicante: por qué se reclasifica.")
    if estado == "en_espera" and not (motivo_espera or "").strip():
        raise ErrorNube("Explica por qué el movimiento queda pendiente.")
    return int(_ejecutar(sb.rpc("reclasificar_movimientos", {"p_movimientos": ids, "p_estado": estado, "p_nota": nota,
                                                             "p_motivo_espera": (motivo_espera or "").strip() or None})) or 0)


def gastos_de_lote(sb, lote_id: str) -> list[dict]:
    """Gastos incluidos en un lote, con los datos de su solicitud."""
    gastos = _ejecutar(sb.table("v_gastos").select("*").eq("lote_id", lote_id).eq("eliminado", False)) or []
    if not gastos:
        return []
    sol_ids = list({g["solicitud_id"] for g in gastos})
    sols = {x["id"]: x for x in (_ejecutar(sb.table("solicitudes").select("id,numero,fecha,description,employee_name,category")
                                           .in_("id", sol_ids)) or [])}
    for g in gastos:
        g["solicitud"] = sols.get(g["solicitud_id"], {})
    return sorted(gastos, key=lambda g: (g["solicitud"].get("numero") or 0, str(g["id"])))


def listar_cobertura(sb, periodo_id: str) -> list[dict]:
    return _ejecutar(sb.table("v_cobertura_mes").select("*").eq("periodo_id", periodo_id)) or []


def estados_cierre_periodo(sb, periodo_id: str) -> dict[str, str]:
    """{cuenta_id: 'abierto'|'cerrado'} del mes dado, para todas las cuentas (vista del revisor)."""
    filas = _ejecutar(sb.table("periodos_cuenta").select("cuenta_id,estado").eq("periodo_id", periodo_id)) or []
    return {f["cuenta_id"]: f["estado"] for f in filas}


def cerrar_mes_cuenta(sb, periodo_id: str, cuenta_id: str) -> None:
    _ejecutar(sb.rpc("cerrar_mes_cuenta", {"p_periodo": periodo_id, "p_cuenta": cuenta_id}))


def reabrir_mes_cuenta(sb, periodo_id: str, cuenta_id: str, motivo: str) -> None:
    _ejecutar(sb.rpc("reabrir_mes_cuenta", {"p_periodo": periodo_id, "p_cuenta": cuenta_id, "p_motivo": motivo}))


def aplicantes_por_cuenta(sb) -> dict[str, str]:
    """{cuenta_id: nombre del aplicante}."""
    filas = _ejecutar(sb.table("perfiles").select("nombre,cuenta_id,rol").eq("rol", "aplicante")) or []
    return {f["cuenta_id"]: f["nombre"] for f in filas if f.get("cuenta_id")}


# ----------------------------------------------------------------------
# Adjuntos (Storage)
# ----------------------------------------------------------------------
def subir_adjunto(sb, cuenta_id: str, etiqueta_mes: str, pdf: bytes) -> str:
    ruta = f"{cuenta_id}/{etiqueta_mes}/{uuid.uuid4().hex}.pdf"
    try:
        sb.storage.from_(BUCKET).upload(path=ruta, file=pdf, file_options={"content-type": "application/pdf"})
    except Exception as e:
        raise ErrorNube(f"No se pudo subir el archivo: {_mensaje(e)}") from e
    return ruta


def borrar_adjuntos_huerfanos(sb, rutas: list[str]) -> None:
    if rutas:
        try:
            sb.storage.from_(BUCKET).remove(rutas)
        except Exception:
            pass


def url_firmada(sb, ruta: str, segundos: int = 3600) -> str | None:
    """Enlace temporal al PDF (para abrirlo en una pestaña nueva). None si no se pudo generar."""
    try:
        r = sb.storage.from_(BUCKET).create_signed_url(ruta, segundos)
        url = r.get("signedURL") or r.get("signedUrl") or r.get("signed_url") if isinstance(r, dict) else None
        if url and url.startswith("/"):
            base = str(getattr(sb, "supabase_url", "") or "").rstrip("/")
            url = f"{base}/storage/v1{url}" if "/storage/v1" not in url else f"{base}{url}"
        return url
    except Exception:
        return None


def descargar_adjunto(sb, ruta: str) -> bytes:
    try:
        return sb.storage.from_(BUCKET).download(ruta)
    except Exception as e:
        raise ErrorNube(f"No se pudo descargar el archivo: {_mensaje(e)}") from e


# ----------------------------------------------------------------------
# Envío del estado de cuenta a revisión (005_envios_estado_cuenta.sql)
# ----------------------------------------------------------------------
def enviar_estado_cuenta(sb, periodo_id: str, nota: str | None = None, solicitud_ids: list[str] | None = None) -> str:
    """Envía a revisión las solicitudes del mes con gastos comprobados y guarda la foto del avance.
    Con `solicitud_ids` solo se envían esas (requiere el script 011); sin él, todas las elegibles."""
    params = {"p_periodo": periodo_id, "p_nota": nota}
    if solicitud_ids is not None:
        params["p_solicitudes"] = solicitud_ids
    try:
        return _ejecutar(sb.rpc("enviar_estado_cuenta", params))
    except ErrorNube as e:
        if solicitud_ids is not None and "p_solicitudes" in str(e):
            raise ErrorNube("Falta correr el script 011_enviar_seleccion.sql en Supabase para enviar solo las solicitudes marcadas.") from e
        raise


def listar_envios(sb, cuenta_id: str | None = None, periodo_id: str | None = None) -> list[dict]:
    q = sb.table("envios_estado_cuenta").select("*")
    if cuenta_id:
        q = q.eq("cuenta_id", cuenta_id)
    if periodo_id:
        q = q.eq("periodo_id", periodo_id)
    return _ejecutar(q.order("enviado_en", desc=True)) or []


COLUMNAS_REVISION = [
    "id", "cuenta_id", "periodo_id", "fecha", "descripcion", "monto", "saldo", "estado", "gasto_id", "categoria",
    "material", "monto_estado", "suma_facturas", "diferencia", "tiene_diferencia", "propina", "solicitud_id",
    "solicitud_numero", "solicitud_estado", "employee_name", "solicitud_descripcion", "request_number",
    "lote_id", "lote_numero", "lote_estado", "fecha_pago", "referencia_pago", "motivo_espera", "espera_desde",
    "nota_revisor", "nota_revisor_en",
]


def listar_movimientos_revision(sb, cuenta_id: str, periodo_id: str) -> pd.DataFrame:
    """Todo el estado de cuenta del mes, un renglón por movimiento, con su solicitud/gasto/lote."""
    def construir():
        return (sb.table("v_movimientos_revision").select("*").eq("cuenta_id", cuenta_id)
                .eq("periodo_id", periodo_id).order("fecha", desc=False).order("id", desc=False))
    filas = _paginado(construir)
    df = pd.DataFrame(filas, columns=COLUMNAS_REVISION) if filas else pd.DataFrame(columns=COLUMNAS_REVISION)
    if not df.empty:
        df["monto"] = df["monto"].astype(float)
    return df


def uuids_usados(sb, uuids: list[str]) -> set[str]:
    """Cuáles de estos UUID de CFDI ya están en alguna factura (que no esté eliminada)."""
    usados: set[str] = set()
    lista = [u for u in dict.fromkeys(uuids) if u]
    for i in range(0, len(lista), 100):
        filas = _ejecutar(sb.table("facturas").select("uuid_cfdi").in_("uuid_cfdi", lista[i:i + 100]).eq("eliminado", False)) or []
        usados |= {f["uuid_cfdi"] for f in filas}
    return usados


# ----------------------------------------------------------------------
# Lotes enviados a revisión (envíos numerados) y reportes
# ----------------------------------------------------------------------
def listar_envio_gastos(sb, cuenta_id: str, periodo_id: str | None = None) -> list[dict]:
    """Cada gasto de cada envío (del mes dado, o de todos) con su resultado (en_revision / aprobado / devuelto)."""
    def construir():
        q = sb.table("v_envio_gastos").select("*").eq("cuenta_id", cuenta_id)
        if periodo_id:
            q = q.eq("periodo_id", periodo_id)
        return q.order("envio_numero", desc=False).order("gasto_id", desc=False)
    return _paginado(construir)


def listar_envios_resumen(sb, cuenta_id: str, periodo_id: str) -> list[dict]:
    """Un renglón por envío: monto enviado vs. aprobado para pago, devuelto, en revisión, pagado y fecha de pago."""
    return _ejecutar(sb.table("v_envios_resumen").select("*").eq("cuenta_id", cuenta_id)
                     .eq("periodo_id", periodo_id).order("numero", desc=False)) or []


def solicitudes_con_pendiente_abierto(sb, cuenta_id: str) -> set[str]:
    """Ids de solicitudes que tienen algún pendiente abierto del revisor (no se pueden reenviar)."""
    pend = _ejecutar(sb.table("gastos_pendientes").select("gasto_id").eq("cuenta_id", cuenta_id).neq("estado", "resuelto")) or []
    if not pend:
        return set()
    ids = list({p["gasto_id"] for p in pend})
    gastos = _ejecutar(sb.table("gastos").select("solicitud_id").in_("id", ids)) or []
    return {g["solicitud_id"] for g in gastos}
