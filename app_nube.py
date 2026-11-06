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
import bbva_detalle
import estilos
import importar
import nube_db as db
import reportes_nube as reportes
import pdf_estado
from bank import BankStatementError, cargar_estado_cuenta
from catalog import CATEGORIAS_SOLICITUD_DEFAULT, EMPLEADOS_DEFAULT, MATERIALES_DEFAULT
from cfdi import CFDIParseError, parse_cfdi
from matching import DIFERENCIA_ACEPTABLE, resumen_gasto_bitacora
from persistence import hoy_cdmx

CATEGORIAS_CON_PROPINA = {"Client Entertainment", "Travel Meal"}
MOTIVO_DEV_COLABORADOR = "Devolución del colaborador"   # motivo de «Pendiente» que alimenta la sección Devoluciones del revisor
MOTIVOS_ESPERA_DEFAULT = [MOTIVO_DEV_COLABORADOR, "Devolución en trámite", "Aclaración con el banco", "Falta la factura del proveedor", "Reembolso por recibir", "Otro"]
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


def _leer_estado(contenido: bytes, nombre: str, formato: str) -> pd.DataFrame:
    """Lee el estado de cuenta. En cuentas BBVA primero se prueba el formato «Detalle de movimientos» con columnas separadas
    (fechas sin encabezado, cargo y abono en columnas separadas; sirve para todas las cuentas BBVA); si el archivo no es de ese tipo, se usa el lector general."""
    if str(formato).strip().upper().startswith("BBVA"):
        try:
            return bbva_detalle.leer_bbva_prj(contenido, nombre)
        except bbva_detalle.FormatoNoReconocido:
            pass
        except bbva_detalle.ErrorEstadoBBVA as e:
            raise BankStatementError(str(e))
    return cargar_estado_cuenta(contenido, nombre, formato)


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
    # Movimientos del estado de cuenta cargado que todavía no se comprueban (sin comprobar o pendientes).
    sin_cmp = db.listar_movimientos(sb, cuenta["id"], estado=["pendiente", "en_espera"])
    if sin_cmp is not None and not sin_cmp.empty:
        f0, f1 = str(min(sin_cmp["fecha"]))[:10], str(max(sin_cmp["fecha"]))[:10]
        rango = f"del {f0} al {f1}" if f0 != f1 else f"del {f0}"
        c1, c2, c3, c4 = st.columns(4)
        c4.markdown(estilos.tarjeta("Movimientos pendientes por comprobar", estilos.money_html(float(sin_cmp["monto"].abs().sum())),
                                    f"{len(sin_cmp)} movimiento(s) · {rango}", "pendiente"), unsafe_allow_html=True)
    else:
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


def _firma(ids) -> str:
    """Huella corta de los renglones que muestra una tabla (en su orden).
    Streamlit conserva la selección de una tabla por POSICIÓN de fila mientras la llave no cambie, aunque cambien los datos
    (buscador, filtros, orden, movimientos que se comprueban...). Al incluir esta huella en la llave, si la lista cambia la
    selección se reinicia en lugar de apuntar a otro renglón."""
    import hashlib
    return hashlib.md5("|".join(str(i) for i in ids).encode()).hexdigest()[:8]


def _nueva_tabla() -> None:
    """Cambia la llave de la tabla de la bitácora para que no quede seleccionada una fila que ya cambió."""
    st.session_state["bit_ver"] = st.session_state.get("bit_ver", 0) + 1


def _tab_bitacora(sb, cuenta: dict, periodo: dict, cerrado: bool, cierre: dict | None = None) -> None:
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

    periodos = st.session_state.get("_periodos") or [periodo]
    cierre = cierre if cierre is not None else {}
    _seccion_registros_bitacora(sb, cuenta, periodos, periodo, cierre)
    _seccion_estado_cuenta_bitacora(sb, cuenta, periodos, periodo, cierre)


def _chips(registros: list[dict], estados: tuple[str, ...]) -> str:
    cuenta = collections.Counter(r["estado"] for r in registros)
    movs_comp = sum(r.get("movs", 0) for r in registros if r["estado"] == "comprobado")
    chips = []
    for est in estados:
        fondo, texto = ESTILO_ETIQUETA[est]
        borde = "#CBD5E1" if est == "no_necesario" else texto
        etiqueta = (f'{cuenta.get(est, 0)} gastos comprobados · {movs_comp} movimientos del banco' if est == "comprobado"
                    else f'{cuenta.get(est, 0)} {reportes.ETIQUETA_REGISTRO[est].lower()}')
        chips.append(f'<span style="display:inline-block;background:{fondo};color:{texto};border:1px solid {borde};border-radius:999px;'
                     f'padding:2px 12px;margin:0 6px 4px 0;font-size:.85rem;font-weight:700">{etiqueta}</span>')
    return "".join(chips)


def _tabla_registros(vista: list[dict], mes_de: dict, columnas: list[str], clave: str, multi: bool = True) -> list[int]:
    """Pinta la tabla de registros y devuelve las filas seleccionadas (una o varias)."""
    tabla = pd.DataFrame([{
        "Estado": reportes.ETIQUETA_REGISTRO[r["estado"]], "Mes": mes_de.get(r["id"], ""), "Fecha": r["fecha"], "#": r["numero"],
        "Descripción": r["descripcion"], "Concepto": r["concepto"], "Empleado": r["empleado"], "Categoría": r["categoria"],
        "Mov.": r["movs"], "Monto": r["monto"], "Situación": r["situacion"]} for r in vista])[columnas]
    if "#" in tabla:
        tabla["#"] = tabla["#"].astype("Int64")
    inverso = {v: k for k, v in reportes.ETIQUETA_REGISTRO.items()}

    def _pintar(col):
        return [f"background-color:{ESTILO_ETIQUETA[inverso[v]][0]};color:{ESTILO_ETIQUETA[inverso[v]][1]};font-weight:700;text-align:center" for v in col]

    estilo = tabla.style.apply(_pintar, subset=["Estado"]).format({"Monto": "${:,.2f}"}, na_rep="—")
    ev = st.dataframe(estilo, hide_index=True, use_container_width=True, height=min(36 * (len(tabla) + 1) + 4, 400),
                      on_select="rerun", selection_mode="multi-row" if multi else "single-row",
                      key=f"{clave}_{st.session_state.get('bit_ver', 0)}_{_firma(r['id'] for r in vista)}",
                      column_config={"#": st.column_config.NumberColumn("#", format="%d", width="small"),
                                     "Estado": st.column_config.TextColumn("Estado", width="small"),
                                     "Mov.": st.column_config.NumberColumn("Mov.", format="%d", width="small", help="Movimientos del estado de cuenta que cubre el gasto")})
    return [i for i in (ev.selection.rows if ev and ev.selection else []) if i < len(vista)]


def _seccion_registros_bitacora(sb, cuenta: dict, periodos: list[dict], periodo: dict, cierre: dict) -> None:
    """Solicitudes que el aplicante registró en la bitácora (de cualquier mes), con su estado de comprobación."""
    por_periodo = {p["id"]: p for p in periodos}
    etiquetas = {p["id"]: db.etiqueta_periodo(p) for p in periodos}
    with st.expander("📒 Registros de la bitácora", expanded=True):
        st.caption("Lo que tú registraste como solicitud. Aquí compruebas cada registro con su cargo del estado de cuenta.")
        m1, m2, m3 = st.columns([1.2, 1.3, 3])
        opciones = ["Todos los meses", *[etiquetas[p["id"]] for p in periodos]]
        mes = m1.selectbox("Mes", opciones, key="bit_mes", help="Por omisión se ven todos los meses para que no se te pierda ningún registro.")
        filtro = m2.selectbox("Mostrar", ["Todos", "Sin comprobar", "Comprobado"], key="bit_filtro")
        texto = m3.text_input("🔎 Buscar (descripción, concepto, empleado, categoría, #)", key="bit_busca")
        id_mes = None if mes == "Todos los meses" else next(p["id"] for p in periodos if etiquetas[p["id"]] == mes)
        sols = db.listar_solicitudes(sb, cuenta["id"], id_mes)
        gastos = db.listar_gastos(sb, [x["id"] for x in sols]) if sols else []
        registros = reportes.registros_bitacora(sols, gastos, pd.DataFrame())
        if not registros:
            st.caption("Aún no hay registros en esta selección. Crea una solicitud con «➕ Nueva solicitud».")
            return
        mes_de = {x["id"]: etiquetas.get(x.get("periodo_id"), "") for x in sols}
        st.markdown(f"**{len(registros)} registro(s)** · {mes}")
        st.markdown(_chips(registros, ("sin_comprobar", "comprobado")), unsafe_allow_html=True)
        vista = registros
        if filtro != "Todos":
            vista = [r for r in vista if r["estado"] == {"Sin comprobar": "sin_comprobar", "Comprobado": "comprobado"}[filtro]]
        if texto.strip():
            t = texto.strip().lower()
            vista = [r for r in vista if t in " ".join(str(r[k]) for k in ("descripcion", "concepto", "empleado", "categoria", "numero")).lower()]
        if not vista:
            st.caption("Ningún registro coincide con el filtro.")
            return
        cols = ["Estado", "Mes", "Fecha", "#", "Descripción", "Concepto", "Empleado", "Categoría", "Mov.", "Monto", "Situación"]
        filas = _tabla_registros(vista, mes_de, cols, "tabla_bit")
        if not filas:
            st.caption("Haz clic en una fila para abrir el registro y ver sus detalles (gasto, facturas y PDF) o comprobarlo. "
                       "Marca varias casillas para seleccionar más de un registro.")
            return
        gastos_por_sol: dict[str, list[dict]] = {}
        for g in gastos:
            gastos_por_sol.setdefault(g["solicitud_id"], []).append(g)
        if len(filas) > 1:
            _acciones_varios_registros(sb, [vista[i] for i in filas], {x["id"]: x for x in sols}, gastos_por_sol, cierre)
            return
        sel = vista[filas[0]]
        sol = next(x for x in sols if x["id"] == sel["id"])
        per_r = por_periodo.get(sol.get("periodo_id"), periodo)
        _detalle_registro(sb, cuenta, per_r, cierre.get(per_r["id"]) == "cerrado", sel, {x["id"]: x for x in sols}, gastos_por_sol, pd.DataFrame())


def _acciones_varios_registros(sb, sel: list[dict], sols: dict, gastos_por_sol: dict, cierre: dict) -> None:
    """Acciones cuando se seleccionan 2 o más registros (solicitudes) de la bitácora."""
    ss = [sols[r["id"]] for r in sel if r["id"] in sols]
    total = round(sum(float(x.get("monto_tentativo") or 0) for x in ss), 2)
    with st.container(border=True):
        st.markdown(f"**{len(ss)} registros seleccionados** (#{', #'.join(str(x['numero']) for x in ss)}) · monto tentativo {money_md(total)}")
        enviables = [x for x in ss if x["estado"] in EDITABLES and gastos_por_sol.get(x["id"]) and cierre.get(x.get("periodo_id")) != "cerrado"]
        borrables = [x for x in ss if x["estado"] in EDITABLES and not gastos_por_sol.get(x["id"]) and cierre.get(x.get("periodo_id")) != "cerrado"]
        b1, b2, _ = st.columns([1.6, 1.6, 3])
        if b1.button(f"📤 Enviar a revisión ({len(enviables)})", type="primary", disabled=not enviables, key="bit_env_varios",
                     help="Solo se envían los que ya tienen gasto comprobado."):
            errores = []
            for x in enviables:
                try:
                    db.enviar_a_revision(sb, x["id"])
                except db.ErrorNube as e:
                    errores.append(f"#{x['numero']}: {e}")
            _nueva_tabla()
            if errores:
                st.error("No se pudieron enviar:\n- " + "\n- ".join(errores))
            else:
                _flash(f"{len(enviables)} solicitud(es) enviada(s) a revisión.")
                st.rerun()
        if b2.button(f"🗑️ Eliminar ({len(borrables)})", disabled=not borrables, key="bit_del_varios",
                     help="Solo se eliminan los que todavía no tienen gasto comprobado."):
            errores = []
            for x in borrables:
                try:
                    db.eliminar_solicitud(sb, x["id"])
                except db.ErrorNube as e:
                    errores.append(f"#{x['numero']}: {e}")
            _nueva_tabla()
            if errores:
                st.error("No se pudieron eliminar:\n- " + "\n- ".join(errores))
            else:
                st.rerun()
        st.caption("Para ver el detalle de un registro, comprobarlo o editarlo, deja seleccionado uno solo.")


def _acciones_varios_cargos(sb, cuenta: dict, periodo: dict, ids: list[str], movs: pd.DataFrame, cierre: dict) -> None:
    """Acciones cuando se seleccionan 2 o más cargos del estado de cuenta en la bitácora."""
    fila = movs[movs["id"].isin(ids)]
    if fila.empty:
        st.warning("Los movimientos ya no están en la lista; vuelve a seleccionar.")
        return
    total = round(float(fila["monto"].abs().sum()), 2)
    cerrados = [p for p in fila["periodo_id"].unique() if cierre.get(p) == "cerrado"]
    abiertos = fila[fila["estado"].isin(["pendiente", "en_espera"])]
    with st.container(border=True):
        st.markdown(f"**{len(fila)} cargos seleccionados** · total {money_md(total)}")
        if cerrados:
            st.info("Algún cargo pertenece a un mes cerrado; no se puede modificar.")
            return
        b0, b1, b2 = st.columns([1.8, 1.6, 2])
        mismo_mes = fila["periodo_id"].nunique() == 1
        if b0.button(f"🔗 Comprobar juntos ({len(fila)})", type="primary", key="bit_cmp_varios",
                     disabled=len(abiertos) != len(fila) or not mismo_mes,
                     help="Los cargos comparten las mismas facturas (un solo gasto)."):
            _nueva_tabla()
            _comprobar_desde_estado(sb, cuenta, periodo, fila)
        if b1.button(f"🚫 No necesarios ({len(abiertos)})", key="bit_nn_varios", disabled=abiertos.empty):
            _nueva_tabla()
            _cambiar(sb, abiertos["id"].tolist(), "no_necesario")
        regresables = fila[fila["estado"].isin(["no_necesario", "en_espera"])]
        if b2.button(f"↩️ Regresar a sin comprobar ({len(regresables)})", key="bit_sc_varios", disabled=regresables.empty):
            _nueva_tabla()
            _cambiar(sb, regresables["id"].tolist(), "pendiente")
        if len(abiertos) != len(fila):
            st.caption("«Comprobar juntos» requiere que todos los cargos seleccionados estén sin comprobar o pendientes.")
        elif not mismo_mes:
            st.caption("«Comprobar juntos» requiere que los cargos sean del mismo mes.")


def _seccion_estado_cuenta_bitacora(sb, cuenta: dict, periodos: list[dict], periodo: dict, cierre: dict) -> None:
    """Cargos del banco que todavía no están comprobados (sin comprobar, pendiente, no necesario)."""
    por_periodo = {p["id"]: p for p in periodos}
    etiquetas = {p["id"]: db.etiqueta_periodo(p) for p in periodos}
    with st.expander("🏦 Estado de cuenta del banco", expanded=True):
        st.caption("Cargos del banco que aún no están comprobados. Los ya comprobados aparecen arriba, dentro de su registro de la bitácora.")
        m1, m2, m3 = st.columns([1.2, 1.3, 3])
        opciones = [*[etiquetas[p["id"]] for p in periodos], "Todos los meses"]
        mes = m1.selectbox("Mes", opciones, index=_idx(f"bit_mes_ec_{periodo['id']}", opciones.index(etiquetas[periodo["id"]]) if periodo["id"] in etiquetas else 0),
                           key=f"bit_mes_ec_{periodo['id']}")
        filtro = m2.selectbox("Mostrar", ["Todos", *[reportes.ETIQUETA_REGISTRO[e] for e in ("sin_comprobar", "pendiente", "no_necesario")]], key="bit_filtro_ec")
        texto = m3.text_input("🔎 Buscar por descripción o monto", key="bit_busca_ec")
        id_mes = None if mes == "Todos los meses" else next(p["id"] for p in periodos if etiquetas[p["id"]] == mes)
        movs = db.listar_movimientos(sb, cuenta["id"], periodo_id=id_mes)
        registros = reportes.registros_bitacora([], [], movs)
        if not registros:
            st.caption("No hay cargos sin comprobar en esta selección. Carga el estado de cuenta en la sección «Estado de cuenta».")
            return
        mes_de = {r.id: etiquetas.get(r.periodo_id, "") for r in movs.itertuples()}
        st.markdown(f"**{len(registros)} cargo(s)** · {mes}")
        st.markdown(_chips(registros, ("sin_comprobar", "pendiente", "no_necesario")), unsafe_allow_html=True)
        vista = registros
        if filtro != "Todos":
            clave = {v: k for k, v in reportes.ETIQUETA_REGISTRO.items()}[filtro]
            vista = [r for r in vista if r["estado"] == clave]
        if texto.strip():
            t = texto.strip().lower()
            vista = [r for r in vista if t in f"{r['descripcion']} {r['monto']:.2f} {r['monto']:,.2f}".lower()]
        if not vista:
            st.caption("Ningún cargo coincide con el filtro.")
            return
        cols = ["Estado", "Mes", "Fecha", "Descripción", "Monto", "Situación"]
        filas = _tabla_registros(vista, mes_de, cols, "tabla_bitec")
        if not filas:
            st.caption("Haz clic en un cargo para comprobarlo, marcarlo como no necesario o regresarlo a sin comprobar. "
                       "Marca varias casillas para seleccionar más de un cargo (por ejemplo, varios cargos de una misma factura).")
            return
        if len(filas) > 1:
            _acciones_varios_cargos(sb, cuenta, periodo, [vista[i]["id"] for i in filas], movs, cierre)
            return
        sel = vista[filas[0]]
        pid = movs.loc[movs["id"] == sel["id"], "periodo_id"].iloc[0]
        per_r = por_periodo.get(pid, periodo)
        _detalle_registro(sb, cuenta, per_r, cierre.get(per_r["id"]) == "cerrado", sel, {}, {}, movs)


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
                + (f" · propina {money(reportes._propina_comprobada(g))}" if reportes._propina_comprobada(g) else ""))
    for m in g["movimientos"]:
        st.caption(f"🏦 {m['fecha']} · {m['descripcion']} · {money_md(m['monto'])}")
    _bloque_devolucion(sb, g, False)
    for f in g["facturas"]:
        cols = st.columns([3, 1, 1])
        cols[0].caption(f"{_etiqueta_factura(f)} · 🧾 {(f.get('uuid_cfdi') or 'SIN-UUID')} · {f.get('rfc_emisor') or ''} · {f.get('nombre_emisor') or ''} · {f.get('fecha_factura') or ''}")
        cols[1].caption(money_md(f["monto_aplicado"]))
        _boton_pdf(sb, f, f"ver_{g['id']}_{f['id']}", cols[2])
    if editable and st.button("↩️ Revertir este gasto", key=f"rev_{g['id']}"):
        try:
            db.revertir_gasto(sb, g["id"])
            _nueva_tabla()
            _flash("Gasto revertido: los movimientos volvieron a pendientes.")
            st.rerun()
        except db.ErrorNube as e:
            st.error(str(e))


def _boton_pdf(sb, f: dict, key: str, columna=None) -> None:
    """Botón que abre el PDF de la factura en una pestaña nueva (enlace temporal firmado de Supabase).
    Si no se puede generar el enlace, cae al visor integrado."""
    if not f.get("adjunto"):
        return
    destino = columna if columna is not None else st
    url = db.url_firmada(sb, f["adjunto"]["ruta"])
    if url:
        destino.link_button("📎 Abrir PDF", url)
    elif destino.button("📎 Ver", key=key):
        st.session_state.adj_ver = f["adjunto"]["ruta"]


def _etiqueta_factura(f: dict) -> str:
    return "📄 XML" if reportes.tipo_factura(f) == "xml" else "✍️ Manual"


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
# Selectores que deben conservar lo que el usuario eligió aunque cambie de sección: Streamlit borra el estado de un
# widget que no se dibuja en una ejecución (por ejemplo, el «Mes» de la bitácora al irte a «Estado de cuenta»).
# Reasignar su valor al inicio de cada ejecución evita esa limpieza (técnica documentada de Streamlit).
_CLAVES_FIJAS = ("mes_sel", "bit_mes", "bit_filtro", "bit_filtro_ec", "filtro_estado_mov", "alcance_mov",
                 "rev_cuenta", "rev_mes", "rev_lotes_alc")
_PREFIJOS_FIJOS = ("bit_mes_ec_", "cmp_mes_")


def _idx(clave: str, indice: int) -> int:
    """Índice inicial de un selectbox con key: si ya hay un valor guardado en la sesión no se pasa índice (evita el aviso de Streamlit)."""
    return 0 if clave in st.session_state else indice


def _conservar_filtros() -> None:
    for k in list(st.session_state.keys()):
        if isinstance(k, str) and (k in _CLAVES_FIJAS or k.startswith(_PREFIJOS_FIJOS)):
            st.session_state[k] = st.session_state[k]


def _rerun_ventana() -> None:
    """Vuelve a dibujar solo el contenido de la ventana «Comprobar gasto» (no la página de atrás), para que no se
    mueva el scroll ni se reinicien las secciones. Fuera de la ventana (o en versiones viejas de Streamlit) hace el rerun completo."""
    try:
        st.rerun(scope="fragment")
    except Exception:
        st.rerun()


def _abrir_ventana_comprobar(sb, cuenta: dict, sol: dict, tol: float) -> None:
    """Abre la comprobación en una ventana aparte (diálogo ancho). Cerrarla con la X equivale a «Volver»."""
    def _contenido() -> None:
        _pantalla_comprobar(sb, cuenta, sol, tol)
    try:
        deco = st.dialog("🔗 Comprobar gasto", width="large", on_dismiss=_limpiar_comprobacion)
    except TypeError:   # versión de Streamlit sin on_dismiss
        deco = st.dialog("🔗 Comprobar gasto", width="large")
    deco(_contenido)()


def _limpiar_comprobacion() -> None:
    for k in [k for k in st.session_state if k.startswith("cmp_") or k.startswith("adjf_")]:
        del st.session_state[k]
    st.session_state.pop("comprobando", None)


def _pantalla_comprobar(sb, cuenta: dict, sol: dict, tol: float) -> None:
    if st.button("← Cerrar y volver a la bitácora", key="volver_cmp"):
        _limpiar_comprobacion()
        st.rerun()
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
        hoy = hoy_cdmx()
        mes_actual = f"{hoy.year}-{hoy.month:02d}"
        mes_pref = st.session_state.get("mes_sel")   # el mes que el usuario tiene elegido en la barra lateral
        mes_def = mes_pref if isinstance(mes_pref, str) and len(mes_pref) == 7 else mes_actual
        meses = sorted({str(f)[:7] for f in pend["fecha"]} | {mes_actual, mes_def}, reverse=True)
        c0, c1, c2 = st.columns([1.1, 2, 1.3])
        mes_sel = c0.selectbox("Mes del estado de cuenta", [*meses, "Todos los meses"], index=_idx(f"cmp_mes_{sol.get('id') or 'nueva'}", meses.index(mes_def)),
                               key=f"cmp_mes_{sol.get('id') or 'nueva'}", help="Por omisión se muestra el mes elegido en la barra lateral; cámbialo si quedan pendientes de otro mes. Se conserva hasta que lo cambies.")
        texto = c1.text_input("🔎 Buscar por descripción", key="cmp_busca")
        cerca = c2.checkbox("Ordenar por cercanía al monto tentativo", value=float(sol["monto_tentativo"] or 0) > 0, key="cmp_cerca")
        vista = pend.copy()
        if mes_sel != "Todos los meses":
            vista = vista[vista["fecha"].astype(str).str[:7] == mes_sel]
            otros = len(pend) - len(vista)
            if vista.empty and otros:
                st.info(f"No hay movimientos pendientes en {mes_sel}. Hay {otros} en otros meses: cámbialo en «Mes del estado de cuenta».")
            elif otros:
                st.caption(f"Mostrando {mes_sel}. Hay {otros} movimiento(s) pendiente(s) en otros meses.")
        if texto.strip():
            vista = vista[vista["descripcion"].str.contains(texto.strip(), case=False, na=False)]
        if cerca and float(sol["monto_tentativo"] or 0) > 0:
            vista = vista.assign(_d=(vista["monto"].abs() - float(sol["monto_tentativo"])).abs()).sort_values(["_d", "fecha", "id"], kind="stable").drop(columns="_d")
        vista = vista.reset_index(drop=True)   # el orden viene fijo de la base de datos (fecha, creado_en, id)
        ev = st.dataframe(
            vista[["fecha", "descripcion", "monto", "saldo"]], hide_index=True, use_container_width=True,
            on_select="rerun", selection_mode="multi-row", key=f"cmp_tabla_{mes_sel}_{_firma(vista['id'])}",
            column_config={"fecha": "Fecha", "descripcion": "Descripción",
                           "monto": st.column_config.NumberColumn("Monto", format="$%.2f"), "saldo": st.column_config.NumberColumn("Saldo", format="$%.2f")})
        filas = [i for i in (ev.selection.rows if ev and ev.selection else []) if i < len(vista)]
        if filas:
            sel = vista.iloc[filas]
            st.info(f"{len(sel)} movimiento(s) seleccionado(s) · total {money_md(sel['monto'].abs().sum())}"
                    + (" · varios cargos comparten las mismas facturas" if len(sel) > 1 else ""))
            if st.button(f"Continuar con {len(sel)} movimiento(s)", type="primary", key="cmp_continuar"):
                st.session_state.cmp_movs = sel["id"].tolist()
                _rerun_ventana()
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
        _rerun_ventana()

    # ---------- Paso 2: facturas ----------
    st.markdown("#### Paso 2 · Agrega la(s) factura(s)")
    datos_sol_ui = None
    if not sol.get("id"):
        # Viene directo del banco: se piden TODOS los campos de la solicitud, igual que en «➕ Nueva solicitud».
        st.markdown("##### 🧍 Datos de la solicitud")
        st.caption("Los mismos campos que «Nueva solicitud»; ya vienen prellenados con lo que dice el banco. "
                   "La solicitud se crea hasta que guardas el gasto y queda en el mes del cargo del banco.")
        datos_sol_ui = _campos_solicitud(sb, "cmp_sol", {
            "fecha": sol["fecha"], "monto_tentativo": sol["monto_tentativo"], "description": sol.get("description"),
            "category": sol.get("category"), "material": sol.get("material")})
        categoria = datos_sol_ui["category"] or "—"
        material = datos_sol_ui["material"] or "—"
    else:
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
    tip_adj = None
    if categoria in CATEGORIAS_CON_PROPINA:
        propina = st.number_input("💵 Propina (no aparece en la factura)", min_value=0.0, step=1.0, format="%.2f", key="cmp_propina")
        if propina > 0:
            tip_adj = st.session_state.get("cmp_tip_adj")
            if tip_adj:
                t1, t2 = st.columns([4, 1])
                t1.caption(f"📎 Comprobante de la propina: {tip_adj[1]['nombre_original']} · {tip_adj[1]['bytes_final'] / 1024:,.0f} KB")
                if t2.button("Quitar", key="cmp_tip_quitar"):
                    st.session_state.pop("cmp_tip_adj", None)
                    st.session_state["cmp_tip_ver"] = st.session_state.get("cmp_tip_ver", 0) + 1
                    _rerun_ventana()
            else:
                archivo_tip = st.file_uploader("📎 Comprobante de la propina (PDF o foto del recibo / voucher)", type=["pdf", "jpg", "jpeg", "png"],
                                               key=f"cmp_tip_up_{st.session_state.get('cmp_tip_ver', 0)}")
                if archivo_tip is not None:
                    try:
                        st.session_state["cmp_tip_adj"] = adj.comprimir_a_pdf(archivo_tip.getvalue(), archivo_tip.name)
                        _rerun_ventana()
                    except adj.AdjuntoError as e:
                        st.error(str(e))
                st.warning(f"Sin comprobante de la propina, esos {money_md(propina)} se marcarán como **«tip sin comprobación»**: "
                           "se separan del gasto y el colaborador deberá devolverlos a la cuenta de la empresa.")

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
        _rerun_ventana()

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
                    _rerun_ventana()

    if facturas:
        st.markdown("##### 🧾 Facturas de este gasto")
    for f in list(facturas):
        d = f["datos"]
        with st.container(border=True, key=f"cmp_tarjeta_{f['k']}"):
            cols = st.columns([3, 1.4, 1.1, 1.1, 0.5])
            cols[0].markdown(f"**{d.get('Archivo', '')}** · `{d['UUID']}`  \n{d.get('RFC Emisor', '')} · {d.get('Concepto', '')}")
            cols[1].write(d.get("Fecha Factura", ""))
            cols[2].write(f"IVA {money_md(d.get('IVA'))}")
            cols[3].write(f"**{money_md(d['Monto Total'])}**")
            if cols[4].button("✕", key=f"cmp_quitar_{f['k']}", help="Quitar factura"):
                facturas.remove(f)
                _rerun_ventana()
            if f["adjunto"]:
                info = f["adjunto"][1]
                a1, a2 = st.columns([4, 1])
                a1.caption(f"📎 {info['nombre_original']} · {info['bytes_original'] / 1024:,.0f} KB → {info['bytes_final'] / 1024:,.0f} KB")
                if a2.button("Quitar PDF", key=f"cmp_qadj_{f['k']}"):
                    f["adjunto"] = None
                    f["adj_ver"] += 1
                    _rerun_ventana()
            else:
                archivo = st.file_uploader("📎 Adjuntar PDF o foto de la factura", type=["pdf", "jpg", "jpeg", "png"], key=f"adjf_{f['k']}_{f['adj_ver']}")
                if archivo is not None:
                    try:
                        pdf, info = adj.comprimir_a_pdf(archivo.getvalue(), archivo.name)
                        f["adjunto"] = (pdf, info)
                        _rerun_ventana()
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
    # Función especial: cotización en dólares y factura en pesos → la diferencia por tipo de cambio puede ser mayor a la tolerancia.
    dif_tc = st.toggle("💱 Facturas con diferencias de tipo de cambio", key="cmp_dif_tc",
                       help="Úsalo solo cuando la cotización fue en dólares y la factura en pesos. Permite una diferencia mayor a "
                            f"{money_md(tol)} y exige explicar el motivo en «Detalles».")
    detalle_tc = ""
    if dif_tc:
        detalle_tc = st.text_area("Detalles (obligatorio): razón de la diferencia", key="cmp_dif_tc_det", height=90,
                                  placeholder="Ej.: cotización en USD 500 a T.C. 17.20; la factura se emitió en pesos a T.C. 18.05 (DOF del día de emisión).")
        st.info("Este gasto se guardará marcado como **diferencia por tipo de cambio**: el revisor verá tu explicación y podrá darle visto bueno aunque la diferencia sea mayor a "
                f"{money_md(tol)}.")
        if not detalle_tc.strip():
            st.warning("Escribe en «Detalles» por qué hay diferencia para poder guardar.")
            return
    elif abs(diferencia) > tol:
        st.warning(f"La diferencia debe ser de máximo {money_md(tol)} para guardar el gasto. "
                   "Si se debe al tipo de cambio (cotización en dólares, factura en pesos), activa «Facturas con diferencias de tipo de cambio».")
        return
    elif abs(diferencia) >= 0.005:
        st.info(f"Se guardará con una diferencia de {money_md(diferencia)} (dentro de la tolerancia); el revisor la verá resaltada.")

    if st.button("💾 Guardar gasto", type="primary", key="cmp_guardar"):
        subidos: list[str] = []
        sol_nueva_id = None
        try:
            if not sol.get("id"):  # viene del estado de cuenta: la solicitud se crea hasta guardar el gasto
                datos_sol = {**sol["_datos"], **(datos_sol_ui or {})}
                for tipo, clave in (("categoria", "category"), ("empleado", "employee_name"), ("material", "material")):
                    _guardar_catalogo_si_nuevo(sb, tipo, datos_sol.get(clave))
                sol_nueva_id = db.crear_solicitud(sb, cuenta["id"], sol["_periodo_id"], datos_sol)["id"]
            mes = str(sol["fecha"])[:7]
            propina_adjunto = None
            if tip_adj and propina > 0:
                pdf_t, info_t = tip_adj
                ruta_t = db.subir_adjunto(sb, cuenta["id"], mes, pdf_t)
                subidos.append(ruta_t)
                propina_adjunto = {"ruta": ruta_t, **{k: info_t[k] for k in ("nombre_original", "bytes_original", "bytes_final", "sha256")}}
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
                             None if material == "—" else material, propina, payload,
                             dif_tc=bool(dif_tc), detalle_tc=detalle_tc.strip() if dif_tc else None,
                             propina_adjunto=propina_adjunto)
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
        _nueva_tabla()
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
        archivo = st.file_uploader("Archivo (CSV, Excel o PDF)", type=["csv", "xlsx", "xls", "pdf"], key="up_estado",
                                   help="PDF: debe traer texto (en BBVAnet: Ctrl+P → Destino «Guardar como PDF»). Se validan los saldos renglón por renglón.")
        df_pdf, error_pdf = None, None
        if archivo is not None and archivo.name.lower().endswith(".pdf"):
            try:
                df_pdf = pdf_estado.leer_pdf_estado(archivo.getvalue())
                if df_pdf.attrs.get("validado"):
                    st.success(f"✔ PDF leído: {len(df_pdf)} movimientos del {df_pdf['Fecha'].min()} al {df_pdf['Fecha'].max()} · "
                               "los saldos cuadran renglón por renglón.")
                else:
                    st.warning(" ".join(df_pdf.attrs.get("avisos", [])))
                st.caption(f"Cargos {money_md(df_pdf['Cargo'].sum())} · abonos {money_md(df_pdf['Abono'].sum())}. "
                           f"El PDF no trae el año: se tomó de la fecha de impresión ({df_pdf.attrs['fecha_impresion']}). "
                           "Revisa la vista previa antes de cargar.")
                st.dataframe(df_pdf[["Fecha", "Descripción", "Cargo", "Abono", "Saldo"]], hide_index=True, use_container_width=True,
                             height=min(36 * (len(df_pdf) + 1) + 4, 300),
                             column_config={c: st.column_config.NumberColumn(c, format="$%.2f") for c in ("Cargo", "Abono", "Saldo")})
            except pdf_estado.ErrorPDF as e:
                error_pdf = str(e)
                st.error("No se pudo leer el PDF:\n\n" + error_pdf)
        if archivo is not None and error_pdf is None and st.button("Cargar movimientos", type="primary", key="btn_cargar_estado"):
            try:
                df = df_pdf if df_pdf is not None else _leer_estado(archivo.getvalue(), archivo.name, cuenta["formato"])
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
                      key=f"tabla_mov_{st.session_state.get('bit_ver', 0)}_{periodo['id']}_{estado}_{alcance}_{_firma(vista['id'])}",
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
    _lotes_pago_aplicante(sb, cuenta, periodos, None, "tab")


# ======================================================================
# Pestaña: Enviados a comprobación (todo el estado de cuenta con su avance)
# ======================================================================
ICONO_MOV = {"comprobado": "🟢", "no_necesario": "⚪", "pendiente": "🟠", "en_espera": "🟣"}
NOMBRE_MOV = {"comprobado": "Comprobado", "no_necesario": "No necesario", "pendiente": "Sin comprobar", "en_espera": "Pendiente"}
NOMBRE_LOTE = {"por_pagar": "⏳ Por pagar", "pagado": "✅ Pagado", "anulado": "↩️ Anulado"}
POR_PAGINA = 25


def _pagado_de(l: dict) -> float:
    """Cuánto lleva pagado un lote (un lote pagado cuenta completo aunque no exista la columna monto_pagado)."""
    if l.get("estado") == "pagado":
        return float(l["monto"])
    return float(l.get("monto_pagado") or 0)


def _saldo_de(l: dict) -> float:
    return round(float(l["monto"]) - _pagado_de(l), 2) if l.get("estado") != "anulado" else 0.0


def _estado_lote(l: dict) -> str:
    if l.get("estado") == "por_pagar" and _pagado_de(l) > 0:
        return "🟡 Pago parcial"
    return NOMBRE_LOTE.get(l.get("estado"), str(l.get("estado")))


def _fecha_cdmx(ts: str) -> str:
    t = pd.to_datetime(ts, utc=True).tz_convert("America/Mexico_City")
    return t.strftime("%Y-%m-%d %H:%M")


def periodos_todos() -> list[dict]:
    return st.session_state.get("_periodos", [])


def _lotes_de_envio(env_g: list[dict], envio_id: str) -> str:
    """«Lote 1 · pagado, Lote 2 · por pagar» (los lotes de pago del revisor donde cayeron los gastos de este envío)."""
    vistos: dict[int, str] = {}
    for r in env_g:
        if r["envio_id"] == envio_id and r.get("lote_numero") is not None and r.get("resultado") == "aprobado":
            vistos[int(r["lote_numero"])] = "pagado" if r.get("lote_estado") == "pagado" else "por pagar"
    return ", ".join(f"Lote {n} · {e}" for n, e in sorted(vistos.items())) or "—"


def _estatus_envio(env_g: list[dict], envio_id: str) -> str:
    n = collections.Counter()
    for r in env_g:
        if r["envio_id"] != envio_id:
            continue
        if r["resultado"] == "aprobado":
            n["pagado" if r.get("lote_estado") == "pagado" else "por pagar"] += 1
        elif r["resultado"] == "devuelto":
            n["devuelto"] += 1
        else:
            n["en revisión"] += 1
    return " · ".join(f"{n[k]} {k}" for k in ("pagado", "por pagar", "en revisión", "devuelto") if n[k]) or "—"


def _lotes_pago_aplicante(sb, cuenta: dict, periodos: list[dict], periodo: dict | None, clave: str) -> None:
    """Lotes de pago de la cuenta (de un mes o de todos): estado y si ya se pagó; al abrir uno se ven sus gastos."""
    lotes = db.listar_lotes(sb, cuenta["id"])
    if periodo is not None:
        lotes = [l for l in lotes if l["periodo_id"] == periodo["id"]]
    if not lotes:
        st.caption("Todavía no hay lotes de pago." if periodo is None else "Todavía no hay lotes de pago este mes: se crean cuando el revisor da el visto bueno.")
        return
    mes = {p["id"]: db.etiqueta_periodo(p) for p in periodos}
    pagado = sum(_pagado_de(l) for l in lotes)
    por_pagar = sum(_saldo_de(l) for l in lotes if l["estado"] == "por_pagar")
    k = st.columns(2)
    k[0].markdown(estilos.tarjeta("Por pagar (saldo)", str(sum(1 for l in lotes if l["estado"] == "por_pagar")), estilos.money_html(por_pagar), "alerta"), unsafe_allow_html=True)
    k[1].markdown(estilos.tarjeta("Pagado", str(sum(1 for l in lotes if _pagado_de(l) > 0)), estilos.money_html(pagado), "comprobado"), unsafe_allow_html=True)
    tabla = pd.DataFrame([{
        "Lote": f"{mes.get(l['periodo_id'], '')}-{l['numero']}", "Cerrado": str(l["cerrado_en"])[:10], "Gastos": l["num_gastos"], "Monto": float(l["monto"]),
        "Pagado": _pagado_de(l), "Saldo": _saldo_de(l),
        "Estado": _estado_lote(l), "Fecha de pago": l.get("fecha_pago") or "", "Referencia": l.get("referencia_pago") or ""}
        for l in lotes])
    ev = st.dataframe(tabla, hide_index=True, use_container_width=True, height=min(36 * (len(tabla) + 1) + 4, 300),
                      on_select="rerun", selection_mode="single-row", key=f"tabla_lpa_{clave}_{st.session_state.get('bit_ver', 0)}_{periodo['id'] if periodo else 'todos'}_{_firma(l['id'] for l in lotes)}",
                      column_config={c: st.column_config.NumberColumn(c, format="$%.2f") for c in ("Monto", "Pagado", "Saldo")})
    sel = [i for i in (ev.selection.rows if ev and ev.selection else []) if i < len(lotes)]
    if not sel:
        st.caption("Haz clic en un lote para ver los gastos que incluye.")
        return
    lote = lotes[sel[0]]
    gastos = db.gastos_de_lote(sb, lote["id"])
    with st.container(border=True):
        st.markdown(f"**Lote {mes.get(lote['periodo_id'], '')}-{lote['numero']}** · {_estado_lote(lote)} · {money_md(lote['monto'])}")
        if lote["estado"] == "pagado":
            st.success(f"Pagado el {lote.get('fecha_pago')} · referencia {lote.get('referencia_pago')}")
        elif _pagado_de(lote) > 0:
            st.info(f"Pago parcial: se han pagado {money_md(_pagado_de(lote))} de {money_md(lote['monto'])}. Saldo pendiente: {money_md(_saldo_de(lote))}.")
        _historial_pagos(sb, lote)
        if gastos:
            st.dataframe(pd.DataFrame([{"Sol. #": g["solicitud"].get("numero"), "Descripción": g["solicitud"].get("description") or "",
                                        "Empleado": g["solicitud"].get("employee_name") or "", "Categoría": g.get("categoria") or "",
                                        "Cargo": float(g["monto_estado"])} for g in gastos]),
                         hide_index=True, use_container_width=True, column_config={"Cargo": st.column_config.NumberColumn("Cargo", format="$%.2f")})


def _lote_por_enviar(sb, periodo: dict, cerrado: bool, por_enviar: pd.DataFrame, sols: list[dict], n_lote: int) -> None:
    """Tabla del lote por enviar con casilla «Enviar» por solicitud: se mandan solo las marcadas."""
    num_a_id = {int(x["numero"]): x["id"] for x in sols}
    ps = por_enviar.groupby("Solicitud", as_index=False).agg(
        Fecha=("Fecha", "min"), Empleado=("Empleado", "first"), Categoría=("Categoría", "first"), Descripción=("Descripción", "first"),
        Gastos=("Monto", "size"), Movimientos=("Movimientos", "sum"), Facturas=("Facturas", "sum"), Monto=("Monto", "sum"),
        Diferencia=("Diferencia", "sum"), Devuelto=("Devuelto", "max")).sort_values("Solicitud").reset_index(drop=True)
    total = float(ps["Monto"].sum())
    k = st.columns(3)
    k[0].markdown(estilos.tarjeta(f"Lote #{n_lote} disponible", str(int(ps["Gastos"].sum())), "gasto(s) comprobado(s)", "acento"), unsafe_allow_html=True)
    k[1].markdown(estilos.tarjeta("Solicitudes", str(len(ps)), "disponibles para enviar", "acento"), unsafe_allow_html=True)
    k[2].markdown(estilos.tarjeta("Total disponible", estilos.money_html(total), "monto de los cargos", "rojo"), unsafe_allow_html=True)

    ver = st.session_state.setdefault("env_ver", 0)
    marca_ini = st.session_state.get("env_marca_ini", True)
    b1, b2, b3 = st.columns([1.2, 1.2, 4])
    if b1.button("☑️ Marcar todas", key="env_todas", disabled=cerrado):
        st.session_state.env_ver = ver + 1
        st.session_state.env_marca_ini = True
        st.rerun()
    if b2.button("⬜ Quitar marcas", key="env_ninguna", disabled=cerrado):
        st.session_state.env_ver = ver + 1
        st.session_state.env_marca_ini = False
        st.rerun()
    b3.caption("Marca las solicitudes que sí quieres mandar a revisión; las demás se quedan aquí para el siguiente lote.")

    editor = ps.copy()
    editor.insert(0, "Enviar", marca_ini)
    editado = st.data_editor(
        editor, hide_index=True, use_container_width=True, disabled=[c for c in editor.columns if c != "Enviar"],
        key=f"env_editor_{ver}_{_firma(ps['Solicitud'])}",
        column_config={"Enviar": st.column_config.CheckboxColumn("Enviar", help="Marca para incluirla en este lote", width="small"),
                       "Monto": st.column_config.NumberColumn("Monto", format="$%.2f"),
                       "Diferencia": st.column_config.NumberColumn("Diferencia", format="$%.2f"),
                       "Solicitud": st.column_config.NumberColumn("Solicitud", format="%d")})
    marcadas = editado[editado["Enviar"]]
    ids = [num_a_id[int(n)] for n in marcadas["Solicitud"] if int(n) in num_a_id]
    n_gastos = int(marcadas["Gastos"].sum()) if len(marcadas) else 0
    monto_sel = float(marcadas["Monto"].sum()) if len(marcadas) else 0.0
    st.markdown(f"**Seleccionadas: {len(marcadas)} de {len(ps)} solicitudes** · {n_gastos} gasto(s) · {money_md(monto_sel)}")
    if len(marcadas) < len(ps):
        st.caption(f"{len(ps) - len(marcadas)} solicitud(es) sin marcar no se enviarán y seguirán disponibles para el siguiente lote.")
    st.caption("Al mandar a revisión el lote queda registrado con su fecha y su total; no se puede editar, solo el revisor puede devolver un gasto.")
    nota = st.text_input("Nota para el revisor (opcional)", key="env_nota", disabled=cerrado)
    if st.button(f"📤 Mandar a revisión · Lote #{n_lote} · {len(marcadas)} solicitud(es) · {money_md(monto_sel)}", type="primary",
                 key="btn_enviar_ec", disabled=cerrado or not ids):
        try:
            db.enviar_estado_cuenta(sb, periodo["id"], nota, None if len(ids) == len(ps) else ids)
            st.session_state.pop("env_nota", None)
            st.session_state.pop("xl_mes", None)
            st.session_state.pop("env_marca_ini", None)
            _flash(f"Lote #{n_lote} enviado a revisión ({len(ids)} solicitud(es)).")
            st.rerun()
        except db.ErrorNube as e:
            st.error(str(e))
    if not ids:
        st.caption("Marca al menos una solicitud para poder enviar.")


def _tab_enviados(sb, cuenta: dict, periodo: dict, cerrado: bool) -> None:
    df = db.listar_movimientos_revision(sb, cuenta["id"], periodo["id"])
    if df.empty:
        st.info("Este mes todavía no tiene movimientos. Carga el estado de cuenta en la pestaña «Estado de cuenta».")
        return
    movs = df.drop_duplicates("id")
    _sols_c = db.listar_solicitudes(sb, cuenta["id"], periodo["id"])
    cu = reportes.cuadre(reportes.con_devolucion(movs, db.listar_gastos(sb, [x["id"] for x in _sols_c]) if _sols_c else []))
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
            _lote_por_enviar(sb, periodo, cerrado, por_enviar, sols, n_lote)
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
            "Lote(s) de pago": _lotes_de_envio(env_g, e["envio_id"]), "Estatus": _estatus_envio(env_g, e["envio_id"]),
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

    # ---- Lotes de pago: los mismos que ve el revisor ----
    st.markdown("##### 💸 Lotes de pago (los mismos que ve el revisor)")
    _lotes_pago_aplicante(sb, cuenta, periodos_todos(), periodo, "enviados")

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
                      key=f"tabla_env_{st.session_state.get('bit_ver', 0)}_{filtro}_{texto}_{_firma(r['tipo'] + str(r['id']) for r in vista_d)}",
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
        _detalle_solicitud_enviada(sb, df, r, gastos_por_id)


def _corregir_fuente(sb, f: dict, gasto_id: str, columna) -> None:
    """El revisor corrige si la factura es XML o manual (queda en bitácora)."""
    actual = reportes.tipo_factura(f)
    nuevo = "manual" if actual == "xml" else "xml"
    with columna.popover(f"🔁 a {'Manual' if nuevo == 'manual' else 'XML'}"):
        st.caption(f"Hoy figura como **{'XML' if actual == 'xml' else 'Manual'}**. Corrígela si no es correcto; queda registrado.")
        mot = st.text_input("Motivo", key=f"fte_m_{gasto_id}_{f['id']}")
        if st.button(f"Marcar como {'manual' if nuevo == 'manual' else 'XML'}", key=f"fte_b_{gasto_id}_{f['id']}"):
            try:
                db.reclasificar_fuente_facturas(sb, [f["id"]], nuevo, mot)
                _nueva_tabla_rev()
                _flash("Tipo de comprobación corregido.")
                st.rerun()
            except db.ErrorNube as e:
                st.error(str(e))


def _detalle_solicitud_enviada(sb, df: pd.DataFrame, r: dict, gastos_por_id: dict, revisor: bool = False) -> None:
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
            if g.get("dif_tc"):
                st.warning(f"💱 **Factura con diferencia de tipo de cambio** (cotización en dólares, factura en pesos). "
                           f"Detalles del aplicante: {g.get('detalle_tc') or '—'}")
        for g in gs:
            _bloque_devolucion(sb, g, revisor)
        for g in gs:
            for f in g.get("facturas") or []:
                cols = st.columns([3.2, 1.2, 1, 1.1] if revisor else [3.2, 1.2, 1])
                cols[0].caption(f"{_etiqueta_factura(f)} · 🧾 {f.get('uuid_cfdi') or 'SIN-UUID'} · {f.get('rfc_emisor') or ''} · {f.get('nombre_emisor') or ''} · {f.get('fecha_factura') or ''}")
                cols[1].caption(money_md(f["monto_aplicado"]))
                _boton_pdf(sb, f, f"venv_{g['id']}_{f['id']}", cols[2])
                if revisor:
                    _corregir_fuente(sb, f, g["id"], cols[3])


TIPO_DEVOLUCION = {"tip_sin_comprobacion": "Tip sin comprobación", "sin_comprobacion_devolucion": "Sin comprobación, devolución"}


def _bloque_devolucion(sb, g: dict, revisor: bool) -> None:
    """Propina (con o sin comprobante) y devoluciones del colaborador de un gasto; el revisor puede marcar una devolución."""
    propina = float(g.get("propina") or 0)
    if propina > 0 and g.get("tip_adjunto"):
        c1, c2 = st.columns([4, 1])
        c1.caption(f"✅ Propina de {money_md(propina)} con comprobante.")
        url = db.url_firmada(sb, g["tip_adjunto"]["ruta"])
        if url:
            c2.link_button("📎 Abrir", url)
    for d in g.get("devoluciones") or []:
        etiqueta = TIPO_DEVOLUCION.get(d["tipo"], d["tipo"])
        estado = "✅ ya devuelto" if d.get("estado") == "devuelto" else "pendiente de devolver"
        c1, c2 = st.columns([4.2, 1.3])
        c1.warning(f"💸 **{etiqueta}: {money_md(d['monto'])}** · {estado}. {d.get('nota') or ''}  \n"
                   "Este monto se separa del gasto (no se reembolsa) y el colaborador debe regresarlo a la cuenta de la empresa.")
        if revisor and d.get("estado") != "devuelto" and not g.get("lote_id"):
            with c2.popover("Quitar"):
                mot = st.text_input("Motivo (obligatorio)", key=f"dev_q_m_{d['id']}")
                if st.button("Quitar devolución", key=f"dev_q_b_{d['id']}"):
                    try:
                        db.anular_devolucion(sb, d["id"], mot)
                        _nueva_tabla_rev()
                        _flash("Devolución quitada.")
                        st.rerun()
                    except db.ErrorNube as e:
                        st.error(str(e))
    if revisor:
        libre = round(float(g["monto_estado"]) - float(g.get("devolucion_total") or 0), 2)
        if g.get("lote_id"):
            return
        if libre <= 0:
            return
        with st.popover("↩️ Marcar «sin comprobación, devolución»"):
            st.caption("Separa del gasto el monto que el colaborador debe regresar a la cuenta de la empresa. Puede ser todo el gasto o una parte; "
                       "ese monto ya no entra al lote de pago.")
            monto = st.number_input("Monto a devolver", min_value=0.0, max_value=libre, value=libre, step=10.0, format="%.2f", key=f"dev_m_{g['id']}")
            nota = st.text_area("Motivo (obligatorio)", key=f"dev_n_{g['id']}", height=80)
            if st.button("Marcar devolución", type="primary", key=f"dev_b_{g['id']}", disabled=monto <= 0):
                try:
                    db.marcar_devolucion(sb, g["id"], monto, nota)
                    _nueva_tabla_rev()
                    _flash(f"Devolución de {money_md(monto)} registrada: se separó del gasto.")
                    st.rerun()
                except db.ErrorNube as e:
                    st.error(str(e))


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
    _prop = reportes._propina_comprobada(g) if g else float(r["propina"] or 0)
    c.write(f"{r.get('categoria') or '—'}" + (f" · {r['material']}" if r.get("material") else ""))
    c.write(f"Cargo: {money_md(r['monto_estado'])}  \nFacturas: {money_md(r['suma_facturas'])}"
            + (f"  \nPropina: {money_md(_prop)}" if _prop else ""))
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
            cols[0].caption(f"{_etiqueta_factura(f)} · 🧾 {f.get('uuid_cfdi') or 'SIN-UUID'} · {f.get('rfc_emisor') or ''} · {f.get('nombre_emisor') or ''} · {f.get('fecha_factura') or ''}")
            cols[1].caption(money_md(f["monto_aplicado"]))
            _boton_pdf(sb, f, f"venv_{g['id']}_{f['id']}", cols[2])


# ======================================================================
# Panel de pendientes (arriba) y búsqueda de pagos (aplicante)
# ======================================================================
def _cache_corto(clave: tuple, ttl: int, calcular):
    """Guarda un resultado en la sesión por `ttl` segundos (se renueva solo si cambia la clave)."""
    import time
    almacen = st.session_state.setdefault("_cache_corto", {})
    hit = almacen.get(clave[0])
    if hit and hit["clave"] == clave and time.time() - hit["t"] < ttl:
        return hit["valor"]
    valor = calcular()
    almacen[clave[0]] = {"clave": clave, "t": time.time(), "valor": valor}
    return valor


def _panel_pendientes(sb, cuenta: dict) -> None:
    """Todo lo que está pendiente (movimientos de cualquier mes y gastos devueltos por el revisor), con su banco."""
    ver = st.session_state.get("bit_ver", 0)
    movs = _cache_corto(("pend_movs", cuenta["id"], ver), 60, lambda: db.listar_movimientos(sb, cuenta["id"], estado="en_espera"))
    devueltos = _cache_corto(("pend_dev", cuenta["id"], ver), 60, lambda: db.listar_pendientes_abiertos(sb, cuenta["id"]))
    if movs.empty and not devueltos:
        return
    banco = cuenta["nombre"] + (f" · {cuenta['banco']}" if cuenta.get("banco") and cuenta["banco"] not in cuenta["nombre"] else "")
    ahora = pd.Timestamp.now(tz="UTC")
    filas = []
    for m in movs.itertuples():
        esp = getattr(m, "espera_desde", None)
        desde = pd.to_datetime(esp, utc=True) if isinstance(esp, str) and esp else None
        dias = int((ahora - desde).days) if desde is not None else None
        nota_r = getattr(m, "nota_revisor", None)
        nota = nota_r if isinstance(nota_r, str) else ""
        filas.append({"Banco / cuenta": banco, "Fecha": m.fecha, "Descripción": m.descripcion, "Monto": abs(float(m.monto)),
                      "Por qué está pendiente": m.motivo_espera or "", "Días": dias, "Nota del revisor": nota})
    total = sum(f["Monto"] for f in filas)
    with st.container(border=True, key="panel-pendientes"):
        st.markdown(f"#### ⏳ Pendientes · {len(filas)} movimiento(s) · {money_md(total)}" if filas else "#### 🚩 Gastos devueltos por el revisor")
        if filas:
            tabla = pd.DataFrame(filas)
            st.dataframe(tabla.style.apply(lambda col: ["color:#B91C1C;font-weight:700" if (isinstance(v, (int, float)) and v >= 15) else "" for v in col], subset=["Días"])
                         .format({"Monto": "${:,.2f}"}, na_rep="—"),
                         hide_index=True, use_container_width=True, height=min(36 * (len(tabla) + 1) + 4, 220),
                         column_config={"Días": st.column_config.NumberColumn("Días", format="%d", help="Días que lleva pendiente (en rojo si pasan de 15)")})
        if devueltos:
            st.markdown(f"**🚩 {len(devueltos)} gasto(s) devuelto(s) por el revisor** · atiéndelos en la sección «Pendientes devueltos»")
            st.dataframe(pd.DataFrame([{"Banco / cuenta": banco, "Solicitud": p.get("solicitud_numero"), "Descripción": p.get("solicitud_descripcion") or "",
                                        "Monto": float(p.get("monto_estado") or 0), "Motivo": p["motivo"],
                                        "Último comentario": (p["comentarios"][-1]["texto"] if p.get("comentarios") else "")} for p in devueltos]),
                         hide_index=True, use_container_width=True, height=min(36 * (len(devueltos) + 1) + 4, 200),
                         column_config={"Monto": st.column_config.NumberColumn("Monto", format="$%.2f")})


def _sin_acentos(t: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", str(t)) if unicodedata.category(c) != "Mn").lower()


def _numero(token: str) -> float | None:
    try:
        return float(token.replace("$", "").replace(",", ""))
    except ValueError:
        return None


def _pagina_busqueda(sb, cuenta: dict, periodos: list[dict]) -> None:
    st.markdown("### 🔎 Buscar pagos e información")
    st.caption("Busca por monto, concepto, número de request, empleado, categoría, número de solicitud, UUID o referencia de pago. "
               "Puedes combinar palabras (todas deben coincidir). Un número se busca como monto (±$0.50) y también como texto.")
    ver = st.session_state.get("bit_ver", 0)

    def cargar():
        sols = db.listar_solicitudes(sb, cuenta["id"])
        gastos = db.listar_gastos(sb, [x["id"] for x in sols]) if sols else []
        movs = db.listar_movimientos(sb, cuenta["id"])
        env_g = db.listar_envio_gastos(sb, cuenta["id"])
        return reportes.filas_busqueda(sols, gastos, movs, env_g, {p["id"]: db.etiqueta_periodo(p) for p in periodos})

    filas = _cache_corto(("busqueda", cuenta["id"], ver, st.session_state.get("busq_ref", 0)), 300, cargar)
    c1, c2, c3, c4 = st.columns([3, 1.2, 1.4, 0.8])
    texto = c1.text_input("Buscar", key="busq_texto", placeholder="Ej.: 3980, Flight, Facebook, ZHANG, 4500012345…")
    meses = sorted({f["Mes"] for f in filas}, reverse=True)
    mes = c2.selectbox("Mes", ["Todos", *meses], key="busq_mes")
    tipos = ["Todos", "Gasto comprobado", "Movimiento del banco", "Solicitud sin comprobar"]
    tipo = c3.selectbox("Qué buscar", tipos, key="busq_tipo")
    if c4.button("🔄", key="busq_ref_btn", help="Actualizar los datos"):
        st.session_state["busq_ref"] = st.session_state.get("busq_ref", 0) + 1
        st.rerun()
    campos = ("Descripción", "Concepto", "Empleado", "Categoría", "Request", "Sol. #", "UUID", "Referencia de pago", "Lote de pago", "Estado", "Fecha", "Fecha de pago")
    vista = [f for f in filas if (mes == "Todos" or f["Mes"] == mes) and (tipo == "Todos" or f["Tipo"] == tipo)]
    for tok in _sin_acentos(texto).split():
        n = _numero(tok)
        vista = [f for f in vista if tok in _sin_acentos(" ".join(str(f[k]) for k in campos if f[k] not in (None, "")))
                 or (n is not None and abs(abs(float(f["Monto"])) - n) <= 0.5)]
    if not vista:
        st.info("Sin resultados. Prueba con menos palabras o con otro mes.")
        return
    tabla = pd.DataFrame(vista, columns=reportes.COLUMNAS_BUSQUEDA)
    tabla["Sol. #"] = tabla["Sol. #"].astype("Int64")
    st.markdown(f"**{len(tabla)} resultado(s)** · total {money_md(tabla['Monto'].sum())}")
    st.dataframe(tabla, hide_index=True, use_container_width=True, height=min(36 * (len(tabla) + 1) + 4, 520),
                 column_config={"Monto": st.column_config.NumberColumn("Monto", format="$%.2f"), "Sol. #": st.column_config.NumberColumn("Sol. #", format="%d", width="small")})
    d1, d2, _ = st.columns([1.3, 1.3, 3])
    d1.download_button("⬇️ Excel de los resultados", reportes.excel_tabla(tabla, "Búsqueda"), file_name=f"Busqueda_{cuenta['nombre'].replace(' ', '_')}_{datetime.date.today():%Y%m%d}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="busq_xl")
    d2.download_button("⬇️ CSV", tabla.to_csv(index=False).encode("utf-8-sig"), file_name=f"Busqueda_{datetime.date.today():%Y%m%d}.csv", mime="text/csv", key="busq_csv")


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


def _pintar_comprobacion(col) -> list[str]:
    """Color de la celda «Comprobación»: XML azul, manual naranja."""
    out = []
    for v in col:
        fondo, texto = reportes.COLOR_COMPROBACION.get(v, (None, None))
        out.append(f"background-color:{fondo};color:{texto};font-weight:700;text-align:center" if fondo else "")
    return out


def _tinte_manual(tabla: pd.DataFrame):
    """Sombrea toda la fila cuando la comprobación fue manual (o mixta)."""
    def aplicar(fila):
        manual = tabla.loc[fila.name, "Comprobación"] in ("Manual", "XML + manual")
        return [f"background-color:{reportes.FONDO_FILA_MANUAL}" if manual else "" for _ in fila]
    return aplicar


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
        sel = st.selectbox("Mes", etiquetas, index=_idx("rev_mes", etiquetas.index(actual) if actual in etiquetas else 0), key="rev_mes")
        periodo = periodos[etiquetas.index(sel)]
        if cuenta.get("limite_reembolso"):
            st.caption(f"Límite de reembolso: {money_md(cuenta['limite_reembolso'])}")

    secciones = ["📥 Cola de revisión", "🏦 Estado de cuenta", "💸 Lotes y pagos", "↩️ Devoluciones", "📊 Resumen y cierre de mes", "⬇️ Descargas"]
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
    elif vista == secciones[3]:
        _devoluciones_revisor(sb, cuenta, cuentas, periodos)
    elif vista == secciones[4]:
        _resumen_revisor(sb, cuentas, periodo)
    else:
        _descargas_revisor(sb, cuentas, periodos, cuenta, periodo)
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
    cu = reportes.cuadre(reportes.con_devolucion(df.drop_duplicates("id"), gastos))
    st.markdown(estilos.cuadre_html(cu), unsafe_allow_html=True)

    # ---- Envíos por fecha: revisar y aprobar cada envío por separado (o todos juntos) ----
    try:
        res_envios = {int(e["numero"]): e for e in db.listar_envios_resumen(sb, cuenta["id"], periodo["id"])}
    except Exception:
        res_envios = {}

    def _etq_envio(n) -> str:
        if n is None:
            return "Sin envío registrado"
        fecha = _fecha_cdmx(res_envios[n]["enviado_en"])[:16] if n in res_envios and res_envios[n].get("enviado_en") else ""
        return f"Envío #{n}" + (f" · {fecha}" if fecha else "")

    pend_nums = {p["solicitud_numero"] for p in pend}
    por_rev = [f for f in filas if f["solicitud_estado"] == "enviado_revision"]
    grupos: dict = {}
    for f in por_rev:
        grupos.setdefault(f["envio"], []).append(f)
    if grupos:
        with st.container(border=True):
            st.markdown("**📦 Por revisar, separado por fecha de envío**")
            st.caption("Revisa y aprueba cada envío por separado, o usa el botón de abajo para aprobar todos juntos.")
            for n in sorted(grupos, key=lambda x: (x is None, x or 0)):
                g = grupos[n]
                ok = [f for f in g if (abs(f["diferencia"]) <= tol or f.get("dif_tc")) and f["numero"] not in pend_nums]
                c = st.columns([3, 1.4, 1.2, 1.6])
                c[0].markdown(f"**{_etq_envio(n)}**  \n{len(g)} solicitud(es) · {money_md(sum(f['monto'] for f in g))}")
                c[1].caption(f"{len(ok)} sin diferencia" + (f" · {len(g) - len(ok)} con observaciones" if len(g) > len(ok) else ""))
                c[2].button("Ver sólo este", key=f"ver_env_{n}", on_click=lambda n=n: st.session_state.__setitem__("rev_envio", _etq_envio(n)))
                if c[3].button(f"✅ Aprobar este envío ({len(ok)})", key=f"vb_env_{n}", disabled=not ok, type="primary"):
                    st.session_state["vb_confirm"] = tuple(f["id"] for f in ok)

    f1, f1b, f2 = st.columns([1.2, 1.6, 2.6])
    filtro = f1.selectbox("Mostrar", [*FILTROS_COLA, "Todas"], key="rev_filtro")
    nums_env = sorted({f["envio"] for f in filas if f["envio"] is not None})
    opciones_env = ["Todos los envíos", *[_etq_envio(n) for n in nums_env], *(["Sin envío registrado"] if any(f["envio"] is None for f in filas) else [])]
    if st.session_state.get("rev_envio") not in opciones_env:
        st.session_state["rev_envio"] = "Todos los envíos"
    envio_sel = f1b.selectbox("Envío", opciones_env, key="rev_envio")
    texto = f2.text_input("🔎 Buscar (descripción, concepto, empleado, #)", key="rev_busca")
    vista = filas
    if filtro != "Todas":
        vista = [f for f in vista if f["solicitud_estado"] == FILTROS_COLA[filtro]]
    if envio_sel != "Todos los envíos":
        vista = [f for f in vista if _etq_envio(f["envio"]) == envio_sel]
    if texto.strip():
        t = texto.strip().lower()
        vista = [f for f in vista if t in " ".join(str(f[k]) for k in ("descripcion", "concepto", "numero")).lower()]

    limpias = [f for f in filas if f["solicitud_estado"] == "enviado_revision" and (abs(f["diferencia"]) <= tol or f.get("dif_tc"))
               and f["numero"] not in {p["solicitud_numero"] for p in pend}]
    if limpias:
        if st.button(f"⚡ Aprobar TODOS los envíos juntos: por revisar sin diferencia ({len(limpias)} · {money_md(sum(f['monto'] for f in limpias))})",
                     key="vb_todas"):
            st.session_state["vb_confirm"] = tuple(f["id"] for f in limpias)

    if not vista:
        st.caption("No hay solicitudes con este filtro." if filas else "Todavía no hay solicitudes comprobadas en este mes.")
        _confirmar_visto_bueno(sb, cuenta, periodo, {f["id"]: f for f in filas})
        return
    tabla = pd.DataFrame([{
        "Estado": NOMBRE_SOL_REV.get(f["solicitud_estado"], f["solicitud_estado"]), "Sol. #": f["numero"], "Fecha": f["fecha"],
        "Descripción": f["descripcion"], "Concepto": f["concepto"], "Comprobación": f["comprobacion"], "Mov.": f["movs"], "Monto": f["monto"],
        "Diferencia": f["diferencia"], "A devolver": f.get("devolucion") or 0.0, "Tipo de cambio": "💱 Sí" if f.get("dif_tc") else "",
        "Enviada en lote": f"#{f['envio']}" if f["envio"] else "—"} for f in vista])

    def _pintar(col):
        return [f"background-color:{ESTILO_SOL_REV[f['solicitud_estado']][0]};color:{ESTILO_SOL_REV[f['solicitud_estado']][1]};"
                "font-weight:700;text-align:center" for f in vista]

    def _pintar_dif(col):
        return [f"background-color:{estilos.C_DIFERENCIA_AMARILLO};font-weight:700" if f["tiene_diferencia"] else "" for f in vista]

    estilo = (tabla.style.apply(_tinte_manual(tabla), axis=1).apply(_pintar, subset=["Estado"]).apply(_pintar_dif, subset=["Diferencia"])
              .apply(_pintar_comprobacion, subset=["Comprobación"]).format({"Monto": "${:,.2f}", "Diferencia": "${:,.2f}", "A devolver": "${:,.2f}"}))
    st.caption("🟧 Las filas sombreadas en naranja se comprobaron capturando datos a mano (sin XML); revísalas con más cuidado. "
               "Selecciona una o varias filas: con una ves el detalle y puedes devolverla; con varias das el visto bueno y se crea el lote de pago.")
    ev = st.dataframe(estilo, hide_index=True, use_container_width=True, height=min(36 * (len(tabla) + 1) + 4, 420),
                      on_select="rerun", selection_mode="multi-row",
                      key=f"tabla_rev_{st.session_state.get('rev_ver', 0)}_{cuenta['id']}_{periodo['id']}_{filtro}_{envio_sel}_{texto}_{_firma(f['id'] for f in vista)}",
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
            malas = [f for f in elegidas if abs(f["diferencia"]) > tol and not f.get("dif_tc")]
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
    _detalle_solicitud_enviada(sb, df, r, gastos_por_id, revisor=True)
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


def _overview_categorias(movs: pd.DataFrame, cu: dict, comprobacion_de: dict | None = None) -> None:
    """Resumen del estado de cuenta por tipo de gasto y por tipo de comprobación; cada uno suma el total del estado de cuenta."""
    def _mostrar(filas: list[dict], etiquetas: list[str], pintar_fila) -> None:
        total_n = sum(f["n"] for f in filas if f.get("nivel", 0) == 0)
        total_m = round(sum(f["monto"] for f in filas if f.get("nivel", 0) == 0), 2)
        tabla = pd.DataFrame([{"Concepto": f["rubro"], "Movimientos": f["n"], "Monto": f["monto"],
                               "% del total": (f["monto"] / total_m if total_m else 0.0)} for f in filas]
                             + [{"Concepto": "TOTAL DEL ESTADO DE CUENTA", "Movimientos": total_n, "Monto": total_m, "% del total": 1.0 if total_m else 0.0}])
        grupo = etiquetas + ["Total"]

        def _pintar(fila):
            return [pintar_fila(grupo[fila.name])] * len(fila)

        st.dataframe(tabla.style.apply(_pintar, axis=1).format({"Monto": "${:,.2f}", "% del total": "{:.1%}"}),
                     hide_index=True, use_container_width=True, height=min(36 * (len(tabla) + 1) + 4, 460),
                     column_config={"Movimientos": st.column_config.NumberColumn("Movimientos", format="%d", width="small")})
        if abs(total_m - cu["monto_total"]) < 0.005 and total_n == cu["n_total"]:
            st.caption(f"✅ Cuadra: suma {money_md(total_m)}, igual al total del estado de cuenta.")
        else:
            st.error(f"Esta tabla suma {money_md(total_m)} pero el estado de cuenta es {money_md(cu['monto_total'])}.")

    por_cat = reportes.overview_categorias(movs)
    por_est = reportes.overview_estados(movs, comprobacion_de)
    if not por_cat:
        return
    fondos = {"Total": "font-weight:800;background-color:#E2E8F0", "No necesarios": "background-color:#F1F5F9",
              "Sin comprobar": "background-color:#FEE2E2;font-weight:700", "Pendientes": "background-color:#FEF3C7;font-weight:700",
              "Comprobados": "background-color:#DCFCE7;font-weight:700", "Comprobado": ""}
    with st.expander("📊 Overview del estado de cuenta", expanded=True):
        t1, t2 = st.tabs(["Por tipo de gasto (categoría)", "Por tipo de comprobación"])
        with t1:
            _mostrar(por_cat, [f["grupo"] for f in por_cat], lambda g: fondos.get(g, ""))
        with t2:
            _mostrar(por_est, [f["rubro"] if f["nivel"] == 0 else "Comprobado" for f in por_est], lambda g: fondos.get(g, ""))
            st.caption("«Comprobados» se desglosa en cómo se comprobó: con XML, capturado a mano, o mezclado.")


def _estado_cuenta_revisor(sb, cuenta: dict, periodo: dict) -> None:
    df = db.listar_movimientos_revision(sb, cuenta["id"], periodo["id"])
    if df.empty:
        st.info(f"{cuenta['nombre']} no tiene movimientos en {db.etiqueta_periodo(periodo)}.")
        return
    movs = df.drop_duplicates("id").sort_values(["fecha", "id"]).reset_index(drop=True)
    sols = db.listar_solicitudes(sb, cuenta["id"], periodo["id"])
    gastos = db.listar_gastos(sb, [x["id"] for x in sols]) if sols else []
    comprobacion_de = {f["id"]: f["comprobacion"] for f in reportes.filas_desglose(df, gastos) if f["tipo"] == "solicitud"}
    movs_dev = reportes.con_devolucion(movs, gastos)
    cu = reportes.cuadre(movs_dev)
    cols = st.columns(4)
    for col, (est, titulo) in zip(cols, (("comprobado", "Comprobados"), ("no_necesario", "No necesarios"),
                                         ("pendiente", "Sin comprobar"), ("en_espera", "Pendientes"))):
        col.markdown(estilos.tarjeta(titulo, str(cu["por_estado"][est]["n"]), estilos.money_html(cu["por_estado"][est]["monto"]), est), unsafe_allow_html=True)
    p = cu["por_estado"]
    st.markdown(estilos.barra_progreso(p["comprobado"]["n"], p["no_necesario"]["n"], p["pendiente"]["n"], p["en_espera"]["n"]), unsafe_allow_html=True)
    st.markdown(estilos.cuadre_html(cu), unsafe_allow_html=True)
    _overview_categorias(movs_dev, cu, comprobacion_de)
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
        "Comprobación": [comprobacion_de.get(r["solicitud_id"], "") if r["estado"] == "comprobado" else "" for _, r in vista.iterrows()],
        "Situación": [_situacion(r) for _, r in vista.iterrows()]})

    def _pintar(col):
        return [f"background-color:{ESTILO_ETIQUETA[inverso[v]][0]};color:{ESTILO_ETIQUETA[inverso[v]][1]};font-weight:700;text-align:center" for v in col]

    ev = st.dataframe(tabla.style.apply(_tinte_manual(tabla), axis=1).apply(_pintar, subset=["Estado"])
                      .apply(_pintar_comprobacion, subset=["Comprobación"]).format({"Monto": "${:,.2f}"}, na_rep="—"),
                      hide_index=True, use_container_width=True, height=min(36 * (len(tabla) + 1) + 4, 460),
                      on_select="rerun", selection_mode="multi-row",
                      key=f"tabla_ecr_{st.session_state.get('rev_ver', 0)}_{cuenta['id']}_{periodo['id']}_{filtro}_{texto}_{_firma(vista['id'])}",
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
        fila = next((f for f in reportes.filas_desglose(df, gastos) if f["tipo"] == "solicitud" and f["id"] == comprobados.iloc[0]["solicitud_id"]), None)
        if fila:
            _detalle_solicitud_enviada(sb, df, fila, {g["id"]: g for g in gastos}, revisor=True)
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
                                 "Monto": float(lote["monto"]), "Pagado": _pagado_de(lote), "Saldo": _saldo_de(lote),
                                 "Estado": lote["estado"], "Fecha de pago": lote.get("fecha_pago") or "",
                                 "Referencia": lote.get("referencia_pago") or ""}])
        resumen.to_excel(w, sheet_name="Lote", index=False)
        tabla.to_excel(w, sheet_name="Gastos", index=False)
        fmt = w.book.add_format({"num_format": "$#,##0.00"})
        w.sheets["Gastos"].set_column("A:E", 18)
        w.sheets["Gastos"].set_column("F:H", 16, fmt)
        w.sheets["Lote"].set_column("A:J", 16)
        w.sheets["Lote"].set_column("E:G", 16, fmt)
    return buf.getvalue()


def _historial_pagos(sb, lote: dict) -> None:
    """Pagos registrados del lote (si ya hubo alguno)."""
    if _pagado_de(lote) <= 0:
        return
    try:
        pagos = db.listar_pagos_lote(sb, lote["id"])
    except Exception:
        pagos = []
    if pagos:
        st.markdown("**🧾 Pagos registrados**")
        st.dataframe(pd.DataFrame([{"Fecha": p["fecha_pago"], "Monto": float(p["monto"]), "Referencia": p["referencia"]} for p in pagos]),
                     hide_index=True, use_container_width=True, column_config={"Monto": st.column_config.NumberColumn("Monto", format="$%.2f")})


def _movs_devolucion_colaborador(sb, cuentas_vistas: list[dict], nombre_cta: dict, periodos: list[dict]) -> None:
    """Movimientos del estado de cuenta clasificados «Pendiente» con el motivo «Devolución del colaborador»."""
    mes = {p["id"]: db.etiqueta_periodo(p) for p in periodos}
    filas = []
    for c in cuentas_vistas:
        m = db.listar_movimientos(sb, c["id"], estado="en_espera")
        if m is None or m.empty:
            continue
        # Cualquier «Pendiente» cuyo motivo hable de una devolución (Devolución del colaborador, Devolución en trámite, etc.)
        m = m[m["motivo_espera"].astype(str).str.lower().str.replace("ó", "o", regex=False).str.contains("devoluc", regex=False)]
        hoy = hoy_cdmx()
        for r in m.itertuples():
            desde = pd.to_datetime(r.espera_desde, errors="coerce")
            filas.append({"Cuenta": nombre_cta.get(c["id"], c["nombre"]), "Mes": mes.get(r.periodo_id, ""), "Fecha": r.fecha, "Descripción": r.descripcion,
                          "Monto": abs(float(r.monto)), "Motivo": r.motivo_espera, "Días pendiente": (hoy - desde.date()).days if pd.notna(desde) else None,
                          "Nota": getattr(r, "nota_revisor", None) or ""})
    st.markdown("#### 🏦 Movimientos pendientes por devolución del colaborador")
    if not filas:
        st.caption("Ningún movimiento de esta vista está en «Pendiente» con un motivo de devolución. "
                   "Para agregarlo: Estado de cuenta → selecciona el movimiento → «Pasar al rubro correcto» → Pendiente → motivo «Devolución del colaborador».")
        return
    total = sum(f["Monto"] for f in filas)
    st.markdown(estilos.tarjeta("Por devolver (movimientos del banco)", str(len(filas)), estilos.money_html(total), "rojo"), unsafe_allow_html=True)
    st.dataframe(pd.DataFrame(filas), hide_index=True, use_container_width=True, height=min(36 * (len(filas) + 1) + 4, 300),
                 column_config={"Monto": st.column_config.NumberColumn("Monto", format="$%.2f"), "Días pendiente": st.column_config.NumberColumn("Días pendiente", format="%d")})
    st.divider()
    st.markdown("#### 💸 Devoluciones de gastos (tip sin comprobación y sin comprobación, devolución)")


def _devoluciones_revisor(sb, cuenta: dict, cuentas: list[dict], periodos: list[dict]) -> None:
    """Lo que los colaboradores deben regresar a la cuenta de la empresa (tip sin comprobación y gastos sin comprobación)."""
    c1, c2 = st.columns([1.4, 1.4])
    alcance = c1.radio("Mostrar", ["Esta cuenta", "Todas las cuentas"], horizontal=True, key="rev_dev_alc")
    filtro = c2.radio("Estado", ["Por devolver", "Ya devueltas", "Todas"], horizontal=True, key="rev_dev_est")
    try:
        devs = db.listar_devoluciones(sb, cuenta["id"] if alcance.startswith("Esta") else None)
    except db.ErrorNube as e:
        st.error(f"{e} (¿ya corriste el script 015?)")
        return
    nombre_cta = {c["id"]: c["nombre"] for c in cuentas}
    _movs_devolucion_colaborador(sb, [cuenta] if alcance.startswith("Esta") else cuentas, nombre_cta, periodos)
    if not devs:
        st.info("No hay devoluciones de gastos. Aparecen cuando una propina se guarda sin comprobante o cuando marcas un gasto como «sin comprobación, devolución».")
        return
    nombre_cta = {c["id"]: c["nombre"] for c in cuentas}
    mes = {p["id"]: db.etiqueta_periodo(p) for p in periodos}
    pend = [d for d in devs if d["estado"] == "por_devolver"]
    hechas = [d for d in devs if d["estado"] == "devuelto"]
    k = st.columns(3)
    k[0].markdown(estilos.tarjeta("Por devolver", str(len(pend)), estilos.money_html(sum(float(d["monto"]) for d in pend)), "rojo"), unsafe_allow_html=True)
    k[1].markdown(estilos.tarjeta("Ya devueltas", str(len(hechas)), estilos.money_html(sum(float(d["monto"]) for d in hechas)), "comprobado"), unsafe_allow_html=True)
    k[2].markdown(estilos.tarjeta("Total", str(len(devs)), estilos.money_html(sum(float(d["monto"]) for d in devs)), "acento"), unsafe_allow_html=True)
    if pend:
        por_col: dict[tuple, float] = {}
        for d in pend:
            clave = (nombre_cta.get(d["cuenta_id"], ""), d["solicitud"].get("employee_name") or "—")
            por_col[clave] = por_col.get(clave, 0.0) + float(d["monto"])
        with st.expander("👤 Pendiente de devolver por colaborador", expanded=False):
            st.dataframe(pd.DataFrame([{"Cuenta": c, "Colaborador": e, "Por devolver": m} for (c, e), m in sorted(por_col.items())]),
                         hide_index=True, use_container_width=True, column_config={"Por devolver": st.column_config.NumberColumn("Por devolver", format="$%.2f")})
    vista = [d for d in devs if filtro == "Todas" or (filtro == "Por devolver" and d["estado"] == "por_devolver") or (filtro == "Ya devueltas" and d["estado"] == "devuelto")]
    if not vista:
        st.caption("No hay devoluciones con este filtro.")
        return
    tabla = pd.DataFrame([{
        "Cuenta": nombre_cta.get(d["cuenta_id"], ""), "Mes": mes.get(d["solicitud"].get("periodo_id"), ""), "Sol. #": d["solicitud"].get("numero"),
        "Colaborador": d["solicitud"].get("employee_name") or "", "Descripción": d["solicitud"].get("description") or "",
        "Categoría": d["gasto"].get("categoria") or "", "Tipo": TIPO_DEVOLUCION.get(d["tipo"], d["tipo"]), "Monto": float(d["monto"]),
        "Estado": "Por devolver" if d["estado"] == "por_devolver" else "Devuelto", "Fecha de devolución": d.get("devuelto_en") or "",
        "Referencia": d.get("devuelto_ref") or "", "Motivo": d.get("nota") or ""} for d in vista])
    ev = st.dataframe(tabla, hide_index=True, use_container_width=True, height=min(36 * (len(tabla) + 1) + 4, 380),
                      on_select="rerun", selection_mode="single-row",
                      key=f"tabla_dev_{st.session_state.get('rev_ver', 0)}_{alcance}_{filtro}_{_firma(d['id'] for d in vista)}",
                      column_config={"Monto": st.column_config.NumberColumn("Monto", format="$%.2f"), "Sol. #": st.column_config.NumberColumn("Sol. #", format="%d", width="small")})
    st.download_button("⬇️ Excel de devoluciones", reportes.excel_tabla(tabla, "Devoluciones"), file_name=f"Devoluciones_{datetime.date.today():%Y%m%d}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="xl_dev")
    sel = [i for i in (ev.selection.rows if ev and ev.selection else []) if i < len(vista)]
    if not sel:
        st.caption("Selecciona una devolución para registrar que el colaborador ya regresó el dinero.")
        return
    d = vista[sel[0]]
    with st.container(border=True):
        st.markdown(f"**{TIPO_DEVOLUCION.get(d['tipo'], d['tipo'])}** · {money_md(d['monto'])} · {d['solicitud'].get('employee_name') or '—'} · solicitud #{d['solicitud'].get('numero')}")
        if d["estado"] == "devuelto":
            st.success(f"Devuelto el {d.get('devuelto_en')} · referencia {d.get('devuelto_ref')}")
            return
        f1, f2 = st.columns(2)
        fecha = f1.date_input("Fecha en que se devolvió", value=hoy_cdmx(), key=f"dv_f_{d['id']}")
        ref = f2.text_input("Referencia del depósito / transferencia", key=f"dv_r_{d['id']}")
        if st.button("✅ Registrar devolución recibida", type="primary", key=f"dv_b_{d['id']}"):
            try:
                db.registrar_devolucion_recibida(sb, d["id"], fecha, ref)
                _nueva_tabla_rev()
                _flash("Devolución registrada.")
                st.rerun()
            except db.ErrorNube as e:
                st.error(str(e))


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
    k[0].markdown(estilos.tarjeta("Por pagar (saldo)", str(len(por_pagar)), estilos.money_html(sum(_saldo_de(l) for l in por_pagar)), "alerta"), unsafe_allow_html=True)
    k[1].markdown(estilos.tarjeta("Pagado", f"{sum(1 for l in lotes if _pagado_de(l) > 0)} lote(s)", estilos.money_html(sum(_pagado_de(l) for l in lotes)), "comprobado"), unsafe_allow_html=True)
    k[2].markdown(estilos.tarjeta("Anulados", str(sum(1 for l in lotes if l["estado"] == "anulado")), "desbloqueados", "pendiente"), unsafe_allow_html=True)
    tabla = pd.DataFrame([{
        "Cuenta": nombre_cta.get(l["cuenta_id"], ""), "Lote": f"{mes.get(l['periodo_id'], '')}-{l['numero']}", "Cerrado": str(l["cerrado_en"])[:10],
        "Gastos": l["num_gastos"], "Monto": float(l["monto"]), "Pagado": _pagado_de(l), "Saldo": _saldo_de(l), "Estado": _estado_lote(l),
        "Fecha de pago": l.get("fecha_pago") or "", "Referencia": l.get("referencia_pago") or ""} for l in lotes])
    ev = st.dataframe(tabla, hide_index=True, use_container_width=True, height=min(36 * (len(tabla) + 1) + 4, 360),
                      on_select="rerun", selection_mode="single-row", key=f"tabla_lotes_{st.session_state.get('rev_ver', 0)}_{alcance}_{_firma(l['id'] for l in lotes)}",
                      column_config={c: st.column_config.NumberColumn(c, format="$%.2f") for c in ("Monto", "Pagado", "Saldo")})
    sel = [i for i in (ev.selection.rows if ev and ev.selection else []) if i < len(lotes)]
    if not sel:
        st.caption("Haz clic en un lote para ver sus gastos, registrar un pago (total o parcial) o desbloquearlo.")
        return
    lote = lotes[sel[0]]
    gastos = db.gastos_de_lote(sb, lote["id"])
    etiqueta = f"{nombre_cta.get(lote['cuenta_id'], '')} · {mes.get(lote['periodo_id'], '')} · lote {lote['numero']}"
    with st.container(border=True):
        st.markdown(f"**{etiqueta}** · {_estado_lote(lote)} · {money_md(lote['monto'])} · {lote['num_gastos']} gasto(s)")
        if lote["estado"] == "por_pagar":
            q = st.columns(3)
            q[0].metric("Monto del lote", money_md(lote["monto"]))
            q[1].metric("Pagado", money_md(_pagado_de(lote)))
            q[2].metric("Saldo pendiente", money_md(_saldo_de(lote)))
        _historial_pagos(sb, lote)
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
                saldo = _saldo_de(lote)
                st.markdown("**💸 Registrar pago** (total o parcial)")
                st.caption("Puedes pagar el lote en varios pagos. Queda «Pagado» (y el aplicante lo ve así) cuando lo pagado alcanza el monto del lote.")
                monto = st.number_input("Monto a pagar ahora", min_value=0.0, max_value=float(saldo), value=float(saldo), step=100.0, format="%.2f", key=f"pg_m_{lote['id']}_{saldo}")
                fecha = st.date_input("Fecha de pago", value=hoy_cdmx(), key=f"pg_f_{lote['id']}")
                ref = st.text_input("Referencia / folio de pago (por ejemplo AP-MX o PY-MX)", key=f"pg_r_{lote['id']}")
                quedara = round(saldo - monto, 2)
                st.caption("Con este pago el lote queda **pagado por completo**." if quedara <= 0.005 else f"Con este pago quedará un saldo de **{money_md(quedara)}**.")
                if st.button("✅ Registrar pago", type="primary", key=f"pg_b_{lote['id']}", disabled=monto <= 0):
                    try:
                        db.registrar_pago_lote(sb, lote["id"], fecha, monto, ref)
                        _nueva_tabla_rev()
                        _flash("Pago completo registrado: el aplicante ya lo ve como pagado." if quedara <= 0.005
                               else f"Pago parcial registrado. Saldo pendiente: {money_md(quedara)}.")
                        st.rerun()
                    except db.ErrorNube as e:
                        st.error(str(e))
            with c2:
                st.markdown("**↩️ Desbloquear lote**")
                st.caption("Anula el lote y regresa sus solicitudes a «Por revisar». Solo si aún no tiene ningún pago.")
                if _pagado_de(lote) > 0:
                    st.info("Este lote ya tiene pagos registrados; no se puede desbloquear.")
                mot = st.text_input("Motivo (obligatorio)", key=f"ds_m_{lote['id']}")
                if st.button("Desbloquear lote", key=f"ds_b_{lote['id']}", disabled=_pagado_de(lote) > 0):
                    try:
                        db.desbloquear_lote(sb, lote["id"], mot)
                        _nueva_tabla_rev()
                        _flash("Lote desbloqueado.")
                        st.rerun()
                    except db.ErrorNube as e:
                        st.error(str(e))


# ----------------------------------------------------------------------
# Descargas: Excel del desglose de comprobación (total o parcial)
# ----------------------------------------------------------------------
HOJAS_EXCEL = {"Comprobados": "comprobados", "No necesarios": "no_necesarios", "Sin comprobar": "sin_comprobar",
               "Pendientes": "pendientes", "Resumen / cuadre": "resumen"}


def _descargas_revisor(sb, cuentas: list[dict], periodos: list[dict], cuenta: dict, periodo: dict) -> None:
    st.markdown("##### 📥 Excel con el desglose de comprobación")
    st.caption("Incluye lo comprobado (por enviar, en revisión, aprobado, pagado o devuelto), lo no necesario, lo pendiente y lo que aún está por comprobar. "
               "Elige qué cuentas y meses quieres, o descarga todo.")
    nombres = [c["nombre"] for c in cuentas]
    etiquetas = [db.etiqueta_periodo(p) for p in periodos]
    a1, a2 = st.columns(2)
    todas_c = a1.checkbox("Todas las cuentas", key="dl_todas_c")
    todos_m = a2.checkbox("Todos los meses", key="dl_todos_m")
    c1, c2 = st.columns(2)
    sel_c = nombres if todas_c else c1.multiselect("Cuentas", nombres, default=[cuenta["nombre"]], key="dl_cuentas")
    sel_m = etiquetas if todos_m else c2.multiselect("Meses", etiquetas, default=[db.etiqueta_periodo(periodo)], key="dl_meses")
    b1, b2 = st.columns(2)
    hojas = b1.multiselect("Qué incluir", list(HOJAS_EXCEL), default=list(HOJAS_EXCEL), key="dl_hojas")
    estatus = b2.multiselect("De lo comprobado, solo gastos…", list(reportes.ESTATUS_GASTO.values()), default=list(reportes.ESTATUS_GASTO.values()),
                             key="dl_estatus")
    combos = len(sel_c) * len(sel_m)
    if not sel_c or not sel_m or not hojas:
        st.info("Elige al menos una cuenta, un mes y una hoja.")
        return
    clave = (tuple(sel_c), tuple(sel_m), tuple(hojas), tuple(estatus))
    if combos > 30:
        st.warning(f"Son {combos} combinaciones de cuenta y mes; la descarga puede tardar. Considera acotarla.")
    if st.button(f"📊 Preparar Excel ({len(sel_c)} cuenta(s) · {len(sel_m)} mes(es))", type="primary", key="dl_preparar"):
        por_nombre = {c["nombre"]: c for c in cuentas}
        por_etiqueta = {db.etiqueta_periodo(p): p for p in periodos}
        aplicantes = db.aplicantes_por_cuenta(sb)
        conjuntos = []
        with st.spinner("Armando el Excel…"):
            for nc in sel_c:
                cta = por_nombre[nc]
                for em in sel_m:
                    per = por_etiqueta[em]
                    movs = db.listar_movimientos(sb, cta["id"], periodo_id=per["id"])
                    sols = db.listar_solicitudes(sb, cta["id"], per["id"])
                    if movs.empty and not sols:
                        continue
                    gastos = db.listar_gastos(sb, [x["id"] for x in sols]) if sols else []
                    conjuntos.append({"cuenta": nc, "periodo": em, "applicant": aplicantes.get(cta["id"], ""), "movs": movs, "sols": sols,
                                      "gastos": gastos, "env_g": db.listar_envio_gastos(sb, cta["id"], per["id"])})
        if not conjuntos:
            st.warning("No hay datos en las cuentas y meses elegidos.")
        else:
            inverso = {v: k for k, v in reportes.ESTATUS_GASTO.items()}
            st.session_state["dl_xl"] = {
                "clave": clave, "hora": datetime.datetime.now().strftime("%H:%M"), "n": len(conjuntos),
                "bytes": reportes.excel_revisor(conjuntos, {HOJAS_EXCEL[h] for h in hojas}, {inverso[e] for e in estatus})}
    xl = st.session_state.get("dl_xl")
    if xl and xl["clave"] == clave:
        st.download_button(f"⬇️ Descargar Excel ({xl['n']} cuenta/mes, generado {xl['hora']})", data=xl["bytes"],
                           file_name=f"Comprobaciones_revisor_{datetime.date.today():%Y%m%d}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_bajar")
    elif xl:
        st.caption("Cambiaste la selección: vuelve a pulsar «Preparar Excel».")


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
                    try:
                        db.agregar_catalogo(sb, "motivo_espera", MOTIVO_DEV_COLABORADOR)   # catálogos que ya existían no lo traen
                    except Exception:
                        pass
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
    _conservar_filtros()
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
        sel = st.selectbox("Mes", etiquetas, index=_idx("mes_sel", etiquetas.index(actual) if actual in etiquetas else 0), key="mes_sel")
        periodo = periodos[etiquetas.index(sel)]
        cerrado = cierre.get(periodo["id"]) == "cerrado"
        if cerrado:
            st.warning("Mes cerrado (solo lectura).")
        if cuenta.get("limite_reembolso"):
            st.caption(f"Límite de reembolso: {money_md(cuenta['limite_reembolso'])}")
        st.divider()
        vista_app = st.radio("Ir a", ["🧾 Mi caja chica", "🔎 Buscar pagos"], key="vista_app")

    if st.session_state.get("comprobando"):
        _abrir_ventana_comprobar(sb, cuenta, st.session_state.comprobando, st.session_state.tol)
    _panel_pendientes(sb, cuenta)
    if vista_app == "🔎 Buscar pagos":
        _pagina_busqueda(sb, cuenta, periodos)
        _visor_adjunto(sb)
        return

    # Control de sección (en lugar de st.tabs) para conservar la sección elegida después de cada acción.
    secciones = ["🧾 Bitácora", "🏦 Estado de cuenta", "📤 Enviados a comprobación", "🚩 Pendientes devueltos", "💸 Lotes y pagos"]
    elegida = st.segmented_control("Sección", secciones, default=secciones[0], label_visibility="collapsed", key="seccion")
    vista = elegida or st.session_state.get("_seccion_ult", secciones[0])
    st.session_state["_seccion_ult"] = vista
    st.divider()
    if vista == secciones[0]:
        _tab_bitacora(sb, cuenta, periodo, cerrado, cierre)
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
