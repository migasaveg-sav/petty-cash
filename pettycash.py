"""Comprobación de Caja Chica — interfaz Streamlit simplificada.

Toda la lógica pesada (lectura de CFDI, lectura de estados de cuenta, emparejamiento
automático, catálogos y persistencia) vive en módulos separados y probados con pytest
(cfdi.py, bank.py, matching.py, catalog.py, persistence.py). Este archivo sólo arma
la interfaz:

  Barra lateral  -> configuración (banco, archivo, guardar/cargar avance, autoguardado, reset)
  Pestaña Pendientes      -> tabla + emparejamiento automático + diálogo para trabajar un gasto
  Pestaña Comprobados     -> historial editable (revertir)
  Pestaña No necesarios   -> historial editable (revertir)
  Pestaña Resumen         -> checksum de cuadre + descarga de Excel

Para simplificar el flujo de trabajo, el panel gigante que antes vivía siempre visible
en la página ahora se abre como una ventana modal (st.dialog) sólo cuando el usuario
elige un gasto específico para trabajar.
"""
from __future__ import annotations

import datetime
import json

import pandas as pd
import streamlit as st

from bank import BANCOS_DISPONIBLES, BankStatementError, cargar_estado_cuenta, columnas_disponibles_para_mapeo
from catalog import (
    APLICANTES_DEFAULT,
    CATEGORIAS_DEFAULT,
    CATEGORIAS_SOLICITUD_DEFAULT,
    EMPLEADOS_DEFAULT,
    MATERIALES_DEFAULT,
    agregar_valor,
    catalogo_inicial,
)
from cfdi import CFDIParseError, parse_cfdi
from matching import calcular_matches_automaticos, checksum_reconciliacion, resumen_estados
from persistence import (
    CAMPOS_SESION,
    autoguardar,
    borrar_autoguardado,
    cargar_sesion_dict,
    hay_autoguardado,
    restaurar_autoguardado,
    sesion_a_json_bytes,
    sesion_vacia,
)

# ============================================================
# PALETA DE COLORES — inspirada en la interfaz de Facebook
# (fondo gris claro + tarjetas blancas + azul de acento + texto casi
# negro, para maximizar el contraste texto/fondo). Todos los pares
# texto/fondo de abajo cumplen al menos 4.5:1 de contraste (WCAG AA).
# ============================================================
C_FONDO = "#ADD8E6"            # fondo general de la página (gris FB)
C_TARJETA = "#FFFFFF"          # tarjetas y contenedores
C_BORDE = "#CED0D4"            # bordes sutiles
C_TEXTO_OSCURO = "#050505"     # texto principal, casi negro (20:1 sobre blanco)
C_TEXTO_SECUNDARIO = "#050505" # texto secundario / captions (5.7:1 sobre blanco)
C_AZUL_FB = "#166FE5"          # azul de acento (encabezados, botones, "comprobado")
C_CORAL_ALERTA = "#D32F2F"     # rojo de error/alerta
C_AMARILLO_ACENTO = "#B45309"  # ámbar de advertencia / "no necesario"
C_GRIS_NEUTRO = "#4B4E53"      # gris oscuro para "pendiente" (8.3:1 con texto blanco)
C_VERDE_OK = "#1E7B34"         # verde de éxito
C_BITACORA_HEADER = "#DC143C"  # encabezado de la bitácora de solicitudes (pedido por el usuario)

st.set_page_config(page_title="Comprobación Caja Chica", layout="wide")

MESES_ES = [
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]

AUTOSAVE_DB = "pettycash_autosave.db"

# ============================================================
# ESTADO DE SESIÓN
# ============================================================
def init_state() -> None:
    defaults = sesion_vacia()
    defaults.update({
        "mostrar_mapeo_manual": False,
        "confirmar_reset": False,
        "autoguardado_activo": True,
        "categorias": None,
        "materiales": None,
        "categorias_solicitud": None,
        "empleados": None,
        "aplicantes": None,
        "solicitud_en_proceso": None,
        "solicitud_form_version": 0,
    })
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v
    if st.session_state.categorias is None:
        st.session_state.categorias = catalogo_inicial(CATEGORIAS_DEFAULT)
    if st.session_state.materiales is None:
        st.session_state.materiales = catalogo_inicial(MATERIALES_DEFAULT)
    if st.session_state.categorias_solicitud is None:
        st.session_state.categorias_solicitud = catalogo_inicial(CATEGORIAS_SOLICITUD_DEFAULT)
    if st.session_state.empleados is None:
        st.session_state.empleados = catalogo_inicial(EMPLEADOS_DEFAULT)
    if st.session_state.aplicantes is None:
        st.session_state.aplicantes = catalogo_inicial(APLICANTES_DEFAULT)


init_state()

# ============================================================
# ESTILOS
# ============================================================
st.markdown(f"""
<style>
.stApp {{ background-color: {C_FONDO}; color: {C_TEXTO_OSCURO}; }}
table, th, td {{ border: 1px solid {C_BORDE}; border-collapse: collapse; padding: 6px; }}
th {{ background-color: {C_AZUL_FB}; color: white; }}
td {{ color: {C_TEXTO_OSCURO}; background-color: {C_TARJETA}; }}

[data-testid="stCaptionContainer"], small {{ color: {C_TEXTO_SECUNDARIO} !important; }}
[data-testid="stExpander"] summary {{ color: {C_TEXTO_OSCURO}; font-weight: 600; }}

.card {{
    background-color: {C_TARJETA}; border-radius: 10px; padding: 16px 20px;
    margin-bottom: 14px; border: 1px solid {C_BORDE}; color: {C_TEXTO_OSCURO};
}}
.summary-box {{
    border-radius: 10px; padding: 14px 18px; text-align: center; color: white;
}}
.summary-title {{ font-size: 0.85rem; opacity: 0.9; margin-bottom: 4px; }}
.summary-count {{ font-size: 1.6rem; font-weight: 700; }}
.summary-amount {{ font-size: 1.0rem; opacity: 0.95; }}

.box-pendiente {{ background-color: {C_GRIS_NEUTRO}; }}
.box-comprobado {{ background-color: {C_AZUL_FB}; }}
.box-no-necesario {{ background-color: {C_AMARILLO_ACENTO}; }}

.success-box {{
    background-color: {C_VERDE_OK}; color: white; padding: 10px; border-radius: 6px; font-weight: 600;
}}
.error-box {{
    background-color: {C_CORAL_ALERTA}; color: white; padding: 10px; border-radius: 6px; font-weight: 600;
}}
.warn-box {{
    background-color: {C_AMARILLO_ACENTO}; color: white; padding: 8px; border-radius: 6px; font-weight: 600;
}}
.bitacora-header {{
    background-color: {C_BITACORA_HEADER}; color: white; font-weight: 700; padding: 6px 4px;
    border-radius: 4px; text-align: center; font-size: 0.85rem;
}}
</style>
""", unsafe_allow_html=True)

st.title("💰 Comprobación de Caja Chica")

# ============================================================
# HELPERS GENERALES
# ============================================================
def money(x) -> str:
    try:
        return f"${float(x):,.2f}"
    except (ValueError, TypeError):
        return "$0.00"


def status_label(estado: str) -> str:
    return {
        "pendiente": "⏳ Sin comprobar",
        "comprobado": "✅ Comprobado",
        "no_necesario": "🚫 No necesario",
    }.get(estado, estado)


def etiqueta_factura(f: dict) -> str:
    origen = f.get("Archivo") or f.get("Fuente", "Factura")
    marca = " ⚠️" if f.get("Incompleta") else ""
    return f"{origen} · {money(f.get('Monto Total', 0))}{marca}"


def mes_es(fecha_str) -> str:
    try:
        f = pd.to_datetime(fecha_str)
        return f"{MESES_ES[f.month - 1]} {f.year}"
    except Exception:
        return ""


def next_factura_id() -> int:
    st.session_state.factura_counter += 1
    return st.session_state.factura_counter


def next_solicitud_id() -> int:
    st.session_state.solicitud_counter += 1
    return st.session_state.solicitud_counter


def _limpiar_formulario_solicitud() -> None:
    """Fuerza a Streamlit a remontar los widgets del formulario de solicitudes desde
    cero tras guardar uno (mismo truco que `_limpiar_seleccion_tabla_pendientes`:
    los widgets normales no tienen `clear_on_submit` fuera de un st.form, así que
    cambiamos la versión que forma parte de su `key`)."""
    st.session_state.solicitud_form_version = st.session_state.get("solicitud_form_version", 0) + 1


def uuids_consumidos() -> set:
    """UUIDs ya asignados a un gasto (pendiente de guardar o ya guardado en historial).
    No incluye el pool automático: una factura ahí sigue disponible."""
    usados = set()
    for facs in st.session_state.facturas_por_gasto.values():
        usados.update(f["UUID"] for f in facs if f["UUID"] != "SIN-UUID")
    for reg in st.session_state.concatenados:
        usados.update(f["UUID"] for f in reg["Facturas"] if f["UUID"] != "SIN-UUID")
    return usados


def _state_snapshot() -> dict:
    snap = {"bank_df": st.session_state.get("bank_df")}
    for campo in CAMPOS_SESION:
        snap[campo] = st.session_state.get(campo)
    return snap


def _restaurar_estado(restaurado: dict) -> None:
    for k, v in restaurado.items():
        st.session_state[k] = v


def _autoguardar_si_activo() -> None:
    if st.session_state.autoguardado_activo and st.session_state.bank_df is not None:
        try:
            autoguardar(_state_snapshot(), db_path=AUTOSAVE_DB)
        except Exception:
            pass  # el autoguardado nunca debe interrumpir el flujo del usuario


def _limpiar_seleccion_tabla_pendientes() -> None:
    """Limpia la fila seleccionada en la tabla de 'Pendientes'.

    Streamlit recuerda qué fila (por posición) estaba seleccionada entre una
    ejecución y otra. Si un gasto se comprueba, se marca como no necesario o se
    revierte, la tabla de pendientes cambia de tamaño y esa posición guardada ya
    no corresponde al mismo gasto (o directamente deja de existir). Hay que
    llamar esto justo antes de cualquier `st.rerun()` que pueda cambiar el
    conjunto de gastos pendientes, para no dejar seleccionada la fila
    equivocada -o provocar un IndexError-.

    No basta con borrar la clave del `session_state`: el widget de tabla
    conserva su propio estado visual en el navegador (el checkbox marcado
    puede seguir viéndose aunque el backend ya no tenga nada seleccionado).
    Por eso cambiamos también la versión, que forma parte de la `key` del
    widget, para que Streamlit lo vuelva a montar desde cero."""
    st.session_state.pop("tabla_pendientes", None)
    st.session_state.tabla_pendientes_version = st.session_state.get("tabla_pendientes_version", 0) + 1


def _selector_catalogo(label: str, catalogo_key: str, valor_actual: str, widget_key: str) -> str:
    """Selectbox respaldado por un catálogo en session_state, con opción de agregar
    un valor nuevo sin salir del flujo."""
    catalogo = st.session_state[catalogo_key]
    agregar_opcion = "➕ Agregar nueva…"
    opciones = [*catalogo, agregar_opcion]
    idx_actual = catalogo.index(valor_actual) if valor_actual in catalogo else 0
    elegido = st.selectbox(label, opciones, index=idx_actual, key=widget_key)
    if elegido == agregar_opcion:
        nuevo = st.text_input(f"Nuevo valor para «{label}»", key=f"{widget_key}_nuevo")
        if st.button(f"Agregar a {label}", key=f"{widget_key}_btn_agregar", disabled=not nuevo.strip()):
            st.session_state[catalogo_key] = agregar_valor(catalogo, nuevo)
            st.rerun()
        return valor_actual or ""
    return elegido


def _dialog(title: str, width: str = "large"):
    """Decorador de diálogo modal, con respaldo si st.dialog no existe en la versión
    de Streamlit instalada (se muestra inline en vez de en modal)."""
    if hasattr(st, "dialog"):
        return st.dialog(title, width=width)

    def decorador(func):
        def envoltura(*args, **kwargs):
            st.divider()
            st.markdown(f"#### {title}")
            func(*args, **kwargs)
        return envoltura
    return decorador


# ============================================================
# BARRA LATERAL — CONFIGURACIÓN
# ============================================================
with st.sidebar:
    st.markdown("## ⚙️ Configuración")

    banco_idx = BANCOS_DISPONIBLES.index(st.session_state.banco) if st.session_state.banco in BANCOS_DISPONIBLES else 0
    banco = st.selectbox("Banco", BANCOS_DISPONIBLES, index=banco_idx, key="banco_select")

    file = st.file_uploader("Estado de cuenta (CSV o Excel)", type=["csv", "xlsx"], key="bank_uploader")

    st.session_state.mostrar_mapeo_manual = st.checkbox(
        "Mapear columnas manualmente",
        value=st.session_state.mostrar_mapeo_manual,
        help="Actívalo si el banco elegido no detecta bien las columnas del archivo.",
    )

    mapeo_manual = None
    if st.session_state.mostrar_mapeo_manual and file is not None:
        try:
            cols_disponibles = columnas_disponibles_para_mapeo(file.getvalue(), file.name)
            st.caption("Asigna cada columna del archivo a un campo estándar.")
            estandar_opciones = ["(ignorar)", "Fecha", "Descripción", "Cargo", "Abono", "Importe", "Saldo"]
            mapeo_manual = {}
            for col in cols_disponibles:
                elegido = st.selectbox(str(col), estandar_opciones, key=f"map_{col}")
                if elegido != "(ignorar)":
                    mapeo_manual[col] = elegido
        except BankStatementError as e:
            st.error(str(e))

    if file is not None:
        mapeo_hash = tuple(sorted(mapeo_manual.items())) if mapeo_manual else None
        file_key = (file.name, file.size, banco, mapeo_hash)
        if st.session_state.bank_file_id != file_key:
            try:
                nuevo_df = cargar_estado_cuenta(file.getvalue(), file.name, banco, mapeo_manual)
            except BankStatementError as e:
                st.error(f"No se pudo cargar el estado de cuenta: {e}")
                nuevo_df = None
            if nuevo_df is not None:
                st.session_state.bank_df = nuevo_df
                st.session_state.banco = banco
                st.session_state.bank_file_id = file_key
                st.session_state.estados = {i: "pendiente" for i in nuevo_df.index}
                st.session_state.facturas_por_gasto = {}
                st.session_state.clasificacion_por_gasto = {}
                st.session_state.pool_facturas = []
                st.session_state.concatenados = []
                st.session_state.no_necesarios = []
                st.success(f"Estado de cuenta cargado: {len(nuevo_df)} movimiento(s).")
                _autoguardar_si_activo()
                st.rerun()

    st.divider()
    st.markdown("### 💾 Guardar / continuar avance")
    if st.session_state.bank_df is not None:
        st.download_button(
            "📥 Descargar avance (.json)",
            data=sesion_a_json_bytes(_state_snapshot()),
            file_name=f"avance_caja_chica_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.json",
            mime="application/json",
            use_container_width=True,
        )
    else:
        st.caption("Sube un estado de cuenta primero para poder guardar avance.")

    json_file = st.file_uploader("Subir avance (.json)", type=["json"], key="json_uploader")
    if json_file is not None and st.button("Cargar este avance", use_container_width=True):
        try:
            data = json.loads(json_file.getvalue().decode("utf-8"))
            _restaurar_estado(cargar_sesion_dict(data))
            if st.session_state.categorias is None:
                st.session_state.categorias = catalogo_inicial(CATEGORIAS_DEFAULT)
            if st.session_state.materiales is None:
                st.session_state.materiales = catalogo_inicial(MATERIALES_DEFAULT)
            if st.session_state.categorias_solicitud is None:
                st.session_state.categorias_solicitud = catalogo_inicial(CATEGORIAS_SOLICITUD_DEFAULT)
            if st.session_state.empleados is None:
                st.session_state.empleados = catalogo_inicial(EMPLEADOS_DEFAULT)
            if st.session_state.aplicantes is None:
                st.session_state.aplicantes = catalogo_inicial(APLICANTES_DEFAULT)
            st.session_state.solicitud_en_proceso = None
            st.success("Avance restaurado correctamente.")
            st.rerun()
        except Exception as e:
            st.error(f"No se pudo leer el archivo de avance: {e}")

    st.session_state.autoguardado_activo = st.toggle(
        "Autoguardado local activo",
        value=st.session_state.autoguardado_activo,
        help="Guarda una copia de tu avance en este servidor con cada cambio, como red "
             "de seguridad si cierras el navegador sin descargar el .json.",
    )
    autosave_info = hay_autoguardado(AUTOSAVE_DB)
    if autosave_info:
        st.caption(f"Último autoguardado: {autosave_info['guardado_en']}")
        if st.session_state.bank_df is None and st.button("♻️ Restaurar último autoguardado", use_container_width=True):
            restaurado = restaurar_autoguardado(AUTOSAVE_DB)
            if restaurado:
                _restaurar_estado(restaurado)
                if st.session_state.categorias is None:
                    st.session_state.categorias = catalogo_inicial(CATEGORIAS_DEFAULT)
                if st.session_state.materiales is None:
                    st.session_state.materiales = catalogo_inicial(MATERIALES_DEFAULT)
                if st.session_state.categorias_solicitud is None:
                    st.session_state.categorias_solicitud = catalogo_inicial(CATEGORIAS_SOLICITUD_DEFAULT)
                if st.session_state.empleados is None:
                    st.session_state.empleados = catalogo_inicial(EMPLEADOS_DEFAULT)
                if st.session_state.aplicantes is None:
                    st.session_state.aplicantes = catalogo_inicial(APLICANTES_DEFAULT)
                st.session_state.solicitud_en_proceso = None
                st.success("Avance restaurado desde autoguardado.")
                st.rerun()

    st.divider()
    if st.session_state.confirmar_reset:
        st.warning("¿Seguro que quieres borrar todo el avance actual? Esta acción no se puede deshacer.")
        rc1, rc2 = st.columns(2)
        if rc1.button("Sí, borrar todo", type="primary", use_container_width=True):
            vacio = sesion_vacia()
            _restaurar_estado(vacio)
            st.session_state.categorias = catalogo_inicial(CATEGORIAS_DEFAULT)
            st.session_state.materiales = catalogo_inicial(MATERIALES_DEFAULT)
            st.session_state.categorias_solicitud = catalogo_inicial(CATEGORIAS_SOLICITUD_DEFAULT)
            st.session_state.empleados = catalogo_inicial(EMPLEADOS_DEFAULT)
            st.session_state.aplicantes = catalogo_inicial(APLICANTES_DEFAULT)
            st.session_state.solicitud_en_proceso = None
            st.session_state.confirmar_reset = False
            borrar_autoguardado(AUTOSAVE_DB)
            st.rerun()
        if rc2.button("Cancelar", use_container_width=True):
            st.session_state.confirmar_reset = False
            st.rerun()
    else:
        if st.button("🔄 Reiniciar todo", use_container_width=True):
            st.session_state.confirmar_reset = True
            st.rerun()

# ============================================================
# CONTENIDO PRINCIPAL
# ============================================================
df = st.session_state.bank_df
if df is None:
    st.info("👈 Sube un estado de cuenta desde el panel de la izquierda para comenzar.")
    st.stop()

resumen = resumen_estados(df, st.session_state.estados)
col1, col2, col3 = st.columns(3)
with col1:
    st.markdown(f"""
    <div class="summary-box box-pendiente">
        <div class="summary-title">⏳ SIN COMPROBAR</div>
        <div class="summary-count">{resumen['pendiente']['count']}</div>
        <div class="summary-amount">{money(resumen['pendiente']['total'])}</div>
    </div>""", unsafe_allow_html=True)
with col2:
    st.markdown(f"""
    <div class="summary-box box-comprobado">
        <div class="summary-title">✅ COMPROBADOS</div>
        <div class="summary-count">{resumen['comprobado']['count']}</div>
        <div class="summary-amount">{money(resumen['comprobado']['total'])}</div>
    </div>""", unsafe_allow_html=True)
with col3:
    st.markdown(f"""
    <div class="summary-box box-no-necesario">
        <div class="summary-title">🚫 NO NECESARIOS</div>
        <div class="summary-count">{resumen['no_necesario']['count']}</div>
        <div class="summary-amount">{money(resumen['no_necesario']['total'])}</div>
    </div>""", unsafe_allow_html=True)

total_monto = sum(r["total"] for r in resumen.values())
monto_resuelto = resumen["comprobado"]["total"] + resumen["no_necesario"]["total"]
if total_monto > 0:
    st.progress(min(monto_resuelto / total_monto, 1.0),
                text=f"Avance por monto: {money(monto_resuelto)} de {money(total_monto)}")

st.write("")

tab_pendientes, tab_comprobados, tab_no_necesarios, tab_resumen = st.tabs(
    ["⏳ Pendientes", "✅ Comprobados", "🚫 No necesarios", "📊 Resumen y descarga"]
)


# ============================================================
# DIÁLOGO: TRABAJAR UN GASTO
# ============================================================
def _solicitud_por_id(solicitud_id):
    if solicitud_id is None:
        return None
    return next((s for s in st.session_state.solicitudes if s["id"] == solicitud_id), None)


@_dialog("📌 Trabajar gasto")
def dialog_trabajar_gasto(idx: int, solicitud_id: int | None = None) -> None:
    df_actual = st.session_state.bank_df
    if idx not in df_actual.index or st.session_state.estados.get(idx) != "pendiente":
        st.info("Este gasto ya no está pendiente (puede que ya se haya comprobado en otra pestaña).")
        return

    sol = _solicitud_por_id(solicitud_id)
    if sol is not None:
        st.markdown(
            f"<div class='card'>🔗 <strong>Vinculado a la solicitud #{sol['No']}</strong> — "
            f"{sol['Applicant']} · {sol['Category']} · {sol['Employee Name']} · "
            f"Request {sol['Request Number'] or '—'}</div>",
            unsafe_allow_html=True,
        )

    gasto = df_actual.loc[idx]
    monto_gasto = abs(float(gasto["Monto"]))

    c1, c2, c3 = st.columns(3)
    c1.metric("Fecha", str(gasto.get("Fecha", "")))
    c2.metric("Descripción", str(gasto.get("Descripción", ""))[:30])
    c3.metric("Monto", money(monto_gasto))

    if st.button("🚫 Marcar como no necesario", key=f"btn_no_necesario_{idx}"):
        clasif_actual = st.session_state.clasificacion_por_gasto.get(idx, {"categoria": "", "material": ""})
        st.session_state.no_necesarios.append({
            "idx": idx,
            "Fecha Estado": gasto.get("Fecha", ""),
            "Descripción Estado": gasto.get("Descripción", ""),
            "Monto Estado": monto_gasto,
            "Categoria": clasif_actual.get("categoria", ""),
            "Material": clasif_actual.get("material", ""),
        })
        st.session_state.estados[idx] = "no_necesario"
        st.session_state.facturas_por_gasto.pop(idx, None)
        if sol is not None:
            # este movimiento no era el correcto para la solicitud; la dejamos
            # pendiente para que se pueda volver a vincular con otro gasto.
            st.session_state.solicitud_en_proceso = None
        _limpiar_seleccion_tabla_pendientes()
        _autoguardar_si_activo()
        st.rerun()

    st.markdown("##### 🏷️ Clasificación del gasto")
    clasif = st.session_state.clasificacion_por_gasto.setdefault(idx, {"categoria": "", "material": ""})
    cl1, cl2 = st.columns(2)
    with cl1:
        clasif["categoria"] = _selector_catalogo(
            "Categoría", "categorias", clasif.get("categoria", ""), f"categoria_{idx}"
        )
    with cl2:
        clasif["material"] = _selector_catalogo(
            "Material", "materiales", clasif.get("material", ""), f"material_{idx}"
        )

    st.session_state.facturas_por_gasto.setdefault(idx, [])
    facturas = st.session_state.facturas_por_gasto[idx]

    st.markdown("##### 📎 Agregar facturas (XML de CFDI, puedes subir varias a la vez)")
    xml_files = st.file_uploader(
        "Sube uno o más XML (CFDI 3.3 o 4.0)", type=["xml"], accept_multiple_files=True,
        key=f"xml_uploader_{idx}",
    )
    if xml_files:
        uuids_existentes = {f["UUID"] for f in facturas} | uuids_consumidos()
        agregadas, duplicadas, con_error = 0, 0, 0
        for xf in xml_files:
            try:
                datos = parse_cfdi(xf.getvalue(), xf.name)
            except CFDIParseError as e:
                st.error(f"No se pudo leer el XML '{xf.name}': {e}")
                con_error += 1
                continue
            if datos["UUID"] in uuids_existentes and datos["UUID"] != "SIN-UUID":
                duplicadas += 1
                continue
            datos["_id"] = next_factura_id()
            facturas.append(datos)
            uuids_existentes.add(datos["UUID"])
            if datos["UUID"] != "SIN-UUID":
                st.session_state.pool_facturas = [
                    f for f in st.session_state.pool_facturas if f["UUID"] != datos["UUID"]
                ]
            if datos.get("Incompleta"):
                st.warning(f"'{xf.name}' se agregó, pero está incompleta: {'; '.join(datos['Advertencias'])}")
            agregadas += 1
        if agregadas:
            st.success(f"{agregadas} factura(s) agregada(s).")
        if duplicadas:
            st.info(f"{duplicadas} factura(s) ya estaban en uso y se omitieron.")

    with st.expander("✏️ Agregar comprobación manual (sin XML)"):
        with st.form(f"manual_form_{idx}", clear_on_submit=True):
            mf_fecha = st.date_input("Fecha de factura", value=datetime.date.today())
            mf_uuid = st.text_input("UUID (opcional)")
            mf_concepto = st.text_input("Concepto/Descripción")
            mf_rfc = st.text_input("RFC Emisor")
            mf_razon = st.text_input("Razón Social Emisor")
            mf_iva = st.number_input("IVA", min_value=0.0, format="%.2f")
            mf_monto = st.number_input("Monto Total", min_value=0.0, format="%.2f")
            guardar_manual = st.form_submit_button("Agregar factura manual")
            if guardar_manual:
                uuid_final = mf_uuid.strip() or "SIN-UUID"
                usados = {f["UUID"] for f in facturas} | uuids_consumidos()
                if uuid_final != "SIN-UUID" and uuid_final in usados:
                    st.error(f"El UUID '{uuid_final}' ya está en uso en otro gasto o factura de esta sesión.")
                else:
                    facturas.append({
                        "_id": next_factura_id(),
                        "Fuente": "Manual",
                        "Archivo": "Manual",
                        "Fecha Factura": str(mf_fecha),
                        "UUID": uuid_final,
                        "Concepto": mf_concepto,
                        "RFC Emisor": mf_rfc,
                        "Razón Social": mf_razon,
                        "IVA": mf_iva,
                        "IVA Retenido": 0.0,
                        "ISR Retenido": 0.0,
                        "Monto Total": mf_monto,
                        "Incompleta": False,
                        "Advertencias": [],
                    })
                    st.rerun()

    st.markdown("##### 🧾 Facturas agregadas a este gasto")
    if facturas:
        df_facturas = pd.DataFrame(facturas)
        columnas_mostrar = [c for c in [
            "Archivo", "UUID", "RFC Emisor", "Concepto", "Fecha Factura", "IVA", "Monto Total", "Incompleta"
        ] if c in df_facturas.columns]
        st.dataframe(
            df_facturas[columnas_mostrar].style.format({"IVA": money, "Monto Total": money}),
            use_container_width=True, hide_index=True,
        )
        if any(f.get("Incompleta") for f in facturas):
            st.markdown(
                "<div class='warn-box'>⚠️ Alguna factura de esta lista quedó incompleta al leer el XML "
                "(revisa la columna 'Incompleta').</div>", unsafe_allow_html=True,
            )

        quitar = st.multiselect(
            "Quitar factura(s) de la lista",
            options=[f["_id"] for f in facturas],
            format_func=lambda fid: next(f"{etiqueta_factura(f)} · {f['UUID']}" for f in facturas if f["_id"] == fid),
            key=f"quitar_{idx}",
        )
        if quitar and st.button("Quitar seleccionadas", key=f"btn_quitar_{idx}"):
            st.session_state.facturas_por_gasto[idx] = [f for f in facturas if f["_id"] not in quitar]
            st.rerun()

        suma_facturas = sum(f["Monto Total"] for f in facturas)
        diferencia = round(monto_gasto - suma_facturas, 2)

        cc1, cc2, cc3 = st.columns(3)
        cc1.metric("Monto del gasto", money(monto_gasto))
        cc2.metric("Suma de facturas", money(suma_facturas))
        cc3.metric("Diferencia", money(diferencia))

        if abs(diferencia) <= 0.01:
            st.markdown(
                f"<div class='success-box'>✅ Gasto comprobado correctamente. Diferencia: {money(diferencia)}</div>",
                unsafe_allow_html=True,
            )
            if st.button("➕ Añadir a los registros", key=f"btn_guardar_{idx}", type="primary"):
                st.session_state.concatenados.append({
                    "idx": idx,
                    "Fecha Estado": gasto.get("Fecha", ""),
                    "Descripción Estado": gasto.get("Descripción", ""),
                    "Monto Estado": monto_gasto,
                    "Categoria": clasif.get("categoria", ""),
                    "Material": clasif.get("material", ""),
                    "Facturas": facturas,
                    "Solicitud": sol,
                })
                st.session_state.estados[idx] = "comprobado"
                st.session_state.facturas_por_gasto.pop(idx, None)
                if sol is not None:
                    sol["estado"] = "comprobado"
                    sol["idx_vinculado"] = idx
                    st.session_state.solicitud_en_proceso = None
                st.success("Gasto añadido a los registros.")
                _limpiar_seleccion_tabla_pendientes()
                _autoguardar_si_activo()
                st.rerun()
        else:
            st.markdown(
                f"<div class='error-box'>❌ La suma de facturas no coincide con el gasto. "
                f"Diferencia: {money(diferencia)}</div>", unsafe_allow_html=True,
            )
    else:
        st.caption("Aún no has agregado ninguna factura para este gasto.")


# ============================================================
# BITÁCORA DE SOLICITUDES DE REEMBOLSO
# ============================================================
def _bitacora_a_excel_bytes(solicitudes: list[dict], concatenados: list[dict]) -> bytes:
    """Arma el Excel de la bitácora con el mismo formato/orden que la hoja "Details"
    de la plantilla que compartió el usuario: columnas A-I son los datos de la
    solicitud (No., Applicant, Category, Description, Linked Request No., Number of
    Days, Total Number of People, Employee Name y Material -en el lugar donde la
    plantilla trae "Client Name", que aquí no se captura-); columnas J-N son la
    comprobación ya hecha con el estado de cuenta/XML (Payment Date, Expense Outflow
    Amt, No., CFDI Folio, Reimbursement Cap); se agrega una columna "Status" extra al
    final para saber de un vistazo qué sigue pendiente."""
    from io import BytesIO

    def _comprobacion_de(sol: dict) -> dict:
        if sol.get("estado") != "comprobado" or sol.get("idx_vinculado") is None:
            return {}
        registro = next((c for c in concatenados if c["idx"] == sol["idx_vinculado"]), None)
        if registro is None:
            return {}
        facturas = registro.get("Facturas", [])
        suma_facturas = round(sum(f.get("Monto Total", 0) or 0 for f in facturas), 2)
        return {
            "Payment Date": registro.get("Fecha Estado", ""),
            "Expense Outflow Amt": round(abs(float(registro.get("Monto Estado", 0) or 0)), 2),
            "Bank No": sol["No"],
            "CFDI Folio": "; ".join(f.get("UUID", "") for f in facturas if f.get("UUID")),
            "Reimbursement Cap": suma_facturas,
        }

    columnas_df = [
        "No", "Applicant", "Category", "Description", "Linked Request No",
        "Number of Days", "Total Number of People", "Employee Name", "Material",
        "Payment Date", "Expense Outflow Amt", "Bank No", "CFDI Folio",
        "Reimbursement Cap", "Status",
    ]
    encabezados = [
        "No.", "Applicant", "Category", "Description", "Linked Request No.",
        "Number of Days", "Total Number of People", "Employee Name", "Material",
        "Payment Date", "Expense Outflow Amt", "No.", "CFDI Folio",
        "Reimbursement Cap (With IVA)", "Status",
    ]

    filas = []
    for sol in solicitudes:
        fila = {
            "No": sol.get("No"),
            "Applicant": sol.get("Applicant") or "",
            "Category": sol.get("Category") or "",
            "Description": sol.get("Description") or "",
            "Linked Request No": sol.get("Request Number") or "",
            "Number of Days": sol.get("Number of Days", 0),
            "Total Number of People": sol.get("Number of People", 0),
            "Employee Name": sol.get("Employee Name") or "",
            "Material": sol.get("Material") or "",
            "Status": "Comprobado" if sol.get("estado") == "comprobado" else "Pendiente",
        }
        fila.update(_comprobacion_de(sol))
        filas.append(fila)

    df = pd.DataFrame(filas, columns=columnas_df) if filas else pd.DataFrame(columns=columnas_df)

    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="Details")
        workbook = writer.book
        ws = writer.sheets["Details"]

        header_fmt = workbook.add_format({
            "bold": True, "bg_color": C_BITACORA_HEADER, "font_color": "white",
            "border": 1, "align": "center", "valign": "vcenter", "text_wrap": True,
        })
        for col_idx, titulo in enumerate(encabezados):
            ws.write(0, col_idx, titulo, header_fmt)

        money_fmt = workbook.add_format({"num_format": "$#,##0.00"})
        for nombre in ("Expense Outflow Amt", "Reimbursement Cap"):
            col_idx = columnas_df.index(nombre)
            ws.set_column(col_idx, col_idx, 20, money_fmt)

        anchos = {
            "No": 6, "Applicant": 14, "Category": 24, "Description": 26,
            "Linked Request No": 24, "Number of Days": 12, "Total Number of People": 14,
            "Employee Name": 24, "Material": 16, "Payment Date": 14, "Bank No": 8,
            "CFDI Folio": 38, "Status": 12,
        }
        for nombre, ancho in anchos.items():
            ws.set_column(columnas_df.index(nombre), columnas_df.index(nombre), ancho)

    return output.getvalue()


def _mostrar_seccion_solicitudes() -> None:
    st.markdown("### 📝 Nueva solicitud de reembolso")
    st.caption(
        "Registra aquí cada gasto conforme se va realizando, antes de tener el estado de cuenta "
        "o la factura. Queda guardado en la bitácora de abajo; cuando el movimiento ya aparezca "
        "en el estado de cuenta, usa su botón «🔗 Comprobar gasto» para vincularlo y adjuntar la "
        "factura, igual que con cualquier otro gasto pendiente."
    )
    v = st.session_state.solicitud_form_version
    with st.container(border=True):
        c1, c2, c3 = st.columns(3)
        with c1:
            applicant = _selector_catalogo("Applicant", "aplicantes", "", f"sol_applicant_{v}")
        with c2:
            category = _selector_catalogo("Category", "categorias_solicitud", "", f"sol_category_{v}")
        with c3:
            employee = _selector_catalogo("Employee name", "empleados", "", f"sol_employee_{v}")

        c4, c5, c6 = st.columns(3)
        with c4:
            material = _selector_catalogo("Material", "materiales", "", f"sol_material_{v}")
        with c5:
            description = st.text_input("Description", key=f"sol_description_{v}")
        with c6:
            request_number = st.text_input("Request number", key=f"sol_request_{v}")

        c7, c8, _c9 = st.columns(3)
        with c7:
            number_of_days = st.number_input("Number of days", min_value=0, step=1, key=f"sol_days_{v}")
        with c8:
            number_of_people = st.number_input("Number of people", min_value=0, step=1, key=f"sol_people_{v}")

        if st.button("➕ Agregar a la bitácora", key=f"sol_btn_guardar_{v}", type="primary"):
            if not applicant.strip():
                st.error("«Applicant» es obligatorio.")
            else:
                nuevo_no = next_solicitud_id()
                st.session_state.solicitudes.append({
                    "id": nuevo_no,
                    "No": nuevo_no,
                    "Applicant": applicant.strip(),
                    "Category": category,
                    "Description": description.strip(),
                    "Material": material,
                    "Employee Name": employee,
                    "Request Number": request_number.strip(),
                    "Number of Days": int(number_of_days),
                    "Number of People": int(number_of_people),
                    "estado": "pendiente",
                    "idx_vinculado": None,
                })
                _limpiar_formulario_solicitud()
                _autoguardar_si_activo()
                st.success(f"Solicitud #{nuevo_no} agregada a la bitácora.")
                st.rerun()

    if not st.session_state.solicitudes:
        st.caption("Todavía no hay solicitudes registradas en la bitácora.")
        st.divider()
        return

    st.markdown("##### 🧾 Bitácora de solicitudes")
    st.caption("Mismo orden de columnas que la hoja «Details» de la plantilla (Material ocupa el "
               "lugar de «Client Name», que aquí no se captura).")

    if st.session_state.solicitud_en_proceso is not None:
        sol_activa = _solicitud_por_id(st.session_state.solicitud_en_proceso)
        if sol_activa is not None:
            st.markdown(
                f"<div class='warn-box'>🔗 Vinculando la solicitud #{sol_activa['No']} "
                f"({sol_activa['Applicant']} · {sol_activa['Category']}): selecciona su gasto en la "
                f"tabla de «Gastos pendientes» de abajo y pulsa «Abrir».</div>",
                unsafe_allow_html=True,
            )
            if st.button("Cancelar vinculación", key="btn_cancelar_vinculacion"):
                st.session_state.solicitud_en_proceso = None
                st.rerun()
        else:
            st.session_state.solicitud_en_proceso = None

    anchos_bitacora = [0.7, 1.5, 1.9, 2.2, 1.6, 0.8, 0.9, 1.8, 1.3, 2]
    titulos_bitacora = [
        "No.", "Applicant", "Category", "Description", "Request #",
        "Días", "Personas", "Employee", "Material", "",
    ]
    encabezados = st.columns(anchos_bitacora)
    for col, titulo in zip(encabezados, titulos_bitacora):
        col.markdown(f"<div class='bitacora-header'>{titulo}</div>", unsafe_allow_html=True)

    for sol in st.session_state.solicitudes:
        cols = st.columns(anchos_bitacora)
        cols[0].write(f"#{sol['No']}")
        cols[1].write(sol["Applicant"])
        cols[2].write(sol["Category"] or "—")
        cols[3].write(sol.get("Description") or "—")
        cols[4].write(sol["Request Number"] or "—")
        cols[5].write(sol["Number of Days"])
        cols[6].write(sol["Number of People"])
        cols[7].write(sol["Employee Name"] or "—")
        cols[8].write(sol["Material"] or "—")
        with cols[9]:
            if sol["estado"] == "comprobado":
                st.caption(f"✅ Comprobado (#{sol['idx_vinculado']})")
            else:
                ya_vinculando_otra = st.session_state.solicitud_en_proceso not in (None, sol["id"])
                if st.button("🔗 Comprobar gasto", key=f"btn_comprobar_sol_{sol['id']}", disabled=ya_vinculando_otra):
                    st.session_state.solicitud_en_proceso = sol["id"]
                    st.rerun()

    st.download_button(
        "📥 Descargar bitácora (formato Details, .xlsx)",
        data=_bitacora_a_excel_bytes(st.session_state.solicitudes, st.session_state.concatenados),
        file_name=f"bitacora_caja_chica_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="btn_descargar_solicitudes",
    )
    st.divider()


# ============================================================
# PESTAÑA: PENDIENTES
# ============================================================
with tab_pendientes:
    _mostrar_seccion_solicitudes()

    pendientes_idx = [i for i, e in st.session_state.estados.items() if e == "pendiente"]

    if not pendientes_idx:
        st.success("No hay gastos pendientes por comprobar. 🎉")
    else:
        busqueda = st.text_input("🔎 Buscar por descripción", key="busqueda_pendientes")
        df_pend = df.loc[df.index.intersection(pendientes_idx)].copy()
        if busqueda and "Descripción" in df_pend.columns:
            df_pend = df_pend[df_pend["Descripción"].astype(str).str.contains(busqueda, case=False, na=False)]

        columnas_mostrar = [c for c in ["Fecha", "Descripción", "Monto", "Saldo"] if c in df_pend.columns]

        with st.expander(f"📋 Gastos pendientes ({len(df_pend)})", expanded=True):
            if df_pend.empty:
                st.caption("Ningún gasto coincide con la búsqueda.")
            else:
                st.caption("👆 Haz clic en una fila para seleccionar el gasto y luego pulsa «Abrir».")
                version_tabla = st.session_state.get("tabla_pendientes_version", 0)
                evento_tabla = st.dataframe(
                    df_pend[columnas_mostrar],
                    use_container_width=True,
                    hide_index=False,
                    on_select="rerun",
                    selection_mode="single-row",
                    key=f"tabla_pendientes_{version_tabla}",
                    column_config={
                        "Monto": st.column_config.NumberColumn("Monto", format="$%.2f"),
                        "Saldo": st.column_config.NumberColumn("Saldo", format="$%.2f"),
                    },
                )
                filas_sel = evento_tabla.selection.rows if evento_tabla and evento_tabla.selection else []
                # Red de seguridad: si la tabla cambió de tamaño entre una ejecución y
                # otra (se comprobó/revirtió un gasto, cambió la búsqueda, etc.) la
                # posición seleccionada puede haber quedado obsoleta o fuera de rango.
                filas_sel = [f for f in filas_sel if 0 <= f < len(df_pend)]

                if filas_sel:
                    idx_sel = df_pend.index[filas_sel[0]]
                    desc_sel = str(df_pend.loc[idx_sel, "Descripción"])[:50] if "Descripción" in df_pend.columns else ""
                    fecha_sel = df_pend.loc[idx_sel, "Fecha"] if "Fecha" in df_pend.columns else ""
                    col_info, col_btn = st.columns([4, 1])
                    with col_info:
                        st.markdown(
                            f"**Seleccionado:** #{idx_sel} · {fecha_sel} · {desc_sel} · "
                            f"{money(abs(df_pend.loc[idx_sel, 'Monto']))}"
                        )
                    with col_btn:
                        if st.button("🔍 Abrir", use_container_width=True, key="btn_abrir_gasto", type="primary"):
                            dialog_trabajar_gasto(idx_sel, solicitud_id=st.session_state.solicitud_en_proceso)
                else:
                    st.caption("Ningún gasto seleccionado todavía.")

    st.divider()
    with st.expander("🔁 Emparejamiento automático de facturas", expanded=False):
        st.caption(
            "Sube todos los XML del periodo de una vez. Para cada gasto pendiente sin facturas ya "
            "asignadas manualmente, buscamos la factura disponible cuyo monto esté más cercano. Las "
            "diferencias de 1 centavo o menos se pueden aplicar directo; el resto necesita tu validación."
        )
        xml_bulk = st.file_uploader(
            "Sube uno o más XML de CFDI para emparejar automáticamente",
            type=["xml"], accept_multiple_files=True, key="xml_bulk_uploader",
        )
        if xml_bulk:
            usados = uuids_consumidos() | {f["UUID"] for f in st.session_state.pool_facturas if f["UUID"] != "SIN-UUID"}
            agregadas, duplicadas, con_error = 0, 0, 0
            for xf in xml_bulk:
                try:
                    datos = parse_cfdi(xf.getvalue(), xf.name)
                except CFDIParseError as e:
                    st.error(f"No se pudo leer el XML '{xf.name}': {e}")
                    con_error += 1
                    continue
                if datos["UUID"] in usados and datos["UUID"] != "SIN-UUID":
                    duplicadas += 1
                    continue
                datos["_id"] = next_factura_id()
                st.session_state.pool_facturas.append(datos)
                usados.add(datos["UUID"])
                agregadas += 1
            if agregadas:
                st.success(f"{agregadas} factura(s) agregada(s) al grupo por emparejar.")
            if duplicadas:
                st.info(f"{duplicadas} factura(s) ya estaban en uso en la sesión y se omitieron.")

        pendientes_idx_match = [i for i, e in st.session_state.estados.items() if e == "pendiente"]
        sugerencias = calcular_matches_automaticos(
            df, pendientes_idx_match, st.session_state.pool_facturas, st.session_state.facturas_por_gasto
        )
        sug_exactas = [s for s in sugerencias if s["tipo"] == "exacto"]
        sug_revision = [s for s in sugerencias if s["tipo"] == "revision"]
        ids_sugeridos = {s["factura"]["_id"] for s in sugerencias}

        def _df_para_editor(lista_sugerencias, incluir_default):
            return pd.DataFrame([{
                "Incluir": incluir_default,
                "Fecha gasto": str(s["gasto"].get("Fecha", "")),
                "Descripción gasto": str(s["gasto"].get("Descripción", ""))[:40],
                "Monto gasto": s["monto_gasto"],
                "Etiqueta factura": etiqueta_factura(s["factura"]),
                "Factura (UUID)": s["factura"]["UUID"],
                "RFC Emisor": s["factura"]["RFC Emisor"],
                "Monto factura": s["factura"]["Monto Total"],
                "Diferencia": s["diferencia"],
                "Categoría": "",
                "Material": "",
            } for s in lista_sugerencias])

        def _aplicar_sugerencias_editadas(df_editado, sugerencias_orig):
            filas = df_editado.reset_index(drop=True)
            if len(filas) != len(sugerencias_orig):
                st.error("La tabla de sugerencias cambió inesperadamente; vuelve a intentarlo.")
                return 0
            aplicados = 0
            for i in range(len(sugerencias_orig)):
                fila = filas.iloc[i]
                if not bool(fila["Incluir"]):
                    continue
                sug = sugerencias_orig[i]
                idx = sug["idx"]
                factura = sug["factura"]
                categoria = fila.get("Categoría", "") or ""
                material = fila.get("Material", "") or ""
                st.session_state.concatenados.append({
                    "idx": idx,
                    "Fecha Estado": sug["gasto"].get("Fecha", ""),
                    "Descripción Estado": sug["gasto"].get("Descripción", ""),
                    "Monto Estado": sug["monto_gasto"],
                    "Categoria": categoria,
                    "Material": material,
                    "Facturas": [factura],
                })
                st.session_state.estados[idx] = "comprobado"
                st.session_state.facturas_por_gasto.pop(idx, None)
                st.session_state.clasificacion_por_gasto[idx] = {"categoria": categoria, "material": material}
                st.session_state.pool_facturas = [
                    f for f in st.session_state.pool_facturas if f["_id"] != factura["_id"]
                ]
                aplicados += 1
            return aplicados

        col_cfg = {
            "Monto gasto": st.column_config.NumberColumn(format="$%.2f"),
            "Monto factura": st.column_config.NumberColumn(format="$%.2f"),
            "Diferencia": st.column_config.NumberColumn(format="$%.2f"),
            "Categoría": st.column_config.SelectboxColumn(options=st.session_state.categorias),
            "Material": st.column_config.SelectboxColumn(options=st.session_state.materiales),
        }
        cols_disabled = ["Fecha gasto", "Descripción gasto", "Monto gasto", "Etiqueta factura",
                          "Factura (UUID)", "RFC Emisor", "Monto factura", "Diferencia"]

        if sug_exactas:
            st.markdown("##### ✅ Coincidencias exactas (diferencia ≤ $0.01)")
            edited_exactas = st.data_editor(
                _df_para_editor(sug_exactas, incluir_default=True), use_container_width=True, hide_index=True,
                disabled=cols_disabled, column_config=col_cfg, key="editor_exactas",
            )
            if st.button("➕ Aplicar coincidencias exactas seleccionadas", type="primary", key="btn_aplicar_exactas"):
                n = _aplicar_sugerencias_editadas(edited_exactas, sug_exactas)
                if n:
                    st.success(f"{n} gasto(s) comprobado(s) automáticamente.")
                    _limpiar_seleccion_tabla_pendientes()
                    _autoguardar_si_activo()
                    st.rerun()
                else:
                    st.warning("No marcaste ninguna fila con 'Incluir'.")

        if sug_revision:
            st.markdown("##### 🔍 Requieren tu validación (diferencia mayor a $0.01)")
            edited_revision = st.data_editor(
                _df_para_editor(sug_revision, incluir_default=False), use_container_width=True, hide_index=True,
                disabled=cols_disabled, column_config=col_cfg, key="editor_revision",
            )
            if st.button("➕ Aplicar coincidencias validadas", key="btn_aplicar_revision"):
                n = _aplicar_sugerencias_editadas(edited_revision, sug_revision)
                if n:
                    st.success(f"{n} gasto(s) comprobado(s) tras validación.")
                    _limpiar_seleccion_tabla_pendientes()
                    _autoguardar_si_activo()
                    st.rerun()
                else:
                    st.warning("Marca 'Incluir' en las filas que quieras confirmar antes de aplicar.")

        pool_sin_match = [f for f in st.session_state.pool_facturas if f["_id"] not in ids_sugeridos]
        if pool_sin_match:
            st.caption(f"📥 {len(pool_sin_match)} factura(s) sin coincidencia sugerida — quedan disponibles "
                       "para asignar manualmente desde 'Abrir' en un gasto pendiente.")
            df_pool = pd.DataFrame(pool_sin_match).drop(columns=["_id"])
            cols_pool = [c for c in df_pool.columns if c != "Advertencias"]
            st.dataframe(
                df_pool[cols_pool].style.format({"IVA": money, "Monto Total": money}),
                use_container_width=True, hide_index=True,
            )


# ============================================================
# PESTAÑA: COMPROBADOS
# ============================================================
def _revertir_a_pendiente(idx: int, origen: str) -> None:
    """Regresa un gasto ya resuelto (comprobado o no necesario) a estado pendiente,
    para poder corregirlo. Las facturas que tenía asignadas quedan disponibles de
    nuevo para editarlas en el diálogo del gasto."""
    if origen == "comprobado":
        registro = next((r for r in st.session_state.concatenados if r["idx"] == idx), None)
        if registro is not None:
            st.session_state.facturas_por_gasto[idx] = registro["Facturas"]
            st.session_state.concatenados = [r for r in st.session_state.concatenados if r["idx"] != idx]
            sol = registro.get("Solicitud")
            if sol is not None:
                sol_actual = _solicitud_por_id(sol["id"])
                if sol_actual is not None:
                    sol_actual["estado"] = "pendiente"
                    sol_actual["idx_vinculado"] = None
    else:
        st.session_state.no_necesarios = [r for r in st.session_state.no_necesarios if r.get("idx") != idx]
    st.session_state.estados[idx] = "pendiente"
    _limpiar_seleccion_tabla_pendientes()
    _autoguardar_si_activo()


with tab_comprobados:
    if st.session_state.concatenados:
        busqueda_c = st.text_input("🔎 Buscar por descripción", key="busqueda_comprobados")
        registros = st.session_state.concatenados
        if busqueda_c:
            registros = [
                r for r in registros
                if busqueda_c.lower() in str(r.get("Descripción Estado", "")).lower()
            ]

        for reg in registros:
            facturas = reg["Facturas"]
            suma_facturas = sum(f["Monto Total"] for f in facturas)
            marca_incompleta = " ⚠️" if any(f.get("Incompleta") for f in facturas) else ""
            with st.expander(
                f"#{reg['idx']} · {reg.get('Fecha Estado', '')} · "
                f"{str(reg.get('Descripción Estado', ''))[:40]} · {money(reg.get('Monto Estado', 0))}{marca_incompleta}"
            ):
                st.write(f"**Categoría:** {reg.get('Categoria', '') or '—'}  ·  **Material:** {reg.get('Material', '') or '—'}")
                df_f = pd.DataFrame(facturas)
                cols_f = [c for c in ["Archivo", "UUID", "RFC Emisor", "Concepto", "IVA", "Monto Total"] if c in df_f.columns]
                st.dataframe(df_f[cols_f].style.format({"IVA": money, "Monto Total": money}),
                             use_container_width=True, hide_index=True)
                st.caption(f"Suma de facturas: {money(suma_facturas)}  ·  Diferencia: "
                           f"{money(round(abs(reg.get('Monto Estado', 0)) - suma_facturas, 2))}")
                if st.button("↩️ Revertir a pendiente", key=f"revertir_comp_{reg['idx']}"):
                    _revertir_a_pendiente(reg["idx"], "comprobado")
                    st.rerun()
    else:
        st.caption("Todavía no hay gastos comprobados.")


# ============================================================
# PESTAÑA: NO NECESARIOS
# ============================================================
with tab_no_necesarios:
    if st.session_state.no_necesarios:
        busqueda_n = st.text_input("🔎 Buscar por descripción", key="busqueda_no_necesarios")
        registros_nn = st.session_state.no_necesarios
        if busqueda_n:
            registros_nn = [
                r for r in registros_nn
                if busqueda_n.lower() in str(r.get("Descripción Estado", "")).lower()
            ]
        df_nn = pd.DataFrame(registros_nn)
        cols_nn = [c for c in df_nn.columns if c != "idx"]
        st.dataframe(df_nn[cols_nn].style.format({"Monto Estado": money}), use_container_width=True, hide_index=True)

        opciones_nn = {r["idx"]: f"#{r['idx']} · {str(r.get('Descripción Estado', ''))[:40]} · {money(r.get('Monto Estado', 0))}"
                       for r in registros_nn if "idx" in r}
        if opciones_nn:
            col_sel_nn, col_btn_nn = st.columns([4, 1])
            with col_sel_nn:
                idx_revertir = st.selectbox(
                    "Gasto a revertir a pendiente", options=list(opciones_nn.keys()),
                    format_func=lambda i: opciones_nn[i], key="selector_revertir_nn",
                )
            with col_btn_nn:
                st.write("")
                st.write("")
                if st.button("↩️ Revertir", use_container_width=True, key="btn_revertir_nn"):
                    _revertir_a_pendiente(idx_revertir, "no_necesario")
                    st.rerun()
    else:
        st.caption("Todavía no hay gastos marcados como no necesarios.")


# ============================================================
# PESTAÑA: RESUMEN Y DESCARGA
# ============================================================
with tab_resumen:
    checksum = checksum_reconciliacion(df, st.session_state.estados, st.session_state.concatenados)
    st.markdown("##### 🧮 Checksum de cuadre")
    cs1, cs2 = st.columns(2)
    cs1.metric("Total del estado de cuenta", money(checksum["total_estado_cuenta"]))
    cs2.metric("Total repartido (pendiente + comprobado + no necesario)", money(checksum["total_por_estados"]))
    if checksum["cuadra"]:
        st.markdown("<div class='success-box'>✅ El total cuadra correctamente.</div>", unsafe_allow_html=True)
    else:
        st.markdown("<div class='error-box'>❌ El total no cuadra. Revisa los movimientos.</div>", unsafe_allow_html=True)
    if checksum["comprobados_descuadrados"]:
        st.markdown("<div class='warn-box'>⚠️ Hay gastos comprobados cuya suma de facturas no coincide "
                     "exactamente con el monto del gasto:</div>", unsafe_allow_html=True)
        st.dataframe(pd.DataFrame(checksum["comprobados_descuadrados"]), use_container_width=True, hide_index=True)

    st.divider()
    st.markdown("##### 📥 Descargar Excel del historial")

    def fila_excel_comprobado(no, registro):
        facturas = registro["Facturas"]
        suma_facturas = sum(f["Monto Total"] for f in facturas)
        suma_iva = sum(f.get("IVA", 0.0) for f in facturas)
        suma_iva_ret = sum(f.get("IVA Retenido", 0.0) for f in facturas)
        suma_isr_ret = sum(f.get("ISR Retenido", 0.0) for f in facturas)
        return {
            "No": no,
            "Bank date": registro["Fecha Estado"],
            "Bank amt": registro["Monto Estado"],
            "Category": registro.get("Categoria", ""),
            "Material": registro.get("Material", ""),
            "Concepto": "; ".join(f.get("Concepto", "") for f in facturas if f.get("Concepto")),
            "Month": mes_es(registro["Fecha Estado"]),
            "UUID date": "; ".join(f.get("Fecha Factura", "") for f in facturas),
            "UUID": "; ".join(f.get("UUID", "") for f in facturas),
            "UUID amt": suma_facturas,
            "IVA": suma_iva,
            "IVA Retenido": suma_iva_ret,
            "ISR Retenido": suma_isr_ret,
            "Diff": round(abs(float(registro["Monto Estado"])) - suma_facturas, 2),
            "Incompleta": "Sí" if any(f.get("Incompleta") for f in facturas) else "",
        }

    df_comprobados_vista = None
    if st.session_state.concatenados:
        filas = [fila_excel_comprobado(n + 1, r) for n, r in enumerate(st.session_state.concatenados)]
        df_comprobados_vista = pd.DataFrame(filas)

    if st.session_state.concatenados or st.session_state.no_necesarios:
        from io import BytesIO
        output = BytesIO()
        with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
            workbook = writer.book
            money_fmt = workbook.add_format({"num_format": "$#,##0.00"})
            bold_fmt = workbook.add_format({"bold": True})

            if df_comprobados_vista is not None:
                columnas_finales = [
                    "No", "Bank date", "Bank amt", "Category", "Material", "Concepto", "Month",
                    "UUID date", "UUID", "UUID amt", "IVA", "IVA Retenido", "ISR Retenido", "Diff", "Incompleta",
                ]
                df_export = df_comprobados_vista[columnas_finales].copy()
                df_export.to_excel(writer, index=False, sheet_name="Comprobados")
                ws = writer.sheets["Comprobados"]
                for col_name in ["Bank amt", "UUID amt", "IVA", "IVA Retenido", "ISR Retenido", "Diff"]:
                    col_idx = columnas_finales.index(col_name)
                    ws.set_column(col_idx, col_idx, 14, money_fmt)
                fila_total = len(df_export) + 1
                ws.write(fila_total, 0, "Total", bold_fmt)
                for col_name in ["Bank amt", "UUID amt", "IVA", "IVA Retenido", "ISR Retenido", "Diff"]:
                    col_idx = columnas_finales.index(col_name)
                    ws.write(fila_total, col_idx, df_export[col_name].sum(), money_fmt)
                ws.set_column(1, 1, 12)
                ws.set_column(3, 4, 16)
                ws.set_column(5, 5, 30)
                ws.set_column(7, 8, 22)

            if st.session_state.no_necesarios:
                cols_nn_export = [c for c in ["Fecha Estado", "Descripción Estado", "Monto Estado", "Categoria", "Material"]
                                   if c in pd.DataFrame(st.session_state.no_necesarios).columns]
                pd.DataFrame(st.session_state.no_necesarios)[cols_nn_export].to_excel(
                    writer, index=False, sheet_name="No necesarios"
                )

            df_pend_export = df[df.index.map(lambda i: st.session_state.estados.get(i) == "pendiente")]
            if not df_pend_export.empty:
                # a diferencia de la versión original, aquí SÍ se conserva la columna 'Monto'
                # (antes se perdía en el Excel exportado).
                df_pend_export.to_excel(writer, index=False, sheet_name="Sin comprobar")

        st.download_button(
            label="📥 Descargar Excel (todo el historial)",
            data=output.getvalue(),
            file_name=f"Comprobaciones_{datetime.datetime.now().strftime('%Y%m%d')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    else:
        st.caption("Todavía no hay nada que exportar.")
