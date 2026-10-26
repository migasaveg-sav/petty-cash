"""Comprobación de Caja Chica — versión en la nube (aplicante y revisor).

Se ejecuta con:  streamlit run app_nube.py
Requiere los secretos SUPABASE_URL y SUPABASE_KEY (clave anon/publishable).
Reutiliza bank.py, cfdi.py, catalog.py, matching.py y persistence.py de la app actual.
"""
from __future__ import annotations

import base64
import collections
import datetime

import pandas as pd
import streamlit as st

import adjuntos as adj
import estilos
import importar
import nube_db as db
import reportes_nube as reportes
from bank import BankStatementError, cargar_estado_cuenta
from catalog import CATEGORIAS_SOLICITUD_DEFAULT, EMPLEADOS_DEFAULT, MATERIALES_DEFAULT
from cfdi import CFDIParseError, parse_cfdi
from matching import DIFERENCIA_ACEPTABLE, resumen_gasto_bitacora
from persistence import hoy_cdmx

CATEGORIAS_CON_PROPINA = {"Client Entertainment", "Travel Meal"}
MOTIVOS_ESPERA_DEFAULT = ["Devolución en trámite", "Aclaración con el banco", "Falta la factura del proveedor", "Reembolso por recibir", "Otro"]
MOTIVOS_DEFAULT = ["Falta factura", "Falta PDF", "Monto no cuadra", "Falta aprobación", "Duplicado sospechoso", "Otro"]
ESTADOS_SOLICITUD = {
    "borrador": "✏️ Borrador", "enviado_revision": "🕓 En revisión", "con_pendientes": "🚩 Con pendientes",
    "en_lote": "🔒 En lote cerrado", "pagado": "✅ Pagado",
}
EDITABLES = ("borrador", "con_pendientes")

st.set_page_config(page_title="Caja Chica — nube", page_icon="💼", layout="wide")
estilos.aplicar()


def money(x) -> str:
    v = float(x or 0)
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def money_md(x) -> str:
    """Igual que money(), con el $ escapado para que Streamlit no lo tome como fórmula LaTeX."""
    return money(x).replace("$", "\\$")


def _flash(msg: str, tipo: str = "success") -> None:
    st.session_state["_flash"] = (tipo, msg)


def _mostrar_flash() -> None:
    f = st.session_state.pop("_flash", None)
    if f:
        {"success": st.success, "error": st.error, "warning": st.warning, "info": st.info}[f[0]](f[1])


# ======================================================================
# Conexión y sesión
# ======================================================================
def _cliente():
    if "sb" not in st.session_state:
        try:
            url, key = st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"]
        except Exception:
            st.error("Faltan los secretos SUPABASE_URL y SUPABASE_KEY. Configúralos en Streamlit (Settings → Secrets).")
            st.stop()
        from supabase import create_client
        # Un cliente POR SESIÓN de navegador: nunca se comparte entre usuarios.
        st.session_state.sb = create_client(url, key)
    return st.session_state.sb


def _pantalla_login(sb) -> None:
    st.markdown(estilos.encabezado(), unsafe_allow_html=True)
    _, centro, _ = st.columns([1, 1.2, 1])
    with centro:
        with st.form("login"):
            st.subheader("Iniciar sesión")
            email = st.text_input("Correo")
            password = st.text_input("Contraseña", type="password")
            entrar = st.form_submit_button("Entrar", type="primary", use_container_width=True)
        if entrar:
            try:
                st.session_state.user_id = db.iniciar_sesion(sb, email, password)
                st.session_state.pop("perfil", None)
                st.rerun()
            except db.ErrorNube as e:
                st.error(str(e))


def _salir(sb) -> None:
    db.cerrar_sesion(sb)
    for k in list(st.session_state.keys()):
        del st.session_state[k]
    st.rerun()


# ======================================================================
# Utilidades de interfaz
# ======================================================================
def _catalogo(sb, tipo: str) -> list[str]:
    """Catálogo en memoria de la sesión: evita una consulta a Supabase por cada selector en pantalla."""
    cache = st.session_state.setdefault("_cat_cache", {})
    if tipo not in cache:
        cache[tipo] = db.cargar_catalogo(sb, tipo)
    return cache[tipo]


def _invalidar_catalogos() -> None:
    st.session_state.pop("_cat_cache", None)


def _selector_catalogo(sb, etiqueta: str, tipo: str, key: str, valor_actual: str | None = None) -> str | None:
    """Selectbox con el catálogo + una caja para agregar un valor nuevo (se guarda al
    usarlo). Regresa el valor elegido (o None)."""
    opciones = _catalogo(sb, tipo)
    if valor_actual and valor_actual not in opciones:
        opciones = [valor_actual, *opciones]
    sel = st.selectbox(etiqueta, ["—", *opciones], index=(opciones.index(valor_actual) + 1) if valor_actual in opciones else 0, key=f"{key}_sel")
    nuevo = st.text_input(f"{etiqueta}: otro valor (si no está en la lista)", key=f"{key}_nuevo", placeholder="Opcional")
    if nuevo.strip():
        return nuevo.strip()
    return None if sel == "—" else sel


def _guardar_catalogo_si_nuevo(sb, tipo: str, valor: str | None) -> None:
    if valor and valor not in _catalogo(sb, tipo):
        db.agregar_catalogo(sb, tipo, valor)
        _invalidar_catalogos()


def _campos_solicitud(sb, pref: str, v: dict | None = None) -> dict:
    v = v or {}
    c1, c2 = st.columns(2)
    with c1:
        fecha = st.date_input("Fecha del gasto", value=datetime.date.fromisoformat(v["fecha"]) if v.get("fecha") else hoy_cdmx(), key=f"{pref}_fecha")
        category = _selector_catalogo(sb, "Category", "categoria", f"{pref}_cat", v.get("category"))
        employee = _selector_catalogo(sb, "Employee name", "empleado", f"{pref}_emp", v.get("employee_name"))
    with c2:
        monto = st.number_input("Monto (tentativo)", min_value=0.0, step=1.0, format="%.2f", value=float(v.get("monto_tentativo") or 0), key=f"{pref}_monto")
        material = _selector_catalogo(sb, "Material", "material", f"{pref}_mat", v.get("material"))
        request_number = st.text_input("Request number", value=v.get("request_number") or "", key=f"{pref}_req")
    description = st.text_input("Description", value=v.get("description") or "", key=f"{pref}_desc")
    d1, d2 = st.columns(2)
    dias = d1.number_input("Number of days", min_value=0, step=1, value=int(v.get("number_of_days") or 0), key=f"{pref}_dias")
    personas = d2.number_input("Total number of people", min_value=0, step=1, value=int(v.get("number_of_people") or 0), key=f"{pref}_pers")
    return {"fecha": fecha, "monto": monto, "category": category, "material": material, "employee_name": employee,
            "request_number": request_number, "description": description, "number_of_days": dias, "number_of_people": personas}


def _periodo_de(sb, fecha: datetime.date) -> str:
    pid = db.obtener_o_crear_periodo(sb, fecha.year, fecha.month)
    st.session_state.pop("_periodos", None)   # puede haberse creado un mes nuevo
    return pid


# ======================================================================
# Pestaña: Bitácora
# ======================================================================
def _tarjetas_gasto(sb, cuenta: dict) -> None:
    sols = db.solicitudes_acumulables(sb, cuenta["id"])
    fecha = st.date_input("Consultar gasto hasta", value=hoy_cdmx(), key="fecha_consulta")
    res = resumen_gasto_bitacora([{"Fecha": s["fecha"], "Monto": s["monto_tentativo"]} for s in sols], fecha)
    c1, c2, c3 = st.columns(3)
    c1.markdown(estilos.tarjeta("Gasto del día", estilos.money_html(res["dia"]), f"{res['n_dia']} registro(s)", "acento"), unsafe_allow_html=True)
    c2.markdown(estilos.tarjeta("Gasto de la semana", estilos.money_html(res["semana"]), f"{res['n_semana']} registro(s)", "acento"), unsafe_allow_html=True)
    c3.markdown(estilos.tarjeta("Acumulado sin reembolso", estilos.money_html(res["acumulado"]), f"{res['n_acumulado']} registro(s)", "rojo"), unsafe_allow_html=True)
    limite = cuenta.get("limite_reembolso")
    if limite:
        limite = float(limite)
        pct = min(res["acumulado"] / limite, 1.0)
        st.progress(pct, text=f"{pct:.0%} del límite de reembolso ({money(limite).replace('$', chr(92) + '$')})")
        if res["acumulado"] >= limite:
            st.warning("Ya alcanzaste el límite: es momento de solicitar el reembolso.")
        else:
            st.caption(f"Faltan {money_md(limite - res['acumulado'])} para el límite de reembolso.")


ESTILO_ETIQUETA = {   # (fondo, texto): verde = comprobado, blanco = no necesario, rojo = sin comprobar / pendiente
    "comprobado": ("#DCFCE7", "#166534"), "no_necesario": ("#FFFFFF", "#475569"),
    "sin_comprobar": ("#FEE2E2", "#B91C1C"), "pendiente": ("#FEE2E2", "#B91C1C"),
}


def _nueva_tabla() -> None:
    """Cambia la llave de la tabla de la bitácora para que no quede seleccionada una fila que ya cambió."""
    st.session_state["bit_ver"] = st.session_state.get("bit_ver", 0) + 1


def _chips_bitacora(registros: list[dict]) -> str:
    cuenta = collections.Counter(r["estado"] for r in registros)
    movs_comp = sum(r.get("movs", 0) for r in registros if r["estado"] == "comprobado")
    chips = []
    for est in ("comprobado", "no_necesario", "sin_comprobar", "pendiente"):
        fondo, texto = ESTILO_ETIQUETA[est]
        borde = "#CBD5E1" if est == "no_necesario" else texto
        if est == "comprobado":
            etiqueta = f'{cuenta.get(est, 0)} gastos comprobados · {movs_comp} movimientos del banco'
        else:
            etiqueta = f'{cuenta.get(est, 0)} {reportes.ETIQUETA_REGISTRO[est].lower()}'
        chips.append(f'<span style="display:inline-block;background:{fondo};color:{texto};border:1px solid {borde};border-radius:999px;'
                     f'padding:2px 12px;margin:0 6px 4px 0;font-size:.85rem;font-weight:700">{etiqueta}</span>')
    return "".join(chips)


def _tab_bitacora(sb, cuenta: dict, periodo: dict, cerrado: bool) -> None:
    _tarjetas_gasto(sb, cuenta)
    st.divider()
    if cerrado:
        st.info("Este mes ya está cerrado: solo lectura.")
    else:
        with st.expander("➕ Nueva solicitud", expanded=False):
            datos = _campos_solicitud(sb, "nueva")
            if st.button("Guardar solicitud", type="primary", key="guardar_sol"):
                try:
                    for tipo, clave in (("categoria", "category"), ("empleado", "employee_name"), ("material", "material")):
                        _guardar_catalogo_si_nuevo(sb, tipo, datos[clave])
                    db.crear_solicitud(sb, cuenta["id"], _periodo_de(sb, datos["fecha"]), datos)
                    for k in [k for k in st.session_state if k.startswith("nueva_")]:
                        del st.session_state[k]
                    _nueva_tabla()
                    _flash("Solicitud registrada.")
                    st.rerun()
                except db.ErrorNube as e:
                    st.error(str(e))

    sols = db.listar_solicitudes(sb, cuenta["id"], periodo["id"])
    gastos = db.listar_gastos(sb, [x["id"] for x in sols]) if sols else []
    movs = db.listar_movimientos(sb, cuenta["id"], periodo_id=periodo["id"])
    registros = reportes.registros_bitacora(sols, gastos, movs)
    st.markdown(f"##### Bitácora de {db.etiqueta_periodo(periodo)} ({len(registros)} registros)")
    if not registros:
        st.caption("Aún no hay registros en este mes. Carga el estado de cuenta o crea una solicitud.")
        return
    st.markdown(_chips_bitacora(registros), unsafe_allow_html=True)

    f1, f2 = st.columns([1.3, 3])
    filtro = f1.selectbox("Mostrar", ["Todos", *reportes.ETIQUETA_REGISTRO.values()], key="bit_filtro")
    texto = f2.text_input("🔎 Buscar (descripción, concepto, empleado, categoría, #)", key="bit_busca")
    vista = registros
    if filtro != "Todos":
        clave = {v: k for k, v in reportes.ETIQUETA_REGISTRO.items()}[filtro]
        vista = [r for r in vista if r["estado"] == clave]
    if texto.strip():
        t = texto.strip().lower()
        vista = [r for r in vista if t in " ".join(str(r[k]) for k in ("descripcion", "concepto", "empleado", "categoria", "numero")).lower()]
    if not vista:
        st.caption("Ningún registro coincide con el filtro.")
        return

    tabla = pd.DataFrame([{
        "Estado": reportes.ETIQUETA_REGISTRO[r["estado"]], "Fecha": r["fecha"], "#": r["numero"], "Descripción": r["descripcion"],
        "Concepto": r["concepto"], "Empleado": r["empleado"], "Categoría": r["categoria"], "Mov.": r["movs"], "Monto": r["monto"],
        "Situación": r["situacion"]} for r in vista])
    tabla["#"] = tabla["#"].astype("Int64")
    inverso = {v: k for k, v in reportes.ETIQUETA_REGISTRO.items()}

    def _pintar(col):
        return [f"background-color:{ESTILO_ETIQUETA[inverso[v]][0]};color:{ESTILO_ETIQUETA[inverso[v]][1]};font-weight:700;text-align:center" for v in col]

    estilo = tabla.style.apply(_pintar, subset=["Estado"]).format({"Monto": "${:,.2f}"}, na_rep="—")
    ev = st.dataframe(estilo, hide_index=True, use_container_width=True, height=min(36 * (len(tabla) + 1) + 4, 400),
                      on_select="rerun", selection_mode="single-row", key=f"tabla_bit_{st.session_state.get('bit_ver', 0)}",
                      column_config={"#": st.column_config.NumberColumn("#", format="%d", width="small"),
                                     "Estado": st.column_config.TextColumn("Estado", width="small"),
                                     "Mov.": st.column_config.NumberColumn("Mov.", format="%d", width="small", help="Movimientos del estado de cuenta que cubre el gasto")})
    filas = [i for i in (ev.selection.rows if ev and ev.selection else []) if i < len(vista)]
    if not filas:
        st.caption("Haz clic en una fila para abrir el registro y ver sus detalles (gasto, facturas y PDF).")
        return
    gastos_por_sol: dict[str, list[dict]] = {}
    for g in gastos:
        gastos_por_sol.setdefault(g["solicitud_id"], []).append(g)
    _detalle_registro(sb, cuenta, periodo, cerrado, vista[filas[0]], {x["id"]: x for x in sols}, gastos_por_sol, movs)


def _detalle_registro(sb, cuenta: dict, periodo: dict, cerrado: bool, r: dict, sols: dict, gastos_por_sol: dict, movs: pd.DataFrame) -> None:
    fondo, texto = ESTILO_ETIQUETA[r["estado"]]
    etiqueta = (f'<span style="background:{fondo};color:{texto};border:1px solid {"#CBD5E1" if r["estado"] == "no_necesario" else texto};'
                f'border-radius:999px;padding:2px 12px;font-weight:700">{reportes.ETIQUETA_REGISTRO[r["estado"]]}</span>')
    with st.container(border=True):
        if r["tipo"] == "movimiento":
            fila = movs[movs["id"] == r["id"]]
            if fila.empty:
                st.warning("El movimiento ya no está en la lista; vuelve a seleccionar.")
                return
            m = fila.iloc[0]
            st.markdown(f"{etiqueta} &nbsp; **🏦 Movimiento del estado de cuenta** · {m['fecha']}", unsafe_allow_html=True)
            st.write(f"{m['descripcion']}")
            st.write(f"Monto: {money_md(m['monto'])}" + (f" · Saldo: {money_md(m['saldo'])}" if pd.notna(m["saldo"]) else ""))
            if m["estado"] == "en_espera":
                st.info(f"⏳ Pendiente: {m['motivo_espera']}")
            if isinstance(m.get("nota_revisor"), str) and m["nota_revisor"].strip():
                st.warning(f"💬 Nota del revisor: {m['nota_revisor']}")
            if cerrado:
                return
            abiertos = m["estado"] in ("pendiente", "en_espera")
            b0, b1, b2 = st.columns([1.4, 1.4, 1.8])
            if abiertos and b0.button("🔗 Comprobar", type="primary", key=f"bit_cmp_{m['id']}"):
                _nueva_tabla()
                _comprobar_desde_estado(sb, cuenta, periodo, fila)
            if abiertos and b1.button("🚫 No necesario", key=f"bit_nn_{m['id']}"):
                _nueva_tabla()
                _cambiar(sb, [m["id"]], "no_necesario")
            if m["estado"] in ("no_necesario", "en_espera") and b2.button("↩️ Regresar a sin comprobar", key=f"bit_sc_{m['id']}"):
                _nueva_tabla()
                _cambiar(sb, [m["id"]], "pendiente")
            if abiertos:
                st.caption("Para marcarlo como pendiente (devolución u otro caso largo) usa la pestaña «Estado de cuenta».")
            return

        s = sols.get(r["id"])
        if not s:
            st.warning("La solicitud ya no está en la lista; vuelve a seleccionar.")
            return
        gs = gastos_por_sol.get(s["id"], [])
        editable = s["estado"] in EDITABLES and not cerrado
        st.markdown(f"{etiqueta} &nbsp; **Solicitud #{s['numero']}** · {s['fecha']} · {ESTADOS_SOLICITUD.get(s['estado'], s['estado'])}", unsafe_allow_html=True)
        d1, d2, d3 = st.columns(3)
        d1.write(f"**Categoría:** {s.get('category') or '—'}  \n**Material:** {s.get('material') or '—'}")
        d2.write(f"**Empleado:** {s.get('employee_name') or '—'}  \n**Request:** {s.get('request_number') or '—'}")
        d3.write(f"**Monto tentativo:** {money_md(s['monto_tentativo'])}  \n**Días / personas:** {s.get('number_of_days') or 0} / {s.get('number_of_people') or 0}")
        if s.get("description"):
            st.caption(s["description"])
        acciones = st.columns([1.2, 1.6, 1.1, 1.1, 3])
        if editable and acciones[0].button("🔗 Comprobar", type="primary" if not gs else "secondary", key=f"cmp_{s['id']}"):
            st.session_state.comprobando = s
            for k in [k for k in st.session_state if k.startswith("cmp_") and k != f"cmp_{s['id']}"]:
                del st.session_state[k]
            _nueva_tabla()
            st.rerun()
        if editable and gs and acciones[1].button("📤 Enviar a revisión", key=f"env_{s['id']}"):
            try:
                db.enviar_a_revision(sb, s["id"])
                _nueva_tabla()
                _flash(f"Solicitud #{s['numero']} enviada a revisión.")
                st.rerun()
            except db.ErrorNube as e:
                st.error(str(e))
        if editable and acciones[2].button("✏️ Editar", key=f"edit_{s['id']}"):
            st.session_state.editando_sol = None if st.session_state.get("editando_sol") == s["id"] else s["id"]
            st.rerun()
        if editable and not gs and acciones[3].button("🗑️ Eliminar", key=f"del_{s['id']}"):
            try:
                db.eliminar_solicitud(sb, s["id"])
                _nueva_tabla()
                st.rerun()
            except db.ErrorNube as e:
                st.error(str(e))
        if editable and st.session_state.get("editando_sol") == s["id"]:
            st.markdown("**Editar datos de la solicitud**")
            nuevos = _campos_solicitud(sb, f"ed_{s['id']}", s)
            if st.button("Guardar cambios", type="primary", key=f"gc_{s['id']}"):
                try:
                    for tipo, clave in (("categoria", "category"), ("empleado", "employee_name"), ("material", "material")):
                        _guardar_catalogo_si_nuevo(sb, tipo, nuevos[clave])
                    db.actualizar_solicitud(sb, s["id"], {
                        "fecha": str(nuevos["fecha"]), "monto_tentativo": nuevos["monto"], "category": nuevos["category"],
                        "material": nuevos["material"], "employee_name": nuevos["employee_name"],
                        "request_number": nuevos["request_number"] or None, "description": nuevos["description"] or None,
                        "number_of_days": nuevos["number_of_days"], "number_of_people": nuevos["number_of_people"]})
                    st.session_state.editando_sol = None
                    _nueva_tabla()
                    _flash("Cambios guardados.")
                    st.rerun()
                except db.ErrorNube as e:
                    st.error(str(e))
        if not gs:
            st.caption("Esta solicitud todavía no tiene gasto comprobado: pulsa «Comprobar» para ligarla a un movimiento del estado de cuenta.")
        for g in gs:
            _detalle_gasto(sb, g, editable)


def _detalle_gasto(sb, g: dict, editable: bool) -> None:
    st.markdown("---")
    marca = "🟡 " if g.get("tiene_diferencia") else ""
    st.markdown(f"**Gasto {money_md(g['monto_estado'])}** · {g.get('categoria') or '—'} · {marca}diferencia {money_md(g['diferencia'])}"
                + (f" · propina {money(g['propina'])}" if float(g.get("propina") or 0) else ""))
    for m in g["movimientos"]:
        st.caption(f"🏦 {m['fecha']} · {m['descripcion']} · {money_md(m['monto'])}")
    for f in g["facturas"]:
        cols = st.columns([3, 1, 1])
        cols[0].caption(f"🧾 {(f.get('uuid_cfdi') or 'SIN-UUID')} · {f.get('rfc_emisor') or ''} · {f.get('nombre_emisor') or ''} · {f.get('fecha_factura') or ''}")
        cols[1].caption(money_md(f["monto_aplicado"]))
        if f.get("adjunto") and cols[2].button("📎 Ver", key=f"ver_{g['id']}_{f['id']}"):
            st.session_state.adj_ver = f["adjunto"]["ruta"]
    if editable and st.button("↩️ Revertir este gasto", key=f"rev_{g['id']}"):
        try:
            db.revertir_gasto(sb, g["id"])
            _nueva_tabla()
            _flash("Gasto revertido: los movimientos volvieron a pendientes.")
            st.rerun()
        except db.ErrorNube as e:
            st.error(str(e))


def _visor_adjunto(sb) -> None:
    ruta = st.session_state.get("adj_ver")
    if not ruta:
        return
    with st.expander("📎 Comprobante", expanded=True):
        try:
            data = db.descargar_adjunto(sb, ruta)
        except db.ErrorNube as e:
            st.error(str(e))
            return
        st.download_button("⬇️ Descargar PDF", data, file_name="comprobante.pdf", mime="application/pdf", key="dl_adj")
        b64 = base64.b64encode(data).decode()
        st.markdown(f'<iframe src="data:application/pdf;base64,{b64}" width="100%" height="520"></iframe>', unsafe_allow_html=True)
        if st.button("Cerrar visor", key="cerrar_visor"):
            st.session_state.pop("adj_ver", None)
            st.rerun()


# ======================================================================
# Flujo: comprobar una solicitud (pantalla completa)
# ======================================================================
def _limpiar_comprobacion() -> None:
    for k in [k for k in st.session_state if k.startswith("cmp_") or k.startswith("adjf_")]:
        del st.session_state[k]
    st.session_state.pop("comprobando", None)


def _pantalla_comprobar(sb, cuenta: dict, sol: dict, tol: float) -> None:
    st.button("← Volver a la bitácora", on_click=_limpiar_comprobacion, key="volver_cmp")
    st.markdown(f"### 🔗 Comprobar solicitud #{sol['numero']}" if sol.get("id") else "### 🔗 Comprobar movimiento(s) del estado de cuenta")
    st.caption(f"{sol['fecha']} · {money_md(sol['monto_tentativo'])} (tentativo) · {sol.get('category') or '—'} · {sol.get('description') or ''}")

    # ---------- Paso 1: elegir el/los movimiento(s) del estado de cuenta ----------
    pend = db.listar_movimientos(sb, cuenta["id"], estado=["pendiente", "en_espera"])
    elegidos: list[str] = st.session_state.get("cmp_movs", [])
    if not elegidos:
        st.markdown("#### Paso 1 · Elige el gasto en el estado de cuenta")
        if pend.empty:
            st.info("No hay movimientos pendientes. Carga primero el estado de cuenta en la pestaña «Estado de cuenta».")
            return
        c1, c2 = st.columns([2, 1])
        texto = c1.text_input("🔎 Buscar por descripción", key="cmp_busca")
        cerca = c2.checkbox("Ordenar por cercanía al monto tentativo", value=float(sol["monto_tentativo"] or 0) > 0, key="cmp_cerca")
        vista = pend.copy()
        if texto.strip():
            vista = vista[vista["descripcion"].str.contains(texto.strip(), case=False, na=False)]
        if cerca and float(sol["monto_tentativo"] or 0) > 0:
            vista = vista.assign(_d=(vista["monto"].abs() - float(sol["monto_tentativo"])).abs()).sort_values("_d").drop(columns="_d")
        vista = vista.reset_index(drop=True)
        ev = st.dataframe(
            vista[["fecha", "descripcion", "monto", "saldo"]], hide_index=True, use_container_width=True,
            on_select="rerun", selection_mode="multi-row", key="cmp_tabla",
            column_config={"fecha": "Fecha", "descripcion": "Descripción",
                           "monto": st.column_config.NumberColumn("Monto", format="$%.2f"), "saldo": st.column_config.NumberColumn("Saldo", format="$%.2f")})
        filas = [i for i in (ev.selection.rows if ev and ev.selection else []) if i < len(vista)]
        if filas:
            sel = vista.iloc[filas]
            st.info(f"{len(sel)} movimiento(s) seleccionado(s) · total {money_md(sel['monto'].abs().sum())}"
                    + (" · varios cargos comparten las mismas facturas" if len(sel) > 1 else ""))
            if st.button(f"Continuar con {len(sel)} movimiento(s)", type="primary", key="cmp_continuar"):
                st.session_state.cmp_movs = sel["id"].tolist()
                st.rerun()
        return

    movs = pend[pend["id"].isin(elegidos)]
    if len(movs) != len(elegidos):
        st.error("Alguno de los movimientos elegidos ya no está pendiente. Vuelve a elegir.")
        st.session_state.pop("cmp_movs", None)
        return
    monto_cargo = round(float(movs["monto"].abs().sum()), 2)
    st.markdown("#### Paso 1 · Gasto elegido")
    st.dataframe(movs[["fecha", "descripcion", "monto"]], hide_index=True, use_container_width=True,
                 column_config={"fecha": "Fecha", "descripcion": "Descripción", "monto": st.column_config.NumberColumn("Monto", format="$%.2f")})
    if st.button("Cambiar selección", key="cmp_cambiar"):
        st.session_state.pop("cmp_movs", None)
        st.rerun()

    # ---------- Paso 2: facturas ----------
    st.markdown("#### Paso 2 · Agrega la(s) factura(s)")
    c1, c2 = st.columns(2)
    categorias = _catalogo(sb, "categoria")
    cat_ini = sol.get("category")
    with c1:
        categoria = st.selectbox("Categoría", ["—", *categorias], index=(categorias.index(cat_ini) + 1) if cat_ini in categorias else 0, key="cmp_categoria")
    with c2:
        mats = _catalogo(sb, "material")
        mat_ini = sol.get("material")
        material = st.selectbox("Material", ["—", *mats], index=(mats.index(mat_ini) + 1) if mat_ini in mats else 0, key="cmp_material")
    propina = 0.0
    if categoria in CATEGORIAS_CON_PROPINA:
        propina = st.number_input("💵 Propina (no aparece en la factura)", min_value=0.0, step=1.0, format="%.2f", key="cmp_propina")

    facturas: list[dict] = st.session_state.setdefault("cmp_facturas", [])
    ver = st.session_state.setdefault("cmp_xml_ver", 0)
    xmls = st.file_uploader("Subir XML de CFDI (uno o varios)", type=["xml"], accept_multiple_files=True, key=f"cmp_xml_{ver}")
    if xmls:
        usados = {f["datos"]["UUID"] for f in facturas if f["datos"]["UUID"] != "SIN-UUID"}
        for xf in xmls:
            try:
                datos = parse_cfdi(xf.getvalue(), xf.name)
            except CFDIParseError as e:
                st.error(f"No se pudo leer el XML '{xf.name}': {e}")
                continue
            if datos["UUID"] != "SIN-UUID" and datos["UUID"] in usados:
                continue
            facturas.append({"k": len(facturas) + 1000 * (ver + 1), "datos": datos, "adjunto": None, "adj_ver": 0})
            usados.add(datos["UUID"])
            if datos.get("Incompleta"):
                st.warning(f"'{xf.name}' está incompleta: {'; '.join(datos.get('Advertencias', []))}")
        st.session_state.cmp_xml_ver = ver + 1
        st.rerun()

    with st.expander("✏️ Agregar factura manual (sin XML)"):
        with st.form("cmp_manual", clear_on_submit=True):
            mf_fecha = st.date_input("Fecha de factura", value=hoy_cdmx())
            mf_uuid = st.text_input("UUID (opcional)")
            mf_concepto = st.text_input("Concepto")
            mf_rfc = st.text_input("RFC Emisor")
            mf_razon = st.text_input("Razón social emisor")
            mf_iva = st.number_input("IVA", min_value=0.0, format="%.2f")
            mf_monto = st.number_input("Monto Total", min_value=0.0, format="%.2f")
            if st.form_submit_button("Agregar factura manual"):
                u = mf_uuid.strip() or "SIN-UUID"
                if u != "SIN-UUID" and any(f["datos"]["UUID"] == u for f in facturas):
                    st.error("Ese UUID ya está en la lista.")
                else:
                    facturas.append({"k": max([f["k"] for f in facturas], default=0) + 1, "adjunto": None, "adj_ver": 0, "datos": {
                        "Fuente": "Manual", "Archivo": "Manual", "Fecha Factura": str(mf_fecha), "UUID": u, "Concepto": mf_concepto,
                        "RFC Emisor": mf_rfc, "Razón Social": mf_razon, "IVA": mf_iva, "IVA Retenido": 0.0, "ISR Retenido": 0.0,
                        "ISH": 0.0, "Monto Total": mf_monto, "Incompleta": False, "Advertencias": []}})
                    st.rerun()

    if facturas:
        st.markdown("##### 🧾 Facturas de este gasto")
    for f in list(facturas):
        d = f["datos"]
        with st.container(border=True):
            cols = st.columns([3, 1.4, 1.1, 1.1, 0.5])
            cols[0].markdown(f"**{d.get('Archivo', '')}** · `{d['UUID']}`  \n{d.get('RFC Emisor', '')} · {d.get('Concepto', '')}")
            cols[1].write(d.get("Fecha Factura", ""))
            cols[2].write(f"IVA {money_md(d.get('IVA'))}")
            cols[3].write(f"**{money_md(d['Monto Total'])}**")
            if cols[4].button("✕", key=f"cmp_quitar_{f['k']}", help="Quitar factura"):
                facturas.remove(f)
                st.rerun()
            if f["adjunto"]:
                info = f["adjunto"][1]
                a1, a2 = st.columns([4, 1])
                a1.caption(f"📎 {info['nombre_original']} · {info['bytes_original'] / 1024:,.0f} KB → {info['bytes_final'] / 1024:,.0f} KB")
                if a2.button("Quitar PDF", key=f"cmp_qadj_{f['k']}"):
                    f["adjunto"] = None
                    f["adj_ver"] += 1
                    st.rerun()
            else:
                archivo = st.file_uploader("📎 Adjuntar PDF o foto de la factura", type=["pdf", "jpg", "jpeg", "png"], key=f"adjf_{f['k']}_{f['adj_ver']}")
                if archivo is not None:
                    try:
                        pdf, info = adj.comprimir_a_pdf(archivo.getvalue(), archivo.name)
                        f["adjunto"] = (pdf, info)
                        st.rerun()
                    except adj.AdjuntoError as e:
                        st.error(str(e))

    suma_facturas = round(sum(float(f["datos"]["Monto Total"]) for f in facturas), 2)
    diferencia = round(monto_cargo - suma_facturas - propina, 2)
    m1, m2, m3 = st.columns(3)
    m1.metric("Cargo en el estado de cuenta", money(monto_cargo))
    m2.metric("Suma de facturas + propina" if propina else "Suma de facturas", money(suma_facturas + propina))
    m3.metric("Diferencia", money(diferencia))
    if not facturas:
        st.info("Agrega al menos una factura (XML o manual).")
        return
    if abs(diferencia) > tol:
        st.warning(f"La diferencia debe ser de máximo {money_md(tol)} para guardar el gasto.")
        return
    if abs(diferencia) >= 0.005:
        st.info(f"Se guardará con una diferencia de {money_md(diferencia)} (dentro de la tolerancia); el revisor la verá resaltada.")

    if st.button("💾 Guardar gasto", type="primary", key="cmp_guardar"):
        subidos: list[str] = []
        sol_nueva_id = None
        try:
            if not sol.get("id"):  # viene del estado de cuenta: la solicitud se crea hasta guardar el gasto
                datos_sol = {**sol["_datos"], "category": None if categoria == "—" else categoria,
                             "material": None if material == "—" else material}
                sol_nueva_id = db.crear_solicitud(sb, cuenta["id"], sol["_periodo_id"], datos_sol)["id"]
            mes = str(sol["fecha"])[:7]
            payload = []
            for f in facturas:
                adjunto = None
                if f["adjunto"]:
                    pdf, info = f["adjunto"]
                    ruta = db.subir_adjunto(sb, cuenta["id"], mes, pdf)
                    subidos.append(ruta)
                    adjunto = {"ruta": ruta, **{k: info[k] for k in ("nombre_original", "bytes_original", "bytes_final", "sha256")}}
                payload.append(db.factura_a_json(f["datos"], adjunto))
            db.guardar_gasto(sb, sol.get("id") or sol_nueva_id, elegidos, None if categoria == "—" else categoria,
                             None if material == "—" else material, propina, payload)
        except db.ErrorNube as e:
            db.borrar_adjuntos_huerfanos(sb, subidos)
            if sol_nueva_id:
                try:
                    db.eliminar_solicitud(sb, sol_nueva_id)
                except db.ErrorNube:
                    pass
            st.error(str(e))
            return
        _limpiar_comprobacion()
        _flash("Gasto guardado. Cuando termines la solicitud, envíala a revisión.")
        st.rerun()


# ======================================================================
# Pestaña: Estado de cuenta
# ======================================================================
def _tab_estado_cuenta(sb, cuenta: dict, periodo: dict, cerrado: bool) -> None:
    todos = db.listar_movimientos(sb, cuenta["id"])
    with st.expander("⬆️ Cargar estado de cuenta", expanded=todos.empty):
        st.caption(f"Cuenta **{cuenta['nombre']}** · formato de lectura: {cuenta['formato']}. "
                   "Puedes subir un archivo acumulado: solo se agregan los movimientos nuevos.")
        archivo = st.file_uploader("Archivo (CSV o Excel)", type=["csv", "xlsx", "xls"], key="up_estado")
        if archivo is not None and st.button("Cargar movimientos", type="primary", key="btn_cargar_estado"):
            try:
                df = cargar_estado_cuenta(archivo.getvalue(), archivo.name, cuenta["formato"])
                r = db.subir_estado_cuenta(sb, cuenta["id"], df)
                _flash(f"{r['nuevos']} movimiento(s) nuevo(s); {r['ya_existian']} ya estaban cargados"
                       + (f"; {r['sin_fecha_o_monto']} omitido(s) por no tener fecha o monto válido" if r["sin_fecha_o_monto"] else "") + ".")
                st.rerun()
            except BankStatementError as e:
                st.error(f"No se pudo leer el estado de cuenta: {e}")
            except db.ErrorNube as e:
                st.error(str(e))
            except Exception as e:
                st.error(f"Archivo no reconocido: {e}")

    if not cerrado:
        with st.expander("📥 Importar avance anterior (.json)", expanded=False):
            _importador(sb, cuenta)

    if todos.empty:
        st.info("Todavía no hay movimientos. Carga el estado de cuenta arriba.")
        return
    mes = todos[todos["periodo_id"] == periodo["id"]]
    c = st.columns(4)
    for col, (est, titulo) in zip(c, (("pendiente", "Sin comprobar"), ("comprobado", "Comprobados"),
                                      ("no_necesario", "No necesarios"), ("en_espera", "Pendientes"))):
        sub = mes[mes["estado"] == est]
        col.markdown(estilos.tarjeta(f"{titulo} ({db.etiqueta_periodo(periodo)})", f"{len(sub)}", estilos.money_html(sub["monto"].abs().sum()), est), unsafe_allow_html=True)
    st.markdown(estilos.cuadre_html(reportes.cuadre(mes)), unsafe_allow_html=True)
    f1, f2 = st.columns([1, 2])
    estado = f1.selectbox("Mostrar", ["pendiente", "comprobado", "no_necesario", "en_espera", "todos"],
                          format_func=lambda k: "Todos" if k == "todos" else NOMBRE_MOV[k], key="filtro_estado_mov")
    alcance = f2.radio("Periodo", ["Solo el mes seleccionado", "Todos los meses"], horizontal=True, key="alcance_mov")
    vista = (mes if alcance.startswith("Solo") else todos)
    if estado != "todos":
        vista = vista[vista["estado"] == estado]
    vista = vista.reset_index(drop=True)
    mostrar = vista[["fecha", "descripcion", "monto", "saldo", "estado", "motivo_espera", "nota_revisor"]].assign(estado=vista["estado"].map(NOMBRE_MOV))
    ev = st.dataframe(mostrar, hide_index=True, use_container_width=True,
                      on_select="rerun", selection_mode="multi-row",
                      key=f"tabla_mov_{st.session_state.get('bit_ver', 0)}_{periodo['id']}_{estado}_{alcance}",
                      column_config={"fecha": "Fecha", "descripcion": "Descripción", "estado": "Estado", "motivo_espera": "Por qué está pendiente", "nota_revisor": "Nota del revisor",
                                     "monto": st.column_config.NumberColumn("Monto", format="$%.2f"), "saldo": st.column_config.NumberColumn("Saldo", format="$%.2f")})
    filas = [i for i in (ev.selection.rows if ev and ev.selection else []) if i < len(vista)]
    if filas and not cerrado:
        sel = vista.iloc[filas]
        estados_sel = set(sel["estado"])
        abiertos = estados_sel <= {"pendiente", "en_espera"}          # sin comprobar o pendiente
        mismo_mes = sel["periodo_id"].nunique() == 1
        b0, b1, b2 = st.columns([1.6, 1.4, 1.6])
        if b0.button("🔗 Comprobar seleccionados", type="primary", key="mov_cmp", disabled=not (abiertos and mismo_mes)):
            _comprobar_desde_estado(sb, cuenta, periodo, sel)
        if not abiertos:
            st.caption("Para comprobar, selecciona solo movimientos sin comprobar o pendientes.")
        elif not mismo_mes:
            st.caption("Para comprobar juntos, los movimientos deben ser del mismo mes.")
        else:
            st.caption("Se creará una solicitud nueva con estos movimientos y se abrirá la ventana de comprobación.")
        if b1.button("🚫 Marcar no necesario", key="mov_nn", disabled=not abiertos):
            _cambiar(sb, sel["id"].tolist(), "no_necesario")
        if b2.button("↩️ Regresar a sin comprobar", key="mov_pend", disabled=not (estados_sel <= {"no_necesario", "en_espera"})):
            _cambiar(sb, sel["id"].tolist(), "pendiente")
        if abiertos:
            with st.expander("⏳ Marcar como pendiente (devolución u otro caso que tardará más de 15 días)"):
                st.caption("Un movimiento pendiente no se ha perdido de vista: se sabe que tardará en comprobarse. Es obligatorio explicar por qué.")
                m1, m2 = st.columns([1, 2])
                motivo = m1.selectbox("Motivo", _catalogo(sb, "motivo_espera") or MOTIVOS_ESPERA_DEFAULT, key="esp_motivo")
                expl = m2.text_input("Explica por qué (obligatorio)", key="esp_expl", placeholder="Ej.: devolución de PayPal, la prometen en octubre")
                if st.button("⏳ Marcar como pendiente", key="mov_esp"):
                    if not expl.strip():
                        st.error("Explica por qué el movimiento queda pendiente.")
                    else:
                        texto = expl.strip() if motivo == "Otro" else f"{motivo}: {expl.strip()}"
                        if "esp_expl" in st.session_state:
                            del st.session_state["esp_expl"]
                        _cambiar(sb, sel["id"].tolist(), "en_espera", texto)


def _importador(sb, cuenta: dict) -> None:
    """Sube a la nube el avance (.json) de la app local: estado de cuenta, gastos comprobados con
    sus facturas y PDF. Primero muestra una vista previa; no escribe nada hasta confirmar."""
    ultimo = st.session_state.get("imp_reporte")
    if ultimo:
        partes = []
        if ultimo["movimientos_nuevos"]:
            partes.append(f"{ultimo['movimientos_nuevos']} movimiento(s) nuevo(s)")
        if ultimo["gastos_importados"]:
            partes.append(f"{ultimo['gastos_importados']} gasto(s) comprobado(s) con {ultimo['pdfs']} PDF")
        if ultimo["no_necesarios"]:
            partes.append(f"{ultimo['no_necesarios']} marcados como no necesarios")
        if ultimo.get("solicitudes"):
            partes.append(f"{ultimo['solicitudes']} solicitud(es) en borrador")
        st.success("Importación terminada: " + (", ".join(partes) if partes else "no había nada nuevo") + ".")
        for m in ultimo["omitidos"]:
            st.caption(f"↷ Omitido — {m}")
        for m in ultimo["errores"]:
            st.warning(m)
        if st.button("Cerrar este resumen", key="imp_cerrar_rep"):
            del st.session_state["imp_reporte"]
            st.rerun()
        return
    st.caption("Carga el avance que descargaste de la app anterior: el estado de cuenta, los gastos ya comprobados con sus facturas y los PDF, "
               "y las solicitudes de la bitácora que aún no se comprueban. Todo queda como solicitudes en **borrador** para que lo revises y lo envíes. "
               "Puedes repetirlo sin duplicar nada.")
    ver = st.session_state.setdefault("imp_v", 0)
    arch = st.file_uploader("Archivo de avance (.json)", type=["json"], key=f"imp_archivo_{ver}")
    if arch is None:
        return
    try:
        clave = (arch.name, arch.size, ver)
        if st.session_state.get("imp_clave") != clave:
            st.session_state.imp_plan = importar.planear(importar.leer_avance(arch.getvalue()))
            st.session_state.imp_clave = clave
        plan = st.session_state.imp_plan
        diag = importar.diagnostico(sb, cuenta["id"], plan)
    except importar.ErrorImportacion as e:
        st.error(str(e))
        return
    except db.ErrorNube as e:
        st.error(str(e))
        return
    except Exception as e:
        st.error(f"No se pudo leer el avance: {e}")
        return

    guardado = str(plan["guardado_en"] or "")[:16].replace("T", " ")
    if plan["fechas"]:
        f0, f1 = plan["fechas"]
        st.markdown(f"**Avance guardado el {guardado}** · formato {plan['banco'] or '—'} · movimientos del {f0} al {f1}")
    else:
        st.markdown(f"**Avance guardado el {guardado}** · sin estado de cuenta (solo solicitudes de la bitácora)")
    if plan["fechas_solicitudes"]:
        g0, g1 = plan["fechas_solicitudes"]
        st.caption(f"Solicitudes con fecha del {g0} al {g1}: se guardan en el mes de su propia fecha.")
    if plan["n_movimientos"]:
        c = st.columns(4)
        c[0].markdown(estilos.tarjeta("Movimientos", str(plan["n_movimientos"]), f"{diag['movimientos_nuevos']} nuevos · {diag['movimientos_existentes']} ya cargados", "acento"), unsafe_allow_html=True)
        c[1].markdown(estilos.tarjeta("Gastos comprobados", str(diag["gastos_listos"]), "se importarán", "comprobado"), unsafe_allow_html=True)
        c[2].markdown(estilos.tarjeta("Facturas", str(plan["n_facturas"]), f"{plan['n_pdfs']} con PDF · {plan['n_sin_pdf']} sin PDF", "acento"), unsafe_allow_html=True)
        c[3].markdown(estilos.tarjeta("No necesarios", str(len(plan["no_necesarios"])), "se marcarán", "no_necesario"), unsafe_allow_html=True)
    if plan["solicitudes"]:
        d = st.columns(2)
        d[0].markdown(estilos.tarjeta("Solicitudes sin comprobar", str(diag["solicitudes_nuevas"]), "se crearán en borrador", "pendiente"), unsafe_allow_html=True)
        d[1].markdown(estilos.tarjeta("Ya en la nube", str(diag["solicitudes_existentes"]), "no se duplican", "acento"), unsafe_allow_html=True)
        with st.expander("Ver las solicitudes del archivo"):
            st.dataframe(pd.DataFrame(plan["solicitudes"])[["fecha", "category", "employee_name", "description", "request_number", "number_of_days", "number_of_people", "monto"]]
                         .rename(columns={"fecha": "Fecha", "category": "Categoría", "employee_name": "Empleado", "description": "Descripción",
                                          "request_number": "Request", "number_of_days": "Días", "number_of_people": "Personas", "monto": "Monto"}),
                         hide_index=True, use_container_width=True)
        aplic = sorted({str(x["applicant_origen"]) for x in plan["solicitudes"] if x.get("applicant_origen")})
        if aplic:
            st.caption(f"En el archivo aparecían como aplicante: {', '.join(aplic)}. En la nube quedan a nombre de quien tiene la cuenta ({cuenta['nombre']}).")
    for a in plan["avisos"]:
        st.warning(a)
    if diag["gastos_ya_comprobados"]:
        st.info(f"{len(diag['gastos_ya_comprobados'])} gasto(s) ya están comprobados en la nube y se omitirán.")
    if diag["gastos_uuid_usado"]:
        st.warning(f"{len(diag['gastos_uuid_usado'])} gasto(s) usan una factura (UUID) que ya existe en otro gasto de la nube y no se importarán: "
                   + "; ".join(str(x) for x in diag["gastos_uuid_usado"]))
    confirma = True
    if plan["banco"] and cuenta.get("formato") and plan["banco"] != cuenta["formato"]:
        st.warning(f"El avance es del formato «{plan['banco']}» y esta cuenta ({cuenta['nombre']}) usa «{cuenta['formato']}». "
                   "Verifica que sea la cuenta correcta.")
        confirma = st.checkbox("Sí, es la cuenta correcta", key="imp_confirma_cuenta")
    nada = (diag["gastos_listos"] == 0 and diag["movimientos_nuevos"] == 0 and not plan["no_necesarios"]
            and diag["solicitudes_nuevas"] == 0)
    if nada:
        st.caption("No hay nada nuevo que importar.")
    if st.button("📥 Importar ahora", type="primary", key="imp_ejecutar", disabled=nada or not confirma):
        barra = st.progress(0.0, text="Preparando…")
        try:
            rep = importar.ejecutar(sb, cuenta, plan, lambda p, t: barra.progress(p, text=t))
        except db.ErrorNube as e:
            st.error(f"La importación se detuvo: {e}")
            return
        st.session_state.imp_reporte = rep
        _invalidar_catalogos()
        st.session_state.pop("_periodos", None)
        st.session_state.imp_v = ver + 1
        st.session_state.pop("imp_clave", None)
        st.rerun()


def _comprobar_desde_estado(sb, cuenta: dict, periodo: dict, sel: pd.DataFrame) -> None:
    """Abre directamente la comprobación de los movimientos elegidos. La solicitud se crea
    (con estos datos) hasta que se guarda el gasto, para no dejar borradores vacíos."""
    fecha = pd.to_datetime(sel["fecha"]).min().date()
    desc = " / ".join(dict.fromkeys(sel["descripcion"].astype(str)))[:200]
    monto = round(float(sel["monto"].abs().sum()), 2)
    _limpiar_comprobacion()
    st.session_state.comprobando = {
        "id": None, "numero": None, "fecha": str(fecha), "monto_tentativo": monto, "description": desc,
        "category": None, "material": None, "_periodo_id": sel["periodo_id"].iloc[0],
        "_datos": {"fecha": fecha, "monto": monto, "description": desc}}
    st.session_state.cmp_movs = sel["id"].tolist()
    st.rerun()


def _cambiar(sb, ids: list[str], estado: str, motivo: str | None = None) -> None:
    try:
        db.cambiar_estado_movimientos(sb, ids, estado, motivo)
        _nueva_tabla()
        st.rerun()
    except db.ErrorNube as e:
        st.error(str(e))


# ======================================================================
# Pestañas: Pendientes devueltos y Lotes
# ======================================================================
def _tab_pendientes(sb, cuenta: dict) -> None:
    pend = db.listar_pendientes_abiertos(sb, cuenta["id"])
    if not pend:
        st.success("No tienes gastos con pendientes. 🎉")
        return
    st.caption("El revisor marcó estos gastos con un pendiente. Atiéndelos, comenta y márcalos como resueltos; después vuelve a enviar la solicitud a revisión.")
    for p in pend:
        with st.container(border=True):
            st.markdown(f"**Solicitud #{p['solicitud_numero']}** · {p.get('solicitud_descripcion') or ''} · gasto {money_md(p['monto_estado'])}")
            st.markdown(f"🚩 **{p['motivo']}** · {p['estado']}")
            for c in p["comentarios"]:
                st.caption(f"💬 **{c['autor']}** ({str(c['creado_en'])[:16].replace('T', ' ')} UTC): {c['texto']}")
            txt = st.text_input("Agregar comentario", key=f"com_{p['id']}")
            b1, b2, _ = st.columns([1, 1.3, 3])
            if b1.button("Comentar", key=f"comb_{p['id']}"):
                try:
                    db.agregar_comentario(sb, cuenta["id"], p["id"], txt)
                    st.rerun()
                except db.ErrorNube as e:
                    st.error(str(e))
            if b2.button("✅ Marcar resuelto", key=f"res_{p['id']}"):
                try:
                    db.resolver_pendiente(sb, p["id"], txt or None)
                    _flash("Pendiente resuelto.")
                    st.rerun()
                except db.ErrorNube as e:
                    st.error(str(e))


def _tab_lotes(sb, cuenta: dict, periodos: list[dict]) -> None:
    lotes = db.listar_lotes(sb, cuenta["id"])
    if not lotes:
        st.info("Todavía no hay lotes de reembolso para tu cuenta.")
        return
    etiquetas = {p["id"]: db.etiqueta_periodo(p) for p in periodos}
    nombres = {"por_pagar": "⏳ Por pagar", "pagado": "✅ Pagado", "anulado": "↩️ Anulado"}
    df = pd.DataFrame([{
        "Lote": f"{etiquetas.get(l['periodo_id'], '')}-{l['numero']}", "Cerrado": str(l["cerrado_en"])[:10], "Monto": float(l["monto"]),
        "Gastos": l["num_gastos"], "Estado": nombres.get(l["estado"], l["estado"]), "Fecha de pago": l.get("fecha_pago") or "",
        "Referencia": l.get("referencia_pago") or ""} for l in lotes])
    st.dataframe(df, hide_index=True, use_container_width=True, column_config={"Monto": st.column_config.NumberColumn(format="$%.2f")})
    pagado = sum(float(l["monto"]) for l in lotes if l["estado"] == "pagado")
    por_pagar = sum(float(l["monto"]) for l in lotes if l["estado"] == "por_pagar")
    c1, c2 = st.columns(2)
    c1.metric("Pagado", money(pagado))
    c2.metric("Por pagar", money(por_pagar))


# ======================================================================
# Pestaña: Enviados a comprobación (todo el estado de cuenta con su avance)
# ======================================================================
ICONO_MOV = {"comprobado": "🟢", "no_necesario": "⚪", "pendiente": "🟠", "en_espera": "🟣"}
NOMBRE_MOV = {"comprobado": "Comprobado", "no_necesario": "No necesario", "pendiente": "Sin comprobar", "en_espera": "Pendiente"}
NOMBRE_LOTE = {"por_pagar": "⏳ Por pagar", "pagado": "✅ Pagado", "anulado": "↩️ Anulado"}
POR_PAGINA = 25


def _fecha_cdmx(ts: str) -> str:
    t = pd.to_datetime(ts, utc=True).tz_convert("America/Mexico_City")
    return t.strftime("%Y-%m-%d %H:%M")


def _tab_enviados(sb, cuenta: dict, periodo: dict, cerrado: bool) -> None:
    df = db.listar_movimientos_revision(sb, cuenta["id"], periodo["id"])
    if df.empty:
        st.info("Este mes todavía no tiene movimientos. Carga el estado de cuenta en la pestaña «Estado de cuenta».")
        return
    movs = df.drop_duplicates("id")
    cu = reportes.cuadre(movs)
    cols = st.columns(4)
    for col, (est, titulo) in zip(cols, (("comprobado", "Comprobados"), ("no_necesario", "No necesarios"),
                                         ("pendiente", "Sin comprobar"), ("en_espera", "Pendientes"))):
        col.markdown(estilos.tarjeta(titulo, str(cu["por_estado"][est]["n"]), estilos.money_html(cu["por_estado"][est]["monto"]), est), unsafe_allow_html=True)
    p = cu["por_estado"]
    st.markdown(estilos.barra_progreso(p["comprobado"]["n"], p["no_necesario"]["n"], p["pendiente"]["n"], p["en_espera"]["n"]), unsafe_allow_html=True)
    st.markdown(estilos.cuadre_html(cu), unsafe_allow_html=True)

    # ---- Datos del mes (una sola carga para todo lo que sigue) ----
    sols = db.listar_solicitudes(sb, cuenta["id"], periodo["id"])
    gastos = db.listar_gastos(sb, [x["id"] for x in sols]) if sols else []
    con_pend = db.solicitudes_con_pendiente_abierto(sb, cuenta["id"])
    resumen = db.listar_envios_resumen(sb, cuenta["id"], periodo["id"])
    env_g = db.listar_envio_gastos(sb, cuenta["id"], periodo["id"])
    por_enviar = reportes.filas_por_enviar(sols, gastos, con_pend)

    # ---- Lote por enviar ----
    st.markdown("##### 📦 Lote por enviar")
    n_lote = len(resumen) + 1
    with st.container(border=True):
        if por_enviar.empty:
            st.markdown("**No hay gastos comprobados nuevos por enviar.**")
            st.caption("Comprueba gastos en la Bitácora o en el Estado de cuenta; aquí aparecerán para que los revises antes de mandarlos. "
                       "Los gastos que el revisor te devuelva vuelven a entrar en el siguiente lote cuando atiendas el pendiente.")
        else:
            total = float(por_enviar["Monto"].sum())
            k = st.columns(3)
            k[0].markdown(estilos.tarjeta(f"Lote #{n_lote} por enviar", str(len(por_enviar)), "gasto(s) comprobado(s)", "acento"), unsafe_allow_html=True)
            k[1].markdown(estilos.tarjeta("Solicitudes", str(por_enviar["Solicitud"].nunique()), "incluidas en el lote", "acento"), unsafe_allow_html=True)
            k[2].markdown(estilos.tarjeta("Total del lote", estilos.money_html(total), "monto de los cargos", "rojo"), unsafe_allow_html=True)
            st.dataframe(por_enviar, hide_index=True, use_container_width=True,
                         column_config={"Monto": st.column_config.NumberColumn("Monto", format="$%.2f"),
                                        "Diferencia": st.column_config.NumberColumn("Diferencia", format="$%.2f")})
            st.caption("Revisa la tabla. Al mandar a revisión el lote queda registrado con su fecha y su total; no se puede editar, solo el revisor puede devolver un gasto.")
            nota = st.text_input("Nota para el revisor (opcional)", key="env_nota", disabled=cerrado)
            if st.button(f"📤 Mandar a revisión · Lote #{n_lote} · {money_md(total)}", type="primary", key="btn_enviar_ec", disabled=cerrado):
                try:
                    db.enviar_estado_cuenta(sb, periodo["id"], nota)
                    if "env_nota" in st.session_state:
                        del st.session_state["env_nota"]
                    st.session_state.pop("xl_mes", None)
                    _flash(f"Lote #{n_lote} enviado a revisión.")
                    st.rerun()
                except db.ErrorNube as e:
                    st.error(str(e))
        # Excel del mes (siempre disponible)
        xl = st.session_state.get("xl_mes")
        if xl and xl["clave"] != (cuenta["id"], periodo["id"]):
            xl = None
        x1, x2 = st.columns([1.3, 3])
        if x1.button("📊 Preparar Excel del mes", key="xl_prep"):
            perfil = st.session_state.get("perfil") or {}
            st.session_state.xl_mes = xl = {
                "clave": (cuenta["id"], periodo["id"]), "hora": datetime.datetime.now().strftime("%H:%M"),
                "bytes": reportes.excel_mes(cuenta["nombre"], db.etiqueta_periodo(periodo), perfil.get("nombre") or "", movs, sols, gastos, env_g, resumen)}
        if xl:
            x2.download_button(f"⬇️ Descargar Excel ({db.etiqueta_periodo(periodo)}, generado {xl['hora']})", data=xl["bytes"],
                               file_name=f"Comprobaciones_{cuenta['nombre'].replace(' ', '_')}_{db.etiqueta_periodo(periodo)}.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="xl_desc")
        else:
            x2.caption("Incluye todo el mes con una columna «Lote enviado a revisión #N – fecha», el monto revisado y aprobado para pago y su fecha de pago.")

    # ---- Lotes enviados ----
    st.markdown("##### 🗂️ Lotes enviados a revisión")
    if not resumen:
        st.caption("Aún no has enviado ningún lote este mes.")
    else:
        tabla = pd.DataFrame([{
            "Lote": f"#{e['numero']}", "Enviado": _fecha_cdmx(e["enviado_en"]), "Gastos": e["gastos_enviados"],
            "Monto enviado": float(e["monto_enviado"]), "Revisado y aprobado para pago": float(e["monto_aprobado"]),
            "Devuelto": float(e["monto_devuelto"]), "En revisión": float(e["monto_en_revision"]), "Pagado": float(e["monto_pagado"]),
            "Fecha de pago": (e["primer_pago"] if e["primer_pago"] == e["ultimo_pago"] else f"{e['primer_pago']} a {e['ultimo_pago']}") if e.get("ultimo_pago") else "—",
        } for e in resumen])
        dinero = {c: st.column_config.NumberColumn(c, format="$%.2f") for c in ("Monto enviado", "Revisado y aprobado para pago", "Devuelto", "En revisión", "Pagado")}
        st.dataframe(tabla, hide_index=True, use_container_width=True, column_config=dinero)
        enviado = sum(float(e["monto_enviado"]) for e in resumen)
        aprobado = sum(float(e["monto_aprobado"]) for e in resumen)
        st.caption(f"Total enviado {money_md(enviado)} · revisado y aprobado para pago {money_md(aprobado)}. "
                   "El monto enviado nunca cambia; si el revisor devuelve un gasto, la diferencia se ve como «Devuelto» o «En revisión».")
        num_sol = {x["id"]: x for x in sols}
        for e in reversed(resumen):
            with st.expander(f"Lote #{e['numero']} · {_fecha_cdmx(e['enviado_en'])} · {e['gastos_enviados']} gasto(s) · {money_md(e['monto_enviado'])}"
                             f" → aprobado {money_md(e['monto_aprobado'])}"):
                if e.get("nota"):
                    st.caption(f"Nota: {e['nota']}")
                det = [{"Solicitud": int(num_sol[r["solicitud_id"]]["numero"]) if r["solicitud_id"] in num_sol else None,
                        "Descripción": (num_sol.get(r["solicitud_id"]) or {}).get("description") or "",
                        "Monto enviado": float(r["monto_enviado"]), "Resultado": reportes.NOMBRE_RESULTADO.get(r["resultado"], r["resultado"]),
                        "Aprobado para pago": float(r["monto_aprobado"]) if r.get("monto_aprobado") is not None else None,
                        "Lote de pago": (f"Lote {int(r['lote_numero'])}" + (" · pagado" if r.get("lote_estado") == "pagado" else " · por pagar")) if r.get("lote_numero") is not None else "",
                        "Fecha de pago": r.get("fecha_pago") if r.get("lote_estado") == "pagado" else ""}
                       for r in env_g if r["envio_id"] == e["envio_id"]]
                st.dataframe(pd.DataFrame(det), hide_index=True, use_container_width=True,
                             column_config={"Monto enviado": st.column_config.NumberColumn(format="$%.2f"),
                                            "Aprobado para pago": st.column_config.NumberColumn(format="$%.2f")})

    # ---- Desglose (una fila por solicitud comprobada; los demás movimientos, uno por fila) ----
    st.markdown("##### Desglose del estado de cuenta")
    filas_d = reportes.filas_desglose(df, gastos)
    f1, f2 = st.columns([1.2, 3])
    filtro = f1.selectbox("Estado", ["Todos", *reportes.ETIQUETA_REGISTRO.values()], key="env_filtro")
    texto = f2.text_input("🔎 Buscar (descripción, concepto, #)", key="env_busca")
    vista_d = filas_d
    if filtro != "Todos":
        clave = {v: k for k, v in reportes.ETIQUETA_REGISTRO.items()}[filtro]
        vista_d = [r for r in vista_d if r["estado"] == clave]
    if texto.strip():
        t = texto.strip().lower()
        vista_d = [r for r in vista_d if t in " ".join(str(r[k]) for k in ("descripcion", "concepto", "numero", "situacion")).lower()]
    n_sol = sum(1 for r in filas_d if r["tipo"] == "solicitud")
    n_mov_sol = sum(r["movs"] for r in filas_d if r["tipo"] == "solicitud")
    st.caption(f"{len(filas_d)} filas: {n_sol} solicitud(es) comprobada(s) que agrupan {n_mov_sol} movimientos, más "
               f"{len(filas_d) - n_sol} movimiento(s) sin comprobar, pendientes o no necesarios. Haz clic en una fila para ver el detalle.")
    if not vista_d:
        st.caption("Ningún registro coincide con el filtro.")
        return
    tabla_d = pd.DataFrame([{
        "Estado": reportes.ETIQUETA_REGISTRO[r["estado"]], "Fecha": r["fecha"], "Sol. #": r["numero"], "Descripción": r["descripcion"],
        "Concepto": r["concepto"], "Mov.": r["movs"], "Monto": r["monto"], "Diferencia": r["diferencia"] if r["tipo"] == "solicitud" else None,
        "Situación": r["situacion"]} for r in vista_d])
    tabla_d["Sol. #"] = tabla_d["Sol. #"].astype("Int64")
    inv = {v: k for k, v in reportes.ETIQUETA_REGISTRO.items()}

    def _pintar_d(col):
        return [f"background-color:{ESTILO_ETIQUETA[inv[v]][0]};color:{ESTILO_ETIQUETA[inv[v]][1]};font-weight:700;text-align:center" for v in col]

    def _pintar_dif(col):
        return [f"background-color:{estilos.C_DIFERENCIA_AMARILLO};font-weight:700" if r["tiene_diferencia"] else "" for r in vista_d]

    estilo_d = (tabla_d.style.apply(_pintar_d, subset=["Estado"]).apply(_pintar_dif, subset=["Diferencia"])
                .format({"Monto": "${:,.2f}", "Diferencia": "${:,.2f}"}, na_rep="—"))
    ev = st.dataframe(estilo_d, hide_index=True, use_container_width=True, height=min(36 * (len(tabla_d) + 1) + 4, 420),
                      on_select="rerun", selection_mode="single-row",
                      key=f"tabla_env_{st.session_state.get('bit_ver', 0)}_{filtro}_{texto}",
                      column_config={"Sol. #": st.column_config.NumberColumn("Sol. #", format="%d", width="small"),
                                     "Estado": st.column_config.TextColumn("Estado", width="small"),
                                     "Mov.": st.column_config.NumberColumn("Mov.", format="%d", width="small", help="Movimientos del banco que forman la comprobación")})
    sel = [i for i in (ev.selection.rows if ev and ev.selection else []) if i < len(vista_d)]
    if not sel:
        return
    r = vista_d[sel[0]]
    gastos_por_id = {g["id"]: g for g in gastos}
    if r["tipo"] == "movimiento":
        fila = df[df["id"] == r["id"]]
        if fila.empty:
            st.warning("El movimiento ya no está en la lista; vuelve a seleccionar.")
            return
        with st.container(border=True):
            _detalle_movimiento(sb, df, fila.index[0], fila.iloc[0], None, cuenta, periodo, cerrado)
    else:
        _detalle_solicitud_enviada(df, r, gastos_por_id)


def _detalle_solicitud_enviada(df: pd.DataFrame, r: dict, gastos_por_id: dict) -> None:
    """Detalle de una comprobación (solicitud) con todos sus cargos y facturas."""
    sub = df[df["solicitud_id"] == r["id"]]
    if sub.empty:
        st.warning("La solicitud ya no está en la lista; vuelve a seleccionar.")
        return
    r0 = sub.iloc[0]
    movs = sub.drop_duplicates("id")
    with st.container(border=True):
        st.markdown(f"{_tag_html('comprobado')} &nbsp; **🧾 Solicitud #{r['numero']}** · {r['situacion']}", unsafe_allow_html=True)
        a, b, c = st.columns(3)
        a.write(f"**Empleado:** {r0.get('employee_name') or '—'}  \n**Request:** {r0.get('request_number') or '—'}")
        a.caption(str(r0.get("solicitud_descripcion") or ""))
        b.write(f"**Categoría:** {r0.get('categoria') or '—'}  \n**Material:** {r0.get('material') or '—'}")
        gs = [gastos_por_id[g] for g in dict.fromkeys(sub["gasto_id"].dropna()) if g in gastos_por_id]
        facturado = float(pd.to_numeric(sub.drop_duplicates("gasto_id")["suma_facturas"], errors="coerce").fillna(0).sum())
        c.write(f"**Total de cargos:** {money_md(r['monto'])}  \n**Facturas:** {money_md(facturado)}")
        c.markdown(("🟡 " if r["tiene_diferencia"] else "") + f"Diferencia: **{money_md(r['diferencia'])}**")
        st.markdown(f"**🏦 Movimientos del banco que forman esta comprobación ({len(movs)})**")
        st.dataframe(pd.DataFrame({"Fecha": movs["fecha"], "Descripción": movs["descripcion"], "Monto": movs["monto"].abs()}),
                     hide_index=True, use_container_width=True,
                     column_config={"Monto": st.column_config.NumberColumn("Monto", format="$%.2f")})
        for g in gs:
            for f in g.get("facturas") or []:
                cols = st.columns([3.2, 1.2, 1])
                cols[0].caption(f"🧾 {f.get('uuid_cfdi') or 'SIN-UUID'} · {f.get('rfc_emisor') or ''} · {f.get('nombre_emisor') or ''} · {f.get('fecha_factura') or ''}")
                cols[1].caption(money_md(f["monto_aplicado"]))
                if f.get("adjunto") and cols[2].button("📎 Ver", key=f"venv_{g['id']}_{f['id']}"):
                    st.session_state.adj_ver = f["adjunto"]["ruta"]


def _tag_html(estado: str) -> str:
    fondo, texto = ESTILO_ETIQUETA[estado]
    return (f'<span style="background:{fondo};color:{texto};border:1px solid {"#CBD5E1" if estado == "no_necesario" else texto};'
            f'border-radius:999px;padding:2px 12px;font-weight:700">{reportes.ETIQUETA_REGISTRO[estado]}</span>')


def _detalle_movimiento(sb, df: pd.DataFrame, idx, r, g: dict | None, cuenta: dict, periodo: dict, cerrado: bool) -> None:
    a, b, c = st.columns(3)
    a.markdown("**🏦 Movimiento**")
    a.write(f"{r['fecha']}  \n{r['descripcion']}")
    a.write(f"Monto: {money_md(r['monto'])}" + (f"  \nSaldo: {money_md(r['saldo'])}" if pd.notna(r["saldo"]) else ""))
    if r["estado"] == "no_necesario":
        b.markdown("**Estado**")
        b.write("Marcado como no necesario: no requiere comprobación.")
        return
    if r["estado"] == "en_espera":
        b.markdown("**Estado: pendiente**")
        b.write(str(r.get("motivo_espera") or "—"))
        if not cerrado and b.button("🔗 Comprobar este movimiento", key=f"mvcmp_{r['id']}"):
            _nueva_tabla()
            _comprobar_desde_estado(sb, cuenta, periodo, df.loc[[idx]])
        return
    if r["estado"] == "pendiente":
        b.markdown("**Estado**")
        b.write("Aún sin comprobar.")
        if not cerrado and b.button("🔗 Comprobar este movimiento", key=f"mvcmp_{r['id']}"):
            _nueva_tabla()
            _comprobar_desde_estado(sb, cuenta, periodo, df.loc[[idx]])
        return
    # comprobado
    b.markdown(f"**🧾 Solicitud #{int(r['solicitud_numero'])}**")
    b.write(ESTADOS_SOLICITUD.get(r["solicitud_estado"], r["solicitud_estado"]))
    extra = [x for x in (r.get("employee_name"), r.get("request_number")) if x]
    if extra:
        b.caption(" · ".join(str(x) for x in extra))
    if r.get("solicitud_descripcion"):
        b.caption(str(r["solicitud_descripcion"]))
    c.markdown("**💵 Gasto**")
    c.write(f"{r.get('categoria') or '—'}" + (f" · {r['material']}" if r.get("material") else ""))
    c.write(f"Cargo: {money_md(r['monto_estado'])}  \nFacturas: {money_md(r['suma_facturas'])}"
            + (f"  \nPropina: {money_md(r['propina'])}" if float(r["propina"] or 0) else ""))
    dif = float(r["diferencia"] or 0)
    c.markdown(("🟡 " if r["tiene_diferencia"] else "") + f"Diferencia: **{money_md(dif)}**")
    if pd.notna(r["lote_numero"]):
        pago = f" · pagado el {r['fecha_pago']} (ref. {r['referencia_pago']})" if r["lote_estado"] == "pagado" else ""
        st.info(f"🔒 Lote {int(r['lote_numero'])} · {NOMBRE_LOTE.get(r['lote_estado'], r['lote_estado'])}{pago}")
    if g:
        if len(g["movimientos"]) > 1:
            st.caption(f"Este gasto cubre {len(g['movimientos'])} movimientos: "
                       + "; ".join(f"{m['fecha']} {m['descripcion']} {money_md(m['monto'])}" for m in g["movimientos"]))
        for f in g["facturas"]:
            cols = st.columns([3.2, 1.2, 1])
            cols[0].caption(f"🧾 {f.get('uuid_cfdi') or 'SIN-UUID'} · {f.get('rfc_emisor') or ''} · {f.get('nombre_emisor') or ''} · {f.get('fecha_factura') or ''}")
            cols[1].caption(money_md(f["monto_aplicado"]))
            if f.get("adjunto") and cols[2].button("📎 Ver", key=f"venv_{g['id']}_{f['id']}"):
                st.session_state.adj_ver = f["adjunto"]["ruta"]


# ======================================================================
# Revisor (Etapa 2): cola de revisión, visto bueno, lotes y pagos, cierre de mes
# ======================================================================
NOMBRE_SOL_REV = {"enviado_revision": "Por revisar", "con_pendientes": "Devuelta", "en_lote": "En lote · por pagar",
                  "pagado": "Pagada", "borrador": "Borrador"}
ESTILO_SOL_REV = {"enviado_revision": ("#DBEAFE", "#1E40AF"), "con_pendientes": ("#FEE2E2", "#B91C1C"),
                  "en_lote": ("#FEF3C7", "#92400E"), "pagado": ("#DCFCE7", "#166534"), "borrador": ("#F1F5F9", "#475569")}
FILTROS_COLA = {"Por revisar": "enviado_revision", "Devueltas": "con_pendientes", "En lote": "en_lote", "Pagadas": "pagado",
                "Borrador (aplicante)": "borrador"}


def _nueva_tabla_rev() -> None:
    st.session_state["rev_ver"] = st.session_state.get("rev_ver", 0) + 1
    st.session_state.pop("vb_confirm", None)


def _vista_revisor(sb) -> None:
    hoy = hoy_cdmx()
    if st.session_state.get("_periodos_dia") != hoy.isoformat() or "_periodos" not in st.session_state:
        db.obtener_o_crear_periodo(sb, hoy.year, hoy.month)
        st.session_state._periodos = db.listar_periodos(sb)
        st.session_state._periodos_dia = hoy.isoformat()
    if "_cuentas" not in st.session_state:
        st.session_state._cuentas = db.listar_cuentas(sb)
    periodos, cuentas = st.session_state._periodos, st.session_state._cuentas
    if not cuentas:
        st.warning("No hay cuentas configuradas.")
        return
    actual = f"{hoy.year}-{hoy.month:02d}"
    etiquetas = [db.etiqueta_periodo(p) for p in periodos]
    nombres = [c["nombre"] for c in cuentas]
    with st.sidebar:
        st.markdown('<div class="sidebar-header"><div class="sidebar-header-icon">🧮</div><span>Revisor</span></div>', unsafe_allow_html=True)
        cuenta = cuentas[nombres.index(st.selectbox("Cuenta", nombres, key="rev_cuenta"))]
        sel = st.selectbox("Mes", etiquetas, index=etiquetas.index(actual) if actual in etiquetas else 0, key="rev_mes")
        periodo = periodos[etiquetas.index(sel)]
        if cuenta.get("limite_reembolso"):
            st.caption(f"Límite de reembolso: {money_md(cuenta['limite_reembolso'])}")

    secciones = ["📥 Cola de revisión", "🏦 Estado de cuenta", "💸 Lotes y pagos", "📊 Resumen y cierre de mes"]
    elegida = st.segmented_control("Sección", secciones, default=secciones[0], label_visibility="collapsed", key="rev_seccion")
    vista = elegida or st.session_state.get("_rev_seccion_ult", secciones[0])
    st.session_state["_rev_seccion_ult"] = vista
    st.divider()
    if vista == secciones[0]:
        _cola_revision(sb, cuenta, periodo)
    elif vista == secciones[1]:
        _estado_cuenta_revisor(sb, cuenta, periodo)
    elif vista == secciones[2]:
        _lotes_revisor(sb, cuenta, cuentas, periodos)
    else:
        _resumen_revisor(sb, cuentas, periodo)
    _visor_adjunto(sb)


def _cola_revision(sb, cuenta: dict, periodo: dict) -> None:
    df = db.listar_movimientos_revision(sb, cuenta["id"], periodo["id"])
    if df.empty:
        st.info(f"{cuenta['nombre']} no tiene movimientos en {db.etiqueta_periodo(periodo)}.")
        return
    sols = db.listar_solicitudes(sb, cuenta["id"], periodo["id"])
    gastos = db.listar_gastos(sb, [x["id"] for x in sols]) if sols else []
    env_g = db.listar_envio_gastos(sb, cuenta["id"], periodo["id"])
    pend = db.listar_pendientes_abiertos(sb, cuenta["id"])
    tol = st.session_state.get("tol", 0.50)
    envio_de: dict[str, int] = {}
    for e in env_g:
        envio_de[e["solicitud_id"]] = max(envio_de.get(e["solicitud_id"], 0), int(e["envio_numero"]))
    filas = [{**f, "envio": envio_de.get(f["id"])} for f in reportes.filas_desglose(df, gastos) if f["tipo"] == "solicitud"]
    # Solicitudes abiertas (borrador / sin gasto) no tienen movimiento ligado y no aparecen aquí.

    # ---- Tarjetas ----
    def _suma(estado):
        sub = [f for f in filas if f["solicitud_estado"] == estado]
        return len(sub), sum(f["monto"] for f in sub)
    cols = st.columns(4)
    for col, (estado, titulo, tono) in zip(cols, (("enviado_revision", "Por revisar", "acento"), ("con_pendientes", "Devueltas al aplicante", "rojo"),
                                                  ("en_lote", "En lote · por pagar", "alerta"), ("pagado", "Pagadas", "comprobado"))):
        n, m = _suma(estado)
        col.markdown(estilos.tarjeta(titulo, str(n), estilos.money_html(m), tono), unsafe_allow_html=True)
    cu = reportes.cuadre(df.drop_duplicates("id"))
    st.markdown(estilos.cuadre_html(cu), unsafe_allow_html=True)

    f1, f2 = st.columns([1.4, 3])
    filtro = f1.selectbox("Mostrar", [*FILTROS_COLA, "Todas"], key="rev_filtro")
    texto = f2.text_input("🔎 Buscar (descripción, concepto, empleado, #)", key="rev_busca")
    vista = filas
    if filtro != "Todas":
        vista = [f for f in vista if f["solicitud_estado"] == FILTROS_COLA[filtro]]
    if texto.strip():
        t = texto.strip().lower()
        vista = [f for f in vista if t in " ".join(str(f[k]) for k in ("descripcion", "concepto", "numero")).lower()]

    limpias = [f for f in filas if f["solicitud_estado"] == "enviado_revision" and abs(f["diferencia"]) <= tol
               and f["numero"] not in {p["solicitud_numero"] for p in pend}]
    if limpias:
        if st.button(f"⚡ Dar visto bueno a todas las por revisar sin diferencia ({len(limpias)} · {money_md(sum(f['monto'] for f in limpias))})",
                     key="vb_todas"):
            st.session_state["vb_confirm"] = tuple(f["id"] for f in limpias)

    if not vista:
        st.caption("No hay solicitudes con este filtro." if filas else "Todavía no hay solicitudes comprobadas en este mes.")
        _confirmar_visto_bueno(sb, cuenta, periodo, {f["id"]: f for f in filas})
        return
    tabla = pd.DataFrame([{
        "Estado": NOMBRE_SOL_REV.get(f["solicitud_estado"], f["solicitud_estado"]), "Sol. #": f["numero"], "Fecha": f["fecha"],
        "Descripción": f["descripcion"], "Concepto": f["concepto"], "Mov.": f["movs"], "Monto": f["monto"], "Diferencia": f["diferencia"],
        "Enviada en lote": f"#{f['envio']}" if f["envio"] else "—"} for f in vista])

    def _pintar(col):
        return [f"background-color:{ESTILO_SOL_REV[f['solicitud_estado']][0]};color:{ESTILO_SOL_REV[f['solicitud_estado']][1]};"
                "font-weight:700;text-align:center" for f in vista]

    def _pintar_dif(col):
        return [f"background-color:{estilos.C_DIFERENCIA_AMARILLO};font-weight:700" if f["tiene_diferencia"] else "" for f in vista]

    estilo = (tabla.style.apply(_pintar, subset=["Estado"]).apply(_pintar_dif, subset=["Diferencia"])
              .format({"Monto": "${:,.2f}", "Diferencia": "${:,.2f}"}))
    st.caption("Selecciona una o varias filas: con una ves el detalle y puedes devolverla; con varias das el visto bueno y se crea el lote de pago.")
    ev = st.dataframe(estilo, hide_index=True, use_container_width=True, height=min(36 * (len(tabla) + 1) + 4, 420),
                      on_select="rerun", selection_mode="multi-row",
                      key=f"tabla_rev_{st.session_state.get('rev_ver', 0)}_{cuenta['id']}_{periodo['id']}_{filtro}_{texto}",
                      column_config={"Sol. #": st.column_config.NumberColumn("Sol. #", format="%d", width="small"),
                                     "Estado": st.column_config.TextColumn("Estado", width="small"),
                                     "Mov.": st.column_config.NumberColumn("Mov.", format="%d", width="small")})
    sel = [i for i in (ev.selection.rows if ev and ev.selection else []) if i < len(vista)]
    por_id = {f["id"]: f for f in filas}
    if sel:
        elegidas = [vista[i] for i in sel]
        revisables = [f for f in elegidas if f["solicitud_estado"] == "enviado_revision"]
        total = sum(f["monto"] for f in elegidas)
        st.markdown(f"**{len(elegidas)} solicitud(es) seleccionada(s)** · {money_md(total)}")
        if len(revisables) == len(elegidas):
            malas = [f for f in elegidas if abs(f["diferencia"]) > tol]
            if malas:
                st.warning("Con diferencia mayor a la tolerancia: " + ", ".join(f"#{f['numero']}" for f in malas)
                           + ". El sistema no deja darles visto bueno; devuélvelas al aplicante.")
            if st.button(f"✅ Dar visto bueno y crear lote de pago ({len(elegidas)} · {money_md(total)})", type="primary", key="vb_sel"):
                st.session_state["vb_confirm"] = tuple(f["id"] for f in elegidas)
        else:
            st.caption("El visto bueno solo aplica a solicitudes «Por revisar»; quita de la selección las que ya están devueltas, en lote o pagadas.")
    _confirmar_visto_bueno(sb, cuenta, periodo, por_id)
    if len(sel) == 1:
        _detalle_revision(sb, cuenta, vista[sel[0]], df, gastos, pend, tol)


def _confirmar_visto_bueno(sb, cuenta: dict, periodo: dict, por_id: dict) -> None:
    ids = st.session_state.get("vb_confirm")
    if not ids:
        return
    total = sum(por_id[i]["monto"] for i in ids if i in por_id)
    with st.container(border=True):
        st.warning(f"**Confirmar visto bueno** · {len(ids)} solicitud(es) de {cuenta['nombre']} · {money_md(total)}. "
                   "Se crea el lote de pago y las solicitudes quedan **bloqueadas** (solo tú puedes desbloquear el lote mientras no esté pagado).")
        c1, c2, _ = st.columns([1.3, 1, 3])
        if c1.button("✅ Sí, cerrar lote", type="primary", key="vb_ok"):
            try:
                lote_id = db.cerrar_lote(sb, cuenta["id"], periodo["id"], list(ids))
                _nueva_tabla_rev()
                st.session_state.pop("_lotes_sel", None)
                _flash(f"Lote cerrado por {money_md(total)}. Ya está en «Lotes y pagos» para registrar su pago.")
                st.rerun()
            except db.ErrorNube as e:
                st.error(str(e))
        if c2.button("Cancelar", key="vb_no"):
            st.session_state.pop("vb_confirm", None)
            st.rerun()


def _detalle_revision(sb, cuenta: dict, r: dict, df: pd.DataFrame, gastos: list[dict], pend: list[dict], tol: float) -> None:
    gastos_por_id = {g["id"]: g for g in gastos}
    _detalle_solicitud_enviada(df, r, gastos_por_id)
    mias = [p for p in pend if p.get("solicitud_numero") == r["numero"]]
    for p in mias:
        with st.container(border=True):
            st.markdown(f"🚩 **{p['motivo']}** · {p['estado']} · gasto {money_md(p['monto_estado'])}")
            for c in p["comentarios"]:
                st.caption(f"💬 **{c['autor']}** ({str(c['creado_en'])[:16].replace('T', ' ')} UTC): {c['texto']}")
            txt = st.text_input("Comentario", key=f"rcom_{p['id']}")
            b1, b2, _ = st.columns([1, 1.6, 3])
            if b1.button("Comentar", key=f"rcomb_{p['id']}"):
                try:
                    db.agregar_comentario(sb, cuenta["id"], p["id"], txt)
                    st.rerun()
                except db.ErrorNube as e:
                    st.error(str(e))
            if b2.button("✅ Dar por resuelto", key=f"rres_{p['id']}"):
                try:
                    db.resolver_pendiente(sb, p["id"], txt or None)
                    _flash("Pendiente resuelto.")
                    st.rerun()
                except db.ErrorNube as e:
                    st.error(str(e))
    if r["solicitud_estado"] != "enviado_revision":
        return
    gs = [gastos_por_id[g] for g in r.get("gasto_ids", []) if g in gastos_por_id]
    with st.expander("🚩 Devolver al aplicante con un pendiente"):
        if not gs:
            st.caption("La solicitud no tiene gastos.")
            return
        opciones = {g["id"]: f"{g.get('categoria') or '—'} · cargo {money_md(g['monto_estado'])} · {len(g['movimientos'])} mov." for g in gs}
        gasto_id = st.selectbox("Gasto", list(opciones), format_func=lambda k: opciones[k], key=f"dev_g_{r['id']}") if len(gs) > 1 else gs[0]["id"]
        motivos = _catalogo(sb, "motivo_pendiente") or MOTIVOS_DEFAULT
        motivo = st.selectbox("Motivo", motivos, key=f"dev_m_{r['id']}")
        coment = st.text_area("Comentario para el aplicante (qué debe corregir)", key=f"dev_c_{r['id']}")
        if st.button("🚩 Devolver", key=f"dev_b_{r['id']}"):
            try:
                db.marcar_pendiente(sb, gasto_id, motivo, coment or None)
                _nueva_tabla_rev()
                _flash("Gasto devuelto: la solicitud regresó al aplicante con el pendiente.")
                st.rerun()
            except db.ErrorNube as e:
                st.error(str(e))


# ----------------------------------------------------------------------
# Estado de cuenta completo (el revisor ve todo como lo clasificó el aplicante y puede reclasificar)
# ----------------------------------------------------------------------
CLAVE_REGISTRO_MOV = {"comprobado": "comprobado", "no_necesario": "no_necesario", "pendiente": "sin_comprobar", "en_espera": "pendiente"}
RUBROS_RECLASIF = {"Sin comprobar": "pendiente", "No necesario": "no_necesario", "Pendiente (devolución u otro caso largo)": "en_espera"}


def _estado_cuenta_revisor(sb, cuenta: dict, periodo: dict) -> None:
    df = db.listar_movimientos_revision(sb, cuenta["id"], periodo["id"])
    if df.empty:
        st.info(f"{cuenta['nombre']} no tiene movimientos en {db.etiqueta_periodo(periodo)}.")
        return
    movs = df.drop_duplicates("id").sort_values(["fecha", "id"]).reset_index(drop=True)
    cu = reportes.cuadre(movs)
    cols = st.columns(4)
    for col, (est, titulo) in zip(cols, (("comprobado", "Comprobados"), ("no_necesario", "No necesarios"),
                                         ("pendiente", "Sin comprobar"), ("en_espera", "Pendientes"))):
        col.markdown(estilos.tarjeta(titulo, str(cu["por_estado"][est]["n"]), estilos.money_html(cu["por_estado"][est]["monto"]), est), unsafe_allow_html=True)
    p = cu["por_estado"]
    st.markdown(estilos.barra_progreso(p["comprobado"]["n"], p["no_necesario"]["n"], p["pendiente"]["n"], p["en_espera"]["n"]), unsafe_allow_html=True)
    st.markdown(estilos.cuadre_html(cu), unsafe_allow_html=True)
    st.caption("Aquí ves **todos** los cargos del estado de cuenta tal como los clasificó el aplicante. "
               "Selecciona uno o varios para pasarlos al rubro correcto.")

    f1, f2 = st.columns([1.4, 3])
    filtro = f1.selectbox("Mostrar", ["Todos", *reportes.ETIQUETA_REGISTRO.values()], key="ecr_filtro")
    texto = f2.text_input("🔎 Buscar por descripción o solicitud", key="ecr_busca")
    vista = movs
    inverso = {v: k for k, v in reportes.ETIQUETA_REGISTRO.items()}
    if filtro != "Todos":
        vista = vista[vista["estado"].map(CLAVE_REGISTRO_MOV) == inverso[filtro]]
    if texto.strip():
        t = texto.strip().lower()
        vista = vista[(vista["descripcion"].astype(str).str.lower().str.contains(t, regex=False))
                      | (vista["solicitud_numero"].astype(str).str.lower().str.contains(t, regex=False))]
    vista = vista.reset_index(drop=True)
    if vista.empty:
        st.caption("Ningún movimiento coincide con el filtro.")
        return

    def _situacion(r) -> str:
        if r["estado"] == "comprobado":
            lote = ""
            if pd.notna(r["lote_numero"]):
                lote = f" · lote de pago {int(r['lote_numero'])}" + (" (pagado)" if r["lote_estado"] == "pagado" else " (por pagar)")
            return reportes.SITUACION_SOLICITUD.get(r["solicitud_estado"], str(r["solicitud_estado"])) + lote
        return reportes._situacion_movimiento(CLAVE_REGISTRO_MOV[r["estado"]], r.get("motivo_espera"), r.get("nota_revisor"))

    tabla = pd.DataFrame({
        "Estado": vista["estado"].map(CLAVE_REGISTRO_MOV).map(reportes.ETIQUETA_REGISTRO), "Fecha": vista["fecha"],
        "Descripción": vista["descripcion"], "Monto": vista["monto"].abs(),
        "Sol. #": pd.to_numeric(vista["solicitud_numero"], errors="coerce").astype("Int64"),
        "Situación": [_situacion(r) for _, r in vista.iterrows()]})

    def _pintar(col):
        return [f"background-color:{ESTILO_ETIQUETA[inverso[v]][0]};color:{ESTILO_ETIQUETA[inverso[v]][1]};font-weight:700;text-align:center" for v in col]

    ev = st.dataframe(tabla.style.apply(_pintar, subset=["Estado"]).format({"Monto": "${:,.2f}"}, na_rep="—"),
                      hide_index=True, use_container_width=True, height=min(36 * (len(tabla) + 1) + 4, 460),
                      on_select="rerun", selection_mode="multi-row",
                      key=f"tabla_ecr_{st.session_state.get('rev_ver', 0)}_{cuenta['id']}_{periodo['id']}_{filtro}_{texto}",
                      column_config={"Sol. #": st.column_config.NumberColumn("Sol. #", format="%d", width="small"),
                                     "Estado": st.column_config.TextColumn("Estado", width="small")})
    sel = [i for i in (ev.selection.rows if ev and ev.selection else []) if i < len(vista)]
    if not sel:
        st.caption("Haz clic en una o varias filas para ver el detalle o cambiarlas de rubro.")
        return
    elegidos = vista.iloc[sel]
    st.markdown(f"**{len(elegidos)} movimiento(s) seleccionado(s)** · {money_md(elegidos['monto'].abs().sum())}")

    bloqueados = elegidos[elegidos["lote_estado"].isin(["por_pagar", "pagado"])]
    comprobados = elegidos[elegidos["estado"] == "comprobado"]
    gasto_ids = [g for g in dict.fromkeys(comprobados["gasto_id"].dropna())]
    arrastrados = int(df[df["gasto_id"].isin(gasto_ids)].drop_duplicates("id").shape[0]) if gasto_ids else 0

    if len(elegidos) == 1 and not comprobados.empty:
        sols = db.listar_solicitudes(sb, cuenta["id"], periodo["id"])
        gastos = db.listar_gastos(sb, [x["id"] for x in sols]) if sols else []
        fila = next((f for f in reportes.filas_desglose(df, gastos) if f["tipo"] == "solicitud" and f["id"] == comprobados.iloc[0]["solicitud_id"]), None)
        if fila:
            _detalle_solicitud_enviada(df, fila, {g["id"]: g for g in gastos})
    elif len(elegidos) == 1:
        m = elegidos.iloc[0]
        nota = m.get("nota_revisor")
        if isinstance(nota, str) and nota.strip():
            st.info(f"💬 Nota que dejaste: {nota}")

    with st.container(border=True):
        st.markdown("**🔀 Pasar al rubro correcto**")
        if not bloqueados.empty:
            st.warning(f"{len(bloqueados)} de los movimientos seleccionados están en un lote de pago cerrado. "
                       "Desbloquea primero el lote en «Lotes y pagos».")
        if not comprobados.empty:
            st.warning(f"{len(comprobados)} de los seleccionados están **comprobados**. Reclasificarlos revierte su gasto completo "
                       f"({len(gasto_ids)} gasto(s), {arrastrados} movimiento(s) en total): se eliminan sus facturas, la solicitud regresa "
                       "al aplicante en borrador y los demás movimientos del gasto quedan «sin comprobar».")
        r1, r2 = st.columns([1.6, 2])
        rubro = r1.radio("Nuevo rubro", list(RUBROS_RECLASIF), key="ecr_rubro")
        motivo_espera = None
        if RUBROS_RECLASIF[rubro] == "en_espera":
            motivo_espera = r2.selectbox("Por qué queda pendiente", _catalogo(sb, "motivo_espera") or MOTIVOS_ESPERA_DEFAULT, key="ecr_motivo")
        nota = st.text_area("Nota para el aplicante (obligatoria): por qué lo cambias", key="ecr_nota")
        ok_rev = True
        if not comprobados.empty:
            ok_rev = st.checkbox("Entiendo que se revierte el gasto comprobado", key="ecr_ok")
        if st.button(f"Pasar {len(elegidos)} movimiento(s) a «{rubro.split(' (')[0]}»", type="primary", key="ecr_aplicar",
                     disabled=not bloqueados.empty or not ok_rev):
            try:
                revertidos = db.reclasificar_movimientos(sb, list(elegidos["id"]), RUBROS_RECLASIF[rubro], nota, motivo_espera)
                _nueva_tabla_rev()
                _flash(f"{len(elegidos)} movimiento(s) reclasificado(s)" + (f"; se revirtieron {revertidos} gasto(s)." if revertidos else "."))
                st.rerun()
            except db.ErrorNube as e:
                st.error(str(e))


# ----------------------------------------------------------------------
# Lotes y pagos
# ----------------------------------------------------------------------
def _excel_lote(lote: dict, cuenta_nombre: str, etiqueta_mes: str, gastos: list[dict]) -> bytes:
    import io
    filas = [{"Solicitud": g["solicitud"].get("numero"), "Fecha": g["solicitud"].get("fecha"), "Descripción": g["solicitud"].get("description") or "",
              "Empleado": g["solicitud"].get("employee_name") or "", "Categoría": g.get("categoria") or "",
              "Monto del cargo": float(g["monto_estado"]), "Facturas": float(g["suma_facturas"]), "Diferencia": float(g["diferencia"])} for g in gastos]
    tabla = pd.DataFrame(filas)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as w:
        resumen = pd.DataFrame([{"Cuenta": cuenta_nombre, "Mes": etiqueta_mes, "Lote": lote["numero"], "Gastos": lote["num_gastos"],
                                 "Monto": float(lote["monto"]), "Estado": lote["estado"], "Fecha de pago": lote.get("fecha_pago") or "",
                                 "Referencia": lote.get("referencia_pago") or ""}])
        resumen.to_excel(w, sheet_name="Lote", index=False)
        tabla.to_excel(w, sheet_name="Gastos", index=False)
        fmt = w.book.add_format({"num_format": "$#,##0.00"})
        w.sheets["Gastos"].set_column("A:E", 18)
        w.sheets["Gastos"].set_column("F:H", 16, fmt)
        w.sheets["Lote"].set_column("A:H", 16)
        w.sheets["Lote"].set_column("E:E", 16, fmt)
    return buf.getvalue()


def _lotes_revisor(sb, cuenta: dict, cuentas: list[dict], periodos: list[dict]) -> None:
    alcance = st.radio("Mostrar", ["Esta cuenta", "Todas las cuentas"], horizontal=True, key="rev_lotes_alc")
    lotes = db.listar_lotes(sb, cuenta["id"] if alcance.startswith("Esta") else None)
    if not lotes:
        st.info("Todavía no hay lotes de pago. Se crean al dar visto bueno en la cola de revisión.")
        return
    nombre_cta = {c["id"]: c["nombre"] for c in cuentas}
    mes = {p["id"]: db.etiqueta_periodo(p) for p in periodos}
    por_pagar = [l for l in lotes if l["estado"] == "por_pagar"]
    pagados = [l for l in lotes if l["estado"] == "pagado"]
    k = st.columns(3)
    k[0].markdown(estilos.tarjeta("Por pagar", str(len(por_pagar)), estilos.money_html(sum(float(l["monto"]) for l in por_pagar)), "alerta"), unsafe_allow_html=True)
    k[1].markdown(estilos.tarjeta("Pagados", str(len(pagados)), estilos.money_html(sum(float(l["monto"]) for l in pagados)), "comprobado"), unsafe_allow_html=True)
    k[2].markdown(estilos.tarjeta("Anulados", str(sum(1 for l in lotes if l["estado"] == "anulado")), "desbloqueados", "pendiente"), unsafe_allow_html=True)
    tabla = pd.DataFrame([{
        "Cuenta": nombre_cta.get(l["cuenta_id"], ""), "Lote": f"{mes.get(l['periodo_id'], '')}-{l['numero']}", "Cerrado": str(l["cerrado_en"])[:10],
        "Gastos": l["num_gastos"], "Monto": float(l["monto"]), "Estado": NOMBRE_LOTE.get(l["estado"], l["estado"]),
        "Fecha de pago": l.get("fecha_pago") or "", "Referencia": l.get("referencia_pago") or ""} for l in lotes])
    ev = st.dataframe(tabla, hide_index=True, use_container_width=True, height=min(36 * (len(tabla) + 1) + 4, 360),
                      on_select="rerun", selection_mode="single-row", key=f"tabla_lotes_{st.session_state.get('rev_ver', 0)}_{alcance}",
                      column_config={"Monto": st.column_config.NumberColumn("Monto", format="$%.2f")})
    sel = [i for i in (ev.selection.rows if ev and ev.selection else []) if i < len(lotes)]
    if not sel:
        st.caption("Haz clic en un lote para ver sus gastos, registrar el pago o desbloquearlo.")
        return
    lote = lotes[sel[0]]
    gastos = db.gastos_de_lote(sb, lote["id"])
    etiqueta = f"{nombre_cta.get(lote['cuenta_id'], '')} · {mes.get(lote['periodo_id'], '')} · lote {lote['numero']}"
    with st.container(border=True):
        st.markdown(f"**{etiqueta}** · {NOMBRE_LOTE.get(lote['estado'], lote['estado'])} · {money_md(lote['monto'])} · {lote['num_gastos']} gasto(s)")
        if gastos:
            det = pd.DataFrame([{"Sol. #": g["solicitud"].get("numero"), "Descripción": g["solicitud"].get("description") or "",
                                 "Empleado": g["solicitud"].get("employee_name") or "", "Categoría": g.get("categoria") or "",
                                 "Cargo": float(g["monto_estado"]), "Facturas": float(g["suma_facturas"]), "Diferencia": float(g["diferencia"])} for g in gastos])
            st.dataframe(det, hide_index=True, use_container_width=True,
                         column_config={c: st.column_config.NumberColumn(c, format="$%.2f") for c in ("Cargo", "Facturas", "Diferencia")})
        st.download_button("⬇️ Excel del lote (para tesorería)", _excel_lote(lote, nombre_cta.get(lote["cuenta_id"], ""), mes.get(lote["periodo_id"], ""), gastos),
                           file_name=f"Lote_{nombre_cta.get(lote['cuenta_id'], '').replace(' ', '_')}_{mes.get(lote['periodo_id'], '')}_{lote['numero']}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key=f"xl_lote_{lote['id']}")
        if lote["estado"] == "pagado":
            st.success(f"Pagado el {lote.get('fecha_pago')} · referencia {lote.get('referencia_pago')}")
        elif lote["estado"] == "anulado":
            st.info(f"Lote anulado: {lote.get('anulado_motivo') or ''}. Sus solicitudes regresaron a «Por revisar».")
        else:
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**💸 Registrar pago**")
                fecha = st.date_input("Fecha de pago", value=hoy_cdmx(), key=f"pg_f_{lote['id']}")
                ref = st.text_input("Referencia / folio de pago (por ejemplo AP-MX o PY-MX)", key=f"pg_r_{lote['id']}")
                if st.button("✅ Marcar como pagado", type="primary", key=f"pg_b_{lote['id']}"):
                    try:
                        db.pagar_lote(sb, lote["id"], fecha, ref)
                        _nueva_tabla_rev()
                        _flash("Pago registrado: el aplicante ya lo ve como pagado.")
                        st.rerun()
                    except db.ErrorNube as e:
                        st.error(str(e))
            with c2:
                st.markdown("**↩️ Desbloquear lote**")
                st.caption("Anula el lote y regresa sus solicitudes a «Por revisar». Solo si aún no está pagado.")
                mot = st.text_input("Motivo (obligatorio)", key=f"ds_m_{lote['id']}")
                if st.button("Desbloquear lote", key=f"ds_b_{lote['id']}"):
                    try:
                        db.desbloquear_lote(sb, lote["id"], mot)
                        _nueva_tabla_rev()
                        _flash("Lote desbloqueado.")
                        st.rerun()
                    except db.ErrorNube as e:
                        st.error(str(e))


# ----------------------------------------------------------------------
# Resumen de cuentas y cierre de mes
# ----------------------------------------------------------------------
def _resumen_revisor(sb, cuentas: list[dict], periodo: dict) -> None:
    cob = {c["cuenta_id"]: c for c in db.listar_cobertura(sb, periodo["id"])}
    cierre = db.estados_cierre_periodo(sb, periodo["id"])
    aplicantes = db.aplicantes_por_cuenta(sb)
    filas = []
    for c in cuentas:
        sols = db.listar_solicitudes(sb, c["id"], periodo["id"])
        cuenta_estados = collections.Counter(s["estado"] for s in sols)
        v = cob.get(c["id"], {})
        filas.append({
            "Cuenta": c["nombre"], "Aplicante": aplicantes.get(c["id"], "—"), "Movimientos": int(v.get("movimientos", 0)),
            "Sin comprobar": int(v.get("pendientes", 0)), "Pendientes": int(v.get("en_espera", 0)), "No necesarios": int(v.get("no_necesarios", 0)),
            "Comprobados sin lote": int(v.get("comprobados_sin_lote", 0)), "En lote cerrado": int(v.get("en_lote_cerrado", 0)),
            "Monto en lote": float(v.get("monto_en_lote", 0)), "Por revisar": cuenta_estados.get("enviado_revision", 0),
            "Devueltas": cuenta_estados.get("con_pendientes", 0), "Mes": "🔒 Cerrado" if cierre.get(c["id"]) == "cerrado" else "Abierto"})
    st.markdown(f"##### Cobertura de {db.etiqueta_periodo(periodo)} por cuenta")
    st.dataframe(pd.DataFrame(filas), hide_index=True, use_container_width=True,
                 column_config={"Monto en lote": st.column_config.NumberColumn(format="$%.2f")})
    st.caption("Un mes se puede cerrar cuando no queda nada sin comprobar, ni comprobado sin lote cerrado, ni solicitudes abiertas o con pendientes. "
               "Los movimientos «pendientes» (devoluciones largas) no impiden el cierre.")
    st.markdown("##### 📅 Cierre de mes")
    nombres = [c["nombre"] for c in cuentas]
    c1, c2 = st.columns([1.4, 3])
    cuenta = cuentas[nombres.index(c1.selectbox("Cuenta", nombres, key="rev_cierre_cuenta"))]
    cerrado = cierre.get(cuenta["id"]) == "cerrado"
    if not cerrado:
        if c2.button(f"🔒 Cerrar {db.etiqueta_periodo(periodo)} de {cuenta['nombre']}", type="primary", key="cierre_ok"):
            try:
                db.cerrar_mes_cuenta(sb, periodo["id"], cuenta["id"])
                _flash(f"Mes cerrado para {cuenta['nombre']}.")
                st.rerun()
            except db.ErrorNube as e:
                st.error(str(e))
    else:
        c2.success("Este mes ya está cerrado (solo lectura para el aplicante).")
        mot = st.text_input("Motivo para reabrir (obligatorio)", key="reab_m")
        if st.button("🔓 Reabrir mes", key="reab_b"):
            try:
                db.reabrir_mes_cuenta(sb, periodo["id"], cuenta["id"], mot)
                _flash("Mes reabierto.")
                st.rerun()
            except db.ErrorNube as e:
                st.error(str(e))


# ======================================================================
# Principal
# ======================================================================
def main() -> None:
    sb = _cliente()
    if "user_id" not in st.session_state or not db.sesion_vigente(sb):
        st.session_state.pop("user_id", None)
        _pantalla_login(sb)
        return

    try:
        if "perfil" not in st.session_state:
            st.session_state.perfil = db.cargar_perfil(sb, st.session_state.user_id)
            st.session_state.tol = db.tolerancia(sb, DIFERENCIA_ACEPTABLE)
            if st.session_state.perfil:
                db.asegurar_catalogos(sb, {
                    "categoria": list(CATEGORIAS_SOLICITUD_DEFAULT), "material": list(MATERIALES_DEFAULT),
                    "empleado": list(EMPLEADOS_DEFAULT), "motivo_pendiente": MOTIVOS_DEFAULT})
                try:
                    db.asegurar_catalogos(sb, {"motivo_espera": MOTIVOS_ESPERA_DEFAULT})
                except db.ErrorNube:
                    pass   # todavía no se corrió el script 007
        perfil = st.session_state.perfil
    except db.ErrorNube as e:
        st.error(str(e))
        if st.button("Cerrar sesión"):
            _salir(sb)
        return

    if perfil and perfil.get("cuenta_id"):
        if "_cuenta" not in st.session_state:
            st.session_state._cuenta = db.cargar_cuenta(sb, perfil["cuenta_id"])
        cuenta = st.session_state._cuenta
    else:
        cuenta = None
    cab = st.columns([5, 2, 1])
    cab[0].markdown(estilos.encabezado(), unsafe_allow_html=True)
    if perfil:
        rol_txt = "Revisor" if perfil["rol"] == "revisor" else "Aplicante · " + (cuenta["nombre"] if cuenta else "")
        cab[1].markdown(f'<div class="usuario-chip"><b>{perfil["nombre"]}</b><br><small>{rol_txt}</small></div>', unsafe_allow_html=True)
    if cab[2].button("Salir", key="salir"):
        _salir(sb)

    if not perfil:
        st.warning("Tu usuario todavía no tiene un perfil asignado. Pide al revisor que lo configure.")
        return
    _mostrar_flash()
    if perfil["rol"] == "revisor":
        _vista_revisor(sb)
        return

    # ---- aplicante ----
    hoy = hoy_cdmx()
    if st.session_state.get("_periodos_dia") != hoy.isoformat() or "_periodos" not in st.session_state:
        db.obtener_o_crear_periodo(sb, hoy.year, hoy.month)
        st.session_state._periodos = db.listar_periodos(sb)
        st.session_state._periodos_dia = hoy.isoformat()
    periodos = st.session_state._periodos
    cierre = db.estado_cierre(sb, cuenta["id"])
    actual = f"{hoy.year}-{hoy.month:02d}"
    etiquetas = [db.etiqueta_periodo(p) for p in periodos]
    with st.sidebar:
        st.markdown(f'<div class="sidebar-header"><div class="sidebar-header-icon">💼</div><span>{cuenta["nombre"]}</span></div>', unsafe_allow_html=True)
        sel = st.selectbox("Mes", etiquetas, index=etiquetas.index(actual) if actual in etiquetas else 0, key="mes_sel")
        periodo = periodos[etiquetas.index(sel)]
        cerrado = cierre.get(periodo["id"]) == "cerrado"
        if cerrado:
            st.warning("Mes cerrado (solo lectura).")
        if cuenta.get("limite_reembolso"):
            st.caption(f"Límite de reembolso: {money_md(cuenta['limite_reembolso'])}")

    if st.session_state.get("comprobando"):
        _pantalla_comprobar(sb, cuenta, st.session_state.comprobando, st.session_state.tol)
        return

    # Control de sección (en lugar de st.tabs) para conservar la sección elegida después de cada acción.
    secciones = ["🧾 Bitácora", "🏦 Estado de cuenta", "📤 Enviados a comprobación", "🚩 Pendientes devueltos", "💸 Lotes y pagos"]
    elegida = st.segmented_control("Sección", secciones, default=secciones[0], label_visibility="collapsed", key="seccion")
    vista = elegida or st.session_state.get("_seccion_ult", secciones[0])
    st.session_state["_seccion_ult"] = vista
    st.divider()
    if vista == secciones[0]:
        _tab_bitacora(sb, cuenta, periodo, cerrado)
    elif vista == secciones[1]:
        _tab_estado_cuenta(sb, cuenta, periodo, cerrado)
    elif vista == secciones[2]:
        _tab_enviados(sb, cuenta, periodo, cerrado)
    elif vista == secciones[3]:
        _tab_pendientes(sb, cuenta)
    else:
        _tab_lotes(sb, cuenta, periodos)
    _visor_adjunto(sb)


main()
