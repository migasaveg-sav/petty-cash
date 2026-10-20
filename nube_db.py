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
        raise ErrorNube("Correo o contraseña incorrectos.") from e
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


def listar_movimientos(sb, cuenta_id: str, estado: str | None = None,
                       periodo_id: str | None = None) -> pd.DataFrame:
    def construir():
        q = sb.table("movimientos").select("*").eq("cuenta_id", cuenta_id)
        if estado:
            q = q.eq("estado", estado)
        if periodo_id:
            q = q.eq("periodo_id", periodo_id)
        return q.order("fecha", desc=False).order("creado_en", desc=False)
    filas = _paginado(construir)
    cols = ["id", "fecha", "descripcion", "monto", "saldo", "estado", "periodo_id", "ocurrencia"]
    df = pd.DataFrame(filas, columns=cols) if filas else pd.DataFrame(columns=cols)
    if not df.empty:
        df["monto"] = df["monto"].astype(float)
    return df


def cambiar_estado_movimientos(sb, ids: list[str], estado: str) -> None:
    if estado not in ("pendiente", "no_necesario"):
        raise ErrorNube("Estado no permitido.")
    if ids:
        _ejecutar(sb.table("movimientos").update({"estado": estado}).in_("id", ids))


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
def listar_gastos(sb, solicitud_ids: list[str]) -> list[dict]:
    """Gastos (con suma de facturas y diferencia) de las solicitudes dadas, con los
    movimientos y facturas que los componen."""
    if not solicitud_ids:
        return []
    gastos = _ejecutar(sb.table("v_gastos").select("*").in_("solicitud_id", solicitud_ids).eq("eliminado", False)) or []
    if not gastos:
        return []
    ids = [g["id"] for g in gastos]
    movs = _ejecutar(sb.table("gasto_movimientos").select("gasto_id,movimiento_id").in_("gasto_id", ids)) or []
    mov_ids = list({m["movimiento_id"] for m in movs})
    detalle_mov = {m["id"]: m for m in (_ejecutar(sb.table("movimientos").select("id,fecha,descripcion,monto").in_("id", mov_ids)) or [])} if mov_ids else {}
    gf = _ejecutar(sb.table("gasto_facturas").select("*").in_("gasto_id", ids)) or []
    fact_ids = list({x["factura_id"] for x in gf})
    detalle_fact = {f["id"]: f for f in (_ejecutar(sb.table("facturas").select("*").in_("id", fact_ids)) or [])} if fact_ids else {}
    adj_ids = list({f["adjunto_id"] for f in detalle_fact.values() if f.get("adjunto_id")})
    adj = {a["id"]: a for a in (_ejecutar(sb.table("adjuntos").select("id,ruta,bytes_original,bytes_final").in_("id", adj_ids)) or [])} if adj_ids else {}
    for f in detalle_fact.values():
        f["adjunto"] = adj.get(f.get("adjunto_id"))
    for g in gastos:
        g["movimientos"] = [detalle_mov[m["movimiento_id"]] for m in movs
                            if m["gasto_id"] == g["id"] and m["movimiento_id"] in detalle_mov]
        g["facturas"] = []
        for x in gf:
            if x["gasto_id"] == g["id"] and x["factura_id"] in detalle_fact:
                g["facturas"].append({**detalle_fact[x["factura_id"]], "monto_aplicado": x["monto_total"]})
    return gastos


def guardar_gasto(sb, solicitud_id: str, movimiento_ids: list[str], categoria: str | None,
                  material: str | None, propina: float, facturas: list[dict]) -> str:
    return _ejecutar(sb.rpc("guardar_gasto", {
        "p_solicitud": solicitud_id,
        "p_movimientos": movimiento_ids,
        "p_categoria": categoria,
        "p_material": material,
        "p_propina": round(float(propina or 0), 2),
        "p_facturas": facturas,
    }))


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
        "nombre_emisor": f.get("Razón Social") or f.get("Razon Social") or f.get("Nombre Emisor") or None,
        "fecha_factura": fecha,
        "monto_total": round(float(f.get("Monto Total") or 0), 2),
        "iva": round(float(f.get("IVA") or 0), 2),
        "isr_retenido": round(float(f.get("ISR Retenido") or 0), 2),
        "iva_retenido": round(float(f.get("IVA Retenido") or 0), 2),
        "ish": round(float(f.get("ISH") or 0), 2),
        "incompleta": bool(f.get("Incompleta")),
        "adjunto": adjunto,
    }


# ----------------------------------------------------------------------
# Pendientes y comentarios
# ----------------------------------------------------------------------
def listar_pendientes_abiertos(sb, cuenta_id: str) -> list[dict]:
    pend = _ejecutar(sb.table("gastos_pendientes").select("*").eq("cuenta_id", cuenta_id)
                     .neq("estado", "resuelto").order("creado_en", desc=True)) or []
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
    return _ejecutar(q.order("cerrado_en", desc=True)) or []


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


def descargar_adjunto(sb, ruta: str) -> bytes:
    try:
        return sb.storage.from_(BUCKET).download(ruta)
    except Exception as e:
        raise ErrorNube(f"No se pudo descargar el archivo: {_mensaje(e)}") from e
