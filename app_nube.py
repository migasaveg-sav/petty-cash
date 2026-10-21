"""Comprobación de Caja Chica — versión en la nube (Etapa 1: aplicante).

Se ejecuta con:  streamlit run app_nube.py
Requiere los secretos SUPABASE_URL y SUPABASE_KEY (clave anon/publishable).
Reutiliza bank.py, cfdi.py, catalog.py, matching.py y persistence.py de la app actual.
"""
from __future__ import annotations

import base64
import datetime

import pandas as pd
import streamlit as st

import adjuntos as adj
import estilos
import nube_db as db
from bank import BankStatementError, cargar_estado_cuenta
from catalog import CATEGORIAS_SOLICITUD_DEFAULT, EMPLEADOS_DEFAULT, MATERIALES_DEFAULT
from cfdi import CFDIParseError, parse_cfdi
from matching import DIFERENCIA_ACEPTABLE, resumen_gasto_bitacora
from persistence import hoy_cdmx

CATEGORIAS_CON_PROPINA = {"Client Entertainment", "Travel Meal"}
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
def _selector_catalogo(sb, etiqueta: str, tipo: str, key: str, valor_actual: str | None = None) -> str | None:
    """Selectbox con el catálogo + una caja para agregar un valor nuevo (se guarda al
    usarlo). Regresa el valor elegido (o None)."""
    opciones = db.cargar_catalogo(sb, tipo)
    if valor_actual and valor_actual not in opciones:
        opciones = [valor_actual, *opciones]
    sel = st.selectbox(etiqueta, ["—", *opciones], index=(opciones.index(valor_actual) + 1) if valor_actual in opciones else 0, key=f"{key}_sel")
    nuevo = st.text_input(f"{etiqueta}: otro valor (si no está en la lista)", key=f"{key}_nuevo", placeholder="Opcional")
    if nuevo.strip():
        return nuevo.strip()
    return None if sel == "—" else sel


def _guardar_catalogo_si_nuevo(sb, tipo: str, valor: str | None) -> None:
    if valor and valor not in db.cargar_catalogo(sb, tipo):
        db.agregar_catalogo(sb, tipo, valor)


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
    return db.obtener_o_crear_periodo(sb, fecha.year, fecha.month)


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
                    _flash("Solicitud registrada.")
                    st.rerun()
                except db.ErrorNube as e:
                    st.error(str(e))

    sols = db.listar_solicitudes(sb, cuenta["id"], periodo["id"])
    st.markdown(f"##### Solicitudes de {db.etiqueta_periodo(periodo)} ({len(sols)})")
    if not sols:
        st.caption("Aún no hay solicitudes en este mes.")
        return
    gastos = db.listar_gastos(sb, [s["id"] for s in sols])
    por_sol: dict[str, list[dict]] = {}
    for g in gastos:
        por_sol.setdefault(g["solicitud_id"], []).append(g)

    st.markdown('<div class="bitacora-head"><div>#</div><div>Fecha</div><div>Monto</div><div>Categoría · Descripción</div>'
                '<div>Estado</div><div>Comprobación</div></div>', unsafe_allow_html=True)
    for s in sols:
        editable = s["estado"] in EDITABLES and not cerrado
        gs = por_sol.get(s["id"], [])
        with st.container(border=True, key=f"sol-{s['id']}"):
            top = st.columns([0.5, 1.1, 1.1, 2.6, 1.4, 1.6])
            top[0].markdown(f"**#{s['numero']}**")
            top[1].write(s["fecha"])
            top[2].write(money_md(s["monto_tentativo"]))
            top[3].write(f"{s.get('category') or '—'} · {s.get('description') or ''}")
            top[4].write(ESTADOS_SOLICITUD.get(s["estado"], s["estado"]))
            top[5].write(f"{len(gs)} gasto(s) comprobado(s)")
            acciones = st.columns([1.2, 1.5, 1, 4])
            if editable and acciones[0].button("🔗 Comprobar", key=f"cmp_{s['id']}"):
                st.session_state.comprobando = s
                for k in [k for k in st.session_state if k.startswith("cmp_") and k != f"cmp_{s['id']}"]:
                    del st.session_state[k]
                st.rerun()
            if editable and gs and acciones[1].button("📤 Enviar a revisión", key=f"env_{s['id']}"):
                try:
                    db.enviar_a_revision(sb, s["id"])
                    _flash(f"Solicitud #{s['numero']} enviada a revisión.")
                    st.rerun()
                except db.ErrorNube as e:
                    st.error(str(e))
            if editable and not gs and acciones[2].button("🗑️", key=f"del_{s['id']}", help="Eliminar solicitud"):
                try:
                    db.eliminar_solicitud(sb, s["id"])
                    st.rerun()
                except db.ErrorNube as e:
                    st.error(str(e))
            if gs or editable:
                with st.expander("Detalle" + (f" ({len(gs)} gasto(s))" if gs else "")):
                    if editable:
                        st.markdown("**Editar datos de la solicitud**")
                        nuevos = _campos_solicitud(sb, f"ed_{s['id']}", s)
                        if st.button("Guardar cambios", key=f"gc_{s['id']}"):
                            try:
                                for tipo, clave in (("categoria", "category"), ("empleado", "employee_name"), ("material", "material")):
                                    _guardar_catalogo_si_nuevo(sb, tipo, nuevos[clave])
                                db.actualizar_solicitud(sb, s["id"], {
                                    "fecha": str(nuevos["fecha"]), "monto_tentativo": nuevos["monto"], "category": nuevos["category"],
                                    "material": nuevos["material"], "employee_name": nuevos["employee_name"],
                                    "request_number": nuevos["request_number"] or None, "description": nuevos["description"] or None,
                                    "number_of_days": nuevos["number_of_days"], "number_of_people": nuevos["number_of_people"]})
                                _flash("Cambios guardados.")
                                st.rerun()
                            except db.ErrorNube as e:
                                st.error(str(e))
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
        cols[0].caption(f"🧾 {(f.get('uuid_cfdi') or 'SIN-UUID')} · {f.get('rfc_emisor') or ''} · {f.get('fecha_factura') or ''}")
        cols[1].caption(money_md(f["monto_aplicado"]))
        if f.get("adjunto") and cols[2].button("📎 Ver", key=f"ver_{g['id']}_{f['id']}"):
            st.session_state.adj_ver = f["adjunto"]["ruta"]
    if editable and st.button("↩️ Revertir este gasto", key=f"rev_{g['id']}"):
        try:
            db.revertir_gasto(sb, g["id"])
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
    pend = db.listar_movimientos(sb, cuenta["id"], estado="pendiente")
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
        filas = ev.selection.rows if ev and ev.selection else []
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
    categorias = db.cargar_catalogo(sb, "categoria")
    cat_ini = sol.get("category")
    with c1:
        categoria = st.selectbox("Categoría", ["—", *categorias], index=(categorias.index(cat_ini) + 1) if cat_ini in categorias else 0, key="cmp_categoria")
    with c2:
        mats = db.cargar_catalogo(sb, "material")
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

    if todos.empty:
        st.info("Todavía no hay movimientos. Carga el estado de cuenta arriba.")
        return
    mes = todos[todos["periodo_id"] == periodo["id"]]
    c = st.columns(3)
    for col, (est, titulo) in zip(c, (("pendiente", "Pendientes"), ("comprobado", "Comprobados"), ("no_necesario", "No necesarios"))):
        sub = mes[mes["estado"] == est]
        col.markdown(estilos.tarjeta(f"{titulo} ({db.etiqueta_periodo(periodo)})", f"{len(sub)}", estilos.money_html(sub["monto"].abs().sum()), est), unsafe_allow_html=True)
    f1, f2 = st.columns([1, 2])
    estado = f1.selectbox("Mostrar", ["pendiente", "comprobado", "no_necesario", "todos"], key="filtro_estado_mov")
    alcance = f2.radio("Periodo", ["Solo el mes seleccionado", "Todos los meses"], horizontal=True, key="alcance_mov")
    vista = (mes if alcance.startswith("Solo") else todos)
    if estado != "todos":
        vista = vista[vista["estado"] == estado]
    vista = vista.reset_index(drop=True)
    ev = st.dataframe(vista[["fecha", "descripcion", "monto", "saldo", "estado"]], hide_index=True, use_container_width=True,
                      on_select="rerun", selection_mode="multi-row", key="tabla_mov",
                      column_config={"fecha": "Fecha", "descripcion": "Descripción", "estado": "Estado",
                                     "monto": st.column_config.NumberColumn("Monto", format="$%.2f"), "saldo": st.column_config.NumberColumn("Saldo", format="$%.2f")})
    filas = ev.selection.rows if ev and ev.selection else []
    if filas and not cerrado:
        sel = vista.iloc[filas]
        solo_pend = not bool((sel["estado"] != "pendiente").any())
        mismo_mes = sel["periodo_id"].nunique() == 1
        b0, b1, b2 = st.columns([1.6, 1.4, 1.4])
        if b0.button("🔗 Comprobar seleccionados", type="primary", key="mov_cmp", disabled=not (solo_pend and mismo_mes)):
            _comprobar_desde_estado(sb, cuenta, periodo, sel)
        if not solo_pend:
            st.caption("Para comprobar, selecciona solo movimientos pendientes.")
        elif not mismo_mes:
            st.caption("Para comprobar juntos, los movimientos deben ser del mismo mes.")
        else:
            st.caption("Se creará una solicitud nueva con estos movimientos y se abrirá la ventana de comprobación.")
        if b1.button("🚫 Marcar no necesario", key="mov_nn", disabled=bool((sel["estado"] != "pendiente").any())):
            _cambiar(sb, sel["id"].tolist(), "no_necesario")
        if b2.button("↩️ Regresar a pendiente", key="mov_pend", disabled=bool((sel["estado"] != "no_necesario").any())):
            _cambiar(sb, sel["id"].tolist(), "pendiente")


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


def _cambiar(sb, ids: list[str], estado: str) -> None:
    try:
        db.cambiar_estado_movimientos(sb, ids, estado)
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
ICONO_MOV = {"comprobado": "🟢", "no_necesario": "⚪", "pendiente": "🟠"}
NOMBRE_MOV = {"comprobado": "Comprobado", "no_necesario": "No necesario", "pendiente": "Pendiente"}
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
    cuenta_est = df["estado"].value_counts().to_dict()
    monto_est = df.assign(a=df["monto"].abs()).groupby("estado")["a"].sum().to_dict()
    comp, nn, pend = (int(cuenta_est.get(k, 0)) for k in ("comprobado", "no_necesario", "pendiente"))
    cols = st.columns(3)
    for col, est, titulo, n in zip(cols, ("comprobado", "no_necesario", "pendiente"), ("Comprobados", "No necesarios", "Pendientes"), (comp, nn, pend)):
        col.markdown(estilos.tarjeta(titulo, str(n), estilos.money_html(monto_est.get(est, 0)), est), unsafe_allow_html=True)
    st.markdown(estilos.barra_progreso(comp, nn, pend), unsafe_allow_html=True)

    # ---- Envío a revisión ----
    por_enviar = df[(df["estado"] == "comprobado") & df["solicitud_estado"].isin(EDITABLES)]["solicitud_id"].nunique()
    envios = db.listar_envios(sb, cuenta["id"], periodo["id"])
    with st.container(border=True):
        if envios:
            u = envios[0]
            st.markdown(f"**Último envío:** {_fecha_cdmx(u['enviado_en'])} · {u['comprobados']} comprobados, "
                        f"{u['no_necesarios']} no necesarios, {u['pendientes']} pendientes · {u['solicitudes_enviadas']} solicitud(es) enviada(s)"
                        + (f" · _{u['nota']}_" if u.get("nota") else ""))
        else:
            st.markdown("**Aún no has enviado este mes a revisión.**")
        st.caption("Al enviar se mandan todas las solicitudes del mes que ya tienen gastos comprobados. El revisor verá el estado de cuenta completo, "
                   "con lo comprobado, lo no necesario y lo que sigue pendiente, para saber cuánto falta.")
        nota = st.text_input("Nota para el revisor (opcional)", key="env_nota", disabled=cerrado)
        if st.button(f"📤 Enviar estado de cuenta a revisión ({por_enviar} solicitud(es) por enviar)", type="primary",
                     key="btn_enviar_ec", disabled=cerrado or por_enviar == 0):
            try:
                db.enviar_estado_cuenta(sb, periodo["id"], nota)
                if "env_nota" in st.session_state:
                    del st.session_state["env_nota"]
                _flash("Estado de cuenta enviado a revisión.")
                st.rerun()
            except db.ErrorNube as e:
                st.error(str(e))
        if por_enviar == 0 and not cerrado:
            st.caption("No hay solicitudes con gastos comprobados por enviar. Comprueba gastos o espera a que el revisor devuelva alguno.")
    if envios:
        with st.expander(f"Historial de envíos ({len(envios)})"):
            st.dataframe(pd.DataFrame([{
                "Enviado": _fecha_cdmx(x["enviado_en"]), "Comprobados": x["comprobados"], "No necesarios": x["no_necesarios"],
                "Pendientes": x["pendientes"], "Solicitudes": x["solicitudes_enviadas"], "Nota": x.get("nota") or ""} for x in envios]),
                hide_index=True, use_container_width=True)

    # ---- Desglose ----
    st.markdown("##### Desglose del estado de cuenta")
    f1, f2, f3 = st.columns([1.2, 2.4, 1])
    filtro = f1.selectbox("Estado", ["Todos", "Comprobado", "No necesario", "Pendiente"], key="env_filtro")
    texto = f2.text_input("🔎 Buscar por descripción", key="env_busca")
    vista = df
    if filtro != "Todos":
        vista = vista[vista["estado"] == {v: k for k, v in NOMBRE_MOV.items()}[filtro]]
    if texto.strip():
        vista = vista[vista["descripcion"].str.contains(texto.strip(), case=False, na=False)]
    paginas = max(1, -(-len(vista) // POR_PAGINA))
    pagina = f3.selectbox("Página", list(range(1, paginas + 1)), key=f"env_pag_{filtro}_{texto}_{paginas}") if paginas > 1 else 1
    st.caption(f"{len(vista)} movimiento(s). Haz clic en uno para ver toda su información.")
    pag = vista.iloc[(pagina - 1) * POR_PAGINA: pagina * POR_PAGINA]
    sol_ids = [x for x in pag["solicitud_id"].dropna().unique().tolist()]
    gastos = {g["id"]: g for g in db.listar_gastos(sb, sol_ids)} if sol_ids else {}
    for idx, r in pag.iterrows():
        dif = bool(r["tiene_diferencia"]) if pd.notna(r["tiene_diferencia"]) else False
        etiqueta = (f"{ICONO_MOV[r['estado']]} {r['fecha']}  ·  {r['descripcion']}  ·  {money_md(r['monto'])}  ·  {NOMBRE_MOV[r['estado']]}"
                    + (f"  ·  Sol. #{int(r['solicitud_numero'])}" if pd.notna(r["solicitud_numero"]) else "") + ("  ·  🟡 diferencia" if dif else ""))
        with st.container(key=f"mv-{r['estado']}{'-dif' if dif else ''}-{r['id']}"):
            with st.expander(etiqueta):
                _detalle_movimiento(sb, df, idx, r, gastos.get(r["gasto_id"]) if pd.notna(r["gasto_id"]) else None, cuenta, periodo, cerrado)


def _detalle_movimiento(sb, df: pd.DataFrame, idx, r, g: dict | None, cuenta: dict, periodo: dict, cerrado: bool) -> None:
    a, b, c = st.columns(3)
    a.markdown("**🏦 Movimiento**")
    a.write(f"{r['fecha']}  \n{r['descripcion']}")
    a.write(f"Monto: {money_md(r['monto'])}" + (f"  \nSaldo: {money_md(r['saldo'])}" if pd.notna(r["saldo"]) else ""))
    if r["estado"] == "no_necesario":
        b.markdown("**Estado**")
        b.write("Marcado como no necesario: no requiere comprobación.")
        return
    if r["estado"] == "pendiente":
        b.markdown("**Estado**")
        b.write("Aún sin comprobar.")
        if not cerrado and b.button("🔗 Comprobar este movimiento", key=f"mvcmp_{r['id']}"):
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
# Revisor (Etapa 2 pendiente)
# ======================================================================
def _vista_revisor(sb) -> None:
    st.info("La vista completa del revisor (cola de revisión, visto bueno, lotes, pagos y cierre de mes) se construye en la Etapa 2. "
            "Mientras tanto aquí puedes ver el avance de las cuentas.")
    cuentas = db.listar_cuentas(sb)
    filas = []
    for c in cuentas:
        sols = db.listar_solicitudes(sb, c["id"])
        cuenta_estados = pd.Series([s["estado"] for s in sols]).value_counts().to_dict() if sols else {}
        envios = db.listar_envios(sb, c["id"])
        u = envios[0] if envios else None
        filas.append({"Cuenta": c["nombre"], **{ESTADOS_SOLICITUD[k]: cuenta_estados.get(k, 0) for k in ESTADOS_SOLICITUD},
                      "Último envío": _fecha_cdmx(u["enviado_en"]) if u else "—",
                      "Avance del envío": (f"{u['comprobados']} comprobados · {u['no_necesarios']} no necesarios · {u['pendientes']} pendientes" if u else "—")})
    st.dataframe(pd.DataFrame(filas), hide_index=True, use_container_width=True)


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
        perfil = st.session_state.perfil
    except db.ErrorNube as e:
        st.error(str(e))
        if st.button("Cerrar sesión"):
            _salir(sb)
        return

    cuenta = db.cargar_cuenta(sb, perfil["cuenta_id"]) if perfil and perfil.get("cuenta_id") else None
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
    db.obtener_o_crear_periodo(sb, hoy.year, hoy.month)
    periodos = db.listar_periodos(sb)
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
