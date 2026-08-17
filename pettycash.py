import streamlit as st
import pandas as pd
import xml.etree.ElementTree as ET
import datetime
import json
from io import BytesIO

# ============================================================
# PALETA DE COLORES — "Neutro Claro Ejecutivo"
# ============================================================
C_FONDO = "#05423F"          # fondo general (tema claro)
C_TARJETA = "#FFFFFF"        # fondo de tarjetas/paneles
C_BORDE = "#D8DEE4"          # bordes sutiles
C_TEXTO_OSCURO = "#1A1F26"   # texto principal sobre fondo claro
C_TEAL_VIVO = "#0E5C73"      # acento primario
C_CORAL_ALERTA = "#C0392B"   # alerta / error
C_AMARILLO_ACENTO = "#B8860B"  # acento secundario (dorado apagado)
C_GRIS_NEUTRO = "#8A94A6"    # neutro
C_VERDE_OK = "#2E8B57"       # éxito

st.set_page_config(page_title="Comprobación Caja Chica", layout="wide")

MESES_ES = [
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]

# ============================================================
# ESTADO DE SESIÓN
# ============================================================
def init_state():
    defaults = {
        "banco": None,
        "bank_df": None,
        "bank_file_id": None,          # (nombre, tamaño) del archivo cargado, para detectar cambios
        "estados": {},                 # idx -> "pendiente" | "comprobado" | "no_necesario"
        "facturas_por_gasto": {},      # idx -> lista de dicts (facturas agregadas a ese gasto)
        "clasificacion_por_gasto": {}, # idx -> {"categoria": str, "material": str}
        "pool_facturas": [],           # facturas subidas en bloque, aún no asignadas a ningún gasto
        "modo_trabajo": None,          # None | "auto" | "manual" -- qué panel de trabajo está activo
        "tabla_expandida": True,       # controla si la tabla de movimientos se muestra o está colapsada
        "selected_idx": None,
        "concatenados": [],            # registros finales de gastos comprobados (1 dict por gasto, con lista de facturas)
        "no_necesarios": [],           # registros de gastos marcados como no necesarios
        "factura_counter": 0,          # id incremental interno para poder quitar facturas
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

init_state()

# ============================================================
# ESTILOS
# ============================================================
st.markdown(f"""
<style>
.stApp {{ background-color: {C_FONDO}; color: {C_TEXTO_OSCURO}; }}
table, th, td {{ border: 1px solid {C_BORDE}; border-collapse: collapse; padding: 6px; }}
th {{ background-color: {C_TEAL_VIVO}; color: white; }}
td {{ color: {C_TEXTO_OSCURO}; background-color: {C_TARJETA}; }}

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
.box-comprobado {{ background-color: {C_TEAL_VIVO}; }}
.box-no-necesario {{ background-color: {C_AMARILLO_ACENTO}; color: {C_TEXTO_OSCURO}; }}

.success-box {{
    background-color: {C_VERDE_OK}; color: white; padding: 10px; border-radius: 6px; font-weight: 600;
}}
.error-box {{
    background-color: {C_CORAL_ALERTA}; color: white; padding: 10px; border-radius: 6px; font-weight: 600;
}}
.badge {{
    padding: 2px 10px; border-radius: 12px; font-size: 0.8rem; font-weight: 600;
}}
.badge-pendiente {{ background-color: {C_GRIS_NEUTRO}; color: white; }}
.badge-comprobado {{ background-color: {C_TEAL_VIVO}; color: white; }}
.badge-no-necesario {{ background-color: {C_AMARILLO_ACENTO}; color: {C_TEXTO_OSCURO}; }}
</style>
""", unsafe_allow_html=True)

st.title("💰 Comprobación de Caja Chica")

# ============================================================
# HELPERS GENERALES
# ============================================================
def money(x):
    try:
        return f"${float(x):,.2f}"
    except (ValueError, TypeError):
        return "$0.00"


def status_label(estado):
    return {
        "pendiente": "⏳ Sin comprobar",
        "comprobado": "✅ Comprobado",
        "no_necesario": "🚫 No necesario",
    }.get(estado, estado)


def mes_es(fecha_str):
    """Convierte 'YYYY-MM-DD' (o similar) a 'Mes YYYY' en español."""
    try:
        f = pd.to_datetime(fecha_str)
        return f"{MESES_ES[f.month - 1]} {f.year}"
    except Exception:
        return ""


def next_factura_id():
    st.session_state.factura_counter += 1
    return st.session_state.factura_counter


def uuids_consumidos():
    """UUIDs que ya quedaron asignados a un gasto (pendiente de guardar o ya guardado en el historial).
    NO incluye el pool automático: una factura ahí sigue disponible, no está 'usada' todavía."""
    usados = set()
    for facs in st.session_state.facturas_por_gasto.values():
        usados.update(f["UUID"] for f in facs if f["UUID"] != "SIN-UUID")
    for reg in st.session_state.concatenados:
        usados.update(f["UUID"] for f in reg["Facturas"] if f["UUID"] != "SIN-UUID")
    return usados


# Umbral para sugerir una coincidencia "a revisar": diferencia absoluta menor a este
# monto O menor al 30% del gasto (lo que sea más laxo), para no inundar de sugerencias
# absurdas cuando el monto no se parece en nada.
UMBRAL_SUGERENCIA_ABS = 500.0
UMBRAL_SUGERENCIA_PCT = 0.30


def calcular_matches_automaticos(df, pendientes_idx, pool):
    """
    Para cada gasto pendiente (sin facturas ya asignadas manualmente), busca la factura
    disponible en el pool cuyo monto esté más cercano. Regresa una lista de sugerencias:
    {"idx": ..., "gasto": Series, "monto_gasto": float, "factura": dict, "diferencia": float, "tipo": "exacto"|"revision"}
    Cada factura del pool se sugiere para un solo gasto (primero en llegar, primero en servir).
    """
    usados_pool_ids = set()
    sugerencias = []
    for idx in pendientes_idx:
        if st.session_state.facturas_por_gasto.get(idx):
            continue  # este gasto ya tiene facturas asignadas manualmente, se deja fuera del auto-match
        gasto = df.loc[idx]
        monto_gasto = float(gasto["Monto"])
        disponibles = [f for f in pool if f["_id"] not in usados_pool_ids]
        if not disponibles:
            continue
        mejor = min(disponibles, key=lambda f: abs(monto_gasto - f["Monto Total"]))
        diferencia = round(monto_gasto - mejor["Monto Total"], 2)
        umbral = max(UMBRAL_SUGERENCIA_ABS, monto_gasto * UMBRAL_SUGERENCIA_PCT)
        if abs(diferencia) <= umbral:
            tipo = "exacto" if abs(diferencia) <= 0.01 else "revision"
            sugerencias.append({
                "idx": idx, "gasto": gasto, "monto_gasto": monto_gasto,
                "factura": mejor, "diferencia": diferencia, "tipo": tipo,
            })
            usados_pool_ids.add(mejor["_id"])
    return sugerencias


def aplicar_sugerencias(df_editado, sugerencias):
    """Aplica las filas marcadas como 'Incluir' de la tabla editada: crea el registro comprobado,
    actualiza estados y libera la factura del pool."""
    aplicados = 0
    for (_, fila), sug in zip(df_editado.iterrows(), sugerencias):
        if not bool(fila["Incluir"]):
            continue
        idx = sug["idx"]
        gasto = sug["gasto"]
        factura = sug["factura"]
        categoria = fila.get("Categoría", "")
        material = fila.get("Material", "")
        st.session_state.concatenados.append({
            "idx": idx,
            "Fecha Estado": gasto.get("Fecha", ""),
            "Descripción Estado": gasto.get("Descripción", ""),
            "Monto Estado": sug["monto_gasto"],
            "Categoria": categoria,
            "Material": material,
            "Facturas": [factura],
        })
        st.session_state.estados[idx] = "comprobado"
        st.session_state.facturas_por_gasto.pop(idx, None)
        st.session_state.clasificacion_por_gasto[idx] = {"categoria": categoria, "material": material}
        st.session_state.pool_facturas = [f for f in st.session_state.pool_facturas if f["_id"] != factura["_id"]]
        aplicados += 1
    return aplicados


def sums_por_estado(df):
    resumen = {}
    for estado in ["pendiente", "comprobado", "no_necesario"]:
        idxs = [i for i, e in st.session_state.estados.items() if e == estado]
        sub = df.loc[df.index.intersection(idxs)]
        resumen[estado] = {"count": len(idxs), "total": sub["Monto"].sum() if not sub.empty else 0.0}
    return resumen


# ============================================================
# CARGA DE ESTADO DE CUENTA
# ============================================================
def get_column_map(banco):
    """Regresa el diccionario de wildcards -> nombre estándar para cada banco."""
    if banco == "Santander 011-1":
        return {
            "fecha": "Fecha",
            "descripción": "Descripción",
            "concepto": "Concepto",
            "referencia": "Descripción",
            "cargo": "Cargo/Abono",
            "abono": "Cargo/Abono",
            "importe": "Importe",
            "monto": "Importe",
            "valor": "Importe",
            "saldo": "Saldo",
        }
    return None


def load_bank_statement(file, banco):
    if file.name.endswith(".csv"):
        df_raw = pd.read_csv(file)
    else:
        df_raw = pd.read_excel(file)

    columnas_deseadas = get_column_map(banco)
    if columnas_deseadas is None:
        st.warning(f"Las columnas para {banco} aún no están definidas. Por favor proporciónalas.")
        st.stop()

    rename_map = {}
    selected_original = []
    for col in df_raw.columns:
        col_lower = col.lower()
        for wildcard, nombre_estandar in columnas_deseadas.items():
            if wildcard in col_lower and nombre_estandar not in rename_map.values():
                rename_map[col] = nombre_estandar
                selected_original.append(col)
                break

    if not selected_original:
        st.error("No se encontraron columnas que coincidan con los nombres esperados.")
        st.stop()

    df = df_raw[selected_original].rename(columns=rename_map).copy()

    # columna de monto a usar para comparar contra las facturas
    if "Importe" in df.columns:
        df["Monto"] = pd.to_numeric(df["Importe"], errors="coerce").fillna(0.0)
    elif "Cargo/Abono" in df.columns:
        df["Monto"] = pd.to_numeric(df["Cargo/Abono"], errors="coerce").fillna(0.0)
    else:
        df["Monto"] = 0.0

    df.reset_index(drop=True, inplace=True)
    return df


# ============================================================
# LECTURA DE CFDI (soporta 3.3 y 4.0)
# ============================================================
CFDI_NS_POR_VERSION = {
    "3": "http://www.sat.gob.mx/cfd/3",
    "4": "http://www.sat.gob.mx/cfd/4",
}
TFD_NS = "http://www.sat.gob.mx/TimbreFiscalDigital"


def detectar_namespace_cfdi(root):
    """Detecta si el XML es CFDI 3.3 o 4.0 a partir del namespace del root."""
    tag = root.tag
    if tag.startswith("{"):
        uri = tag[1:tag.index("}")]
        if uri == CFDI_NS_POR_VERSION["3"]:
            return CFDI_NS_POR_VERSION["3"]
        if uri == CFDI_NS_POR_VERSION["4"]:
            return CFDI_NS_POR_VERSION["4"]
        return uri  # namespace no reconocido, se intenta igual
    return CFDI_NS_POR_VERSION["4"]  # fallback


def parse_cfdi(xml_bytes, filename):
    """Extrae los datos relevantes de un CFDI (3.3 o 4.0). Regresa dict o lanza excepción."""
    tree = ET.parse(BytesIO(xml_bytes))
    root = tree.getroot()

    cfdi_uri = detectar_namespace_cfdi(root)
    ns = {"cfdi": cfdi_uri, "tfd": TFD_NS}

    total = float(root.get("Total", 0) or 0)
    fecha_factura = root.get("Fecha", str(datetime.date.today()))[:10]
    timbre = root.find(".//tfd:TimbreFiscalDigital", ns)
    uuid = timbre.get("UUID") if timbre is not None else "SIN-UUID"
    emisor = root.find(".//cfdi:Emisor", ns)
    rfc_emisor = emisor.get("Rfc", "N/A") if emisor is not None else "N/A"
    razon_social = emisor.get("Nombre", "N/A") if emisor is not None else "N/A"

    valor_iva = 0.0
    impuestos_globales = root.find("./cfdi:Impuestos", ns)
    if impuestos_globales is not None:
        for traslado in impuestos_globales.findall(".//cfdi:Traslados/cfdi:Traslado", ns):
            if traslado.get("Impuesto") == "002":
                valor_iva += float(traslado.get("Importe", 0) or 0)

    conceptos = root.findall(".//cfdi:Concepto", ns)
    concepto = "; ".join(c.get("Descripcion", "") for c in conceptos) if conceptos else "N/A"

    return {
        "_id": None,  # se asigna al agregarlo
        "Fuente": f"XML: {filename}",
        "Fecha Factura": fecha_factura,
        "UUID": uuid,
        "Concepto": concepto,
        "RFC Emisor": rfc_emisor,
        "Razón Social": razon_social,
        "IVA": valor_iva,
        "Monto Total": total,
    }


# ============================================================
# GUARDADO / CARGA DE SESIÓN EN JSON
# ============================================================
def construir_sesion_json():
    """Empaqueta todo el estado de trabajo (incluyendo el propio estado de cuenta) en un dict serializable."""
    df = st.session_state.bank_df
    return {
        "version": 1,
        "guardado_en": datetime.datetime.now().isoformat(timespec="seconds"),
        "banco": st.session_state.banco,
        "bank_file_id": st.session_state.bank_file_id,
        "bank_df": df.to_dict(orient="split") if df is not None else None,
        "estados": {str(k): v for k, v in st.session_state.estados.items()},
        "facturas_por_gasto": {str(k): v for k, v in st.session_state.facturas_por_gasto.items()},
        "clasificacion_por_gasto": {str(k): v for k, v in st.session_state.clasificacion_por_gasto.items()},
        "concatenados": st.session_state.concatenados,
        "no_necesarios": st.session_state.no_necesarios,
        "factura_counter": st.session_state.factura_counter,
    }


def cargar_sesion_json(data):
    """Restaura el estado de la app a partir de un dict previamente generado por construir_sesion_json()."""
    bank_df_data = data.get("bank_df")
    if bank_df_data is not None:
        st.session_state.bank_df = pd.DataFrame(
            data=bank_df_data["data"],
            columns=bank_df_data["columns"],
            index=bank_df_data["index"],
        )
    else:
        st.session_state.bank_df = None

    st.session_state.banco = data.get("banco")
    st.session_state.bank_file_id = tuple(data["bank_file_id"]) if data.get("bank_file_id") else None
    st.session_state.estados = {int(k): v for k, v in data.get("estados", {}).items()}
    st.session_state.facturas_por_gasto = {int(k): v for k, v in data.get("facturas_por_gasto", {}).items()}
    st.session_state.clasificacion_por_gasto = {
        int(k): v for k, v in data.get("clasificacion_por_gasto", {}).items()
    }
    st.session_state.concatenados = data.get("concatenados", [])
    st.session_state.no_necesarios = data.get("no_necesarios", [])
    st.session_state.factura_counter = data.get("factura_counter", 0)
    st.session_state.selected_idx = None


with st.expander("💾 Guardar o continuar un avance guardado", expanded=False):
    colA, colB = st.columns(2)
    with colA:
        st.markdown("**Guardar avance actual**")
        if st.session_state.bank_df is not None:
            sesion_bytes = json.dumps(construir_sesion_json(), ensure_ascii=False, indent=2).encode("utf-8")
            st.download_button(
                "📥 Descargar avance (.json)",
                data=sesion_bytes,
                file_name=f"avance_caja_chica_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.json",
                mime="application/json",
            )
        else:
            st.caption("Sube un estado de cuenta primero para poder guardar avance.")
    with colB:
        st.markdown("**Continuar un avance guardado**")
        json_file = st.file_uploader("Sube tu archivo .json de avance", type=["json"], key="json_uploader")
        if json_file is not None:
            if st.button("Cargar este avance", key="btn_cargar_json"):
                try:
                    data = json.loads(json_file.getvalue().decode("utf-8"))
                    cargar_sesion_json(data)
                    st.success("Avance restaurado correctamente.")
                    st.rerun()
                except Exception as e:
                    st.error(f"No se pudo leer el archivo de avance: {e}")

# ============================================================
# 1. SELECCIÓN DE BANCO Y CARGA DE ESTADO DE CUENTA
# ============================================================
banco = st.selectbox(
    "Selecciona el banco del estado de cuenta",
    ["Santander 011-1", "BBVA 4546", "ICBC XXXX", "BBVA XXXX"],
)

file = st.file_uploader("Sube el estado de cuenta", type=["csv", "xlsx"])

if file:
    file_id = (file.name, file.size)
    if st.session_state.bank_file_id != file_id:
        # archivo nuevo o distinto: reconstruir todo el estado
        st.session_state.bank_df = load_bank_statement(file, banco)
        st.session_state.banco = banco
        st.session_state.bank_file_id = file_id
        st.session_state.estados = {i: "pendiente" for i in st.session_state.bank_df.index}
        st.session_state.facturas_por_gasto = {}
        st.session_state.clasificacion_por_gasto = {}
        st.session_state.selected_idx = None
        st.session_state.concatenados = []
        st.session_state.no_necesarios = []

df = st.session_state.bank_df

# ============================================================
# 2. PANEL RESUMEN SUPERIOR
# ============================================================
if df is not None:
    resumen = sums_por_estado(df)
    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown(f"""
        <div class="summary-box box-pendiente">
            <div class="summary-title">⏳ GASTOS SIN COMPROBAR</div>
            <div class="summary-count">{resumen['pendiente']['count']}</div>
            <div class="summary-amount">{money(resumen['pendiente']['total'])}</div>
        </div>""", unsafe_allow_html=True)
    with col2:
        st.markdown(f"""
        <div class="summary-box box-comprobado">
            <div class="summary-title">✅ GASTOS COMPROBADOS</div>
            <div class="summary-count">{resumen['comprobado']['count']}</div>
            <div class="summary-amount">{money(resumen['comprobado']['total'])}</div>
        </div>""", unsafe_allow_html=True)
    with col3:
        st.markdown(f"""
        <div class="summary-box box-no-necesario">
            <div class="summary-title">🚫 GASTOS NO NECESARIOS</div>
            <div class="summary-count">{resumen['no_necesario']['count']}</div>
            <div class="summary-amount">{money(resumen['no_necesario']['total'])}</div>
        </div>""", unsafe_allow_html=True)

    total_monto = sum(r["total"] for r in resumen.values())
    monto_resuelto = resumen["comprobado"]["total"] + resumen["no_necesario"]["total"]
    if total_monto > 0:
        st.progress(min(monto_resuelto / total_monto, 1.0),
                    text=f"Avance por monto: {money(monto_resuelto)} de {money(total_monto)}")

    st.write("")

    # ========================================================
    # 3. TABLA DE MOVIMIENTOS
    # ========================================================
    col_titulo, col_toggle = st.columns([10, 1])
    with col_titulo:
        st.subheader("Movimientos del estado de cuenta")
    with col_toggle:
        st.write("")
        if st.button("🔽" if st.session_state.tabla_expandida else "▶️", key="btn_toggle_tabla",
                     help="Colapsar/expandir la tabla"):
            st.session_state.tabla_expandida = not st.session_state.tabla_expandida
            st.rerun()

    if st.session_state.tabla_expandida:
        filtro_estado = st.radio(
            "Filtrar por estado",
            ["Todos", "Sin comprobar", "Comprobados", "No necesarios"],
            horizontal=True,
        )
        busqueda = st.text_input("🔎 Buscar por descripción")

        df_vista = df.copy()
        df_vista["Estado"] = df_vista.index.map(lambda i: status_label(st.session_state.estados.get(i, "pendiente")))

        if filtro_estado == "Sin comprobar":
            df_vista = df_vista[df_vista.index.map(lambda i: st.session_state.estados.get(i) == "pendiente")]
        elif filtro_estado == "Comprobados":
            df_vista = df_vista[df_vista.index.map(lambda i: st.session_state.estados.get(i) == "comprobado")]
        elif filtro_estado == "No necesarios":
            df_vista = df_vista[df_vista.index.map(lambda i: st.session_state.estados.get(i) == "no_necesario")]

        if busqueda and "Descripción" in df_vista.columns:
            df_vista = df_vista[df_vista["Descripción"].astype(str).str.contains(busqueda, case=False, na=False)]

        columnas_mostrar = [c for c in ["Estado", "Fecha", "Descripción", "Monto", "Saldo"] if c in df_vista.columns]
        st.dataframe(
            df_vista[columnas_mostrar].style.format({"Monto": money, "Saldo": money}),
            use_container_width=True,
            hide_index=False,
        )
    else:
        st.caption("Tabla colapsada. Da clic en ▶️ para volver a mostrarla.")

    # ========================================================
    # 4. SELECTOR DE MODO DE TRABAJO
    # ========================================================
    st.divider()
    st.subheader("¿Cómo quieres trabajar?")
    mb1, mb2 = st.columns(2)
    with mb1:
        if st.button("🔁 Emparejamiento automático de facturas", key="btn_modo_auto",
                      type="primary" if st.session_state.modo_trabajo == "auto" else "secondary",
                      use_container_width=True):
            st.session_state.modo_trabajo = "auto"
            st.rerun()
    with mb2:
        if st.button("✍️ Trabajar un gasto a la vez", key="btn_modo_manual",
                      type="primary" if st.session_state.modo_trabajo == "manual" else "secondary",
                      use_container_width=True):
            st.session_state.modo_trabajo = "manual"
            st.rerun()

    # ========================================================
    # 4a. EMPAREJAMIENTO AUTOMÁTICO DE FACTURAS (por monto)
    # ========================================================
    if st.session_state.modo_trabajo == "auto":
        st.subheader("🔁 Emparejamiento automático de facturas")
        st.caption(
            "Sube todos los XML del periodo de una vez. Buscamos, para cada gasto pendiente, "
            "la factura cuyo monto esté más cercano. Las diferencias de 1 centavo o menos se pueden "
            "aplicar directo; el resto necesita tu validación antes de agregarse."
        )

        xml_bulk = st.file_uploader(
            "Sube uno o más XML de CFDI para emparejar automáticamente",
            type=["xml"],
            accept_multiple_files=True,
            key="xml_bulk_uploader",
        )
        if xml_bulk:
            usados = uuids_consumidos() | {f["UUID"] for f in st.session_state.pool_facturas if f["UUID"] != "SIN-UUID"}
            agregadas, duplicadas, con_error = 0, 0, 0
            for xf in xml_bulk:
                try:
                    datos = parse_cfdi(xf.getvalue(), xf.name)
                except Exception as e:
                    st.error(f"No se pudo leer el XML '{xf.name}': {type(e).__name__}: {e}")
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
        sugerencias = calcular_matches_automaticos(df, pendientes_idx_match, st.session_state.pool_facturas)
        sug_exactas = [s for s in sugerencias if s["tipo"] == "exacto"]
        sug_revision = [s for s in sugerencias if s["tipo"] == "revision"]
        ids_sugeridos = {s["factura"]["_id"] for s in sugerencias}

        def _df_para_editor(lista_sugerencias, incluir_default):
            return pd.DataFrame([{
                "Incluir": incluir_default,
                "Fecha gasto": str(s["gasto"].get("Fecha", "")),
                "Descripción gasto": str(s["gasto"].get("Descripción", ""))[:40],
                "Monto gasto": s["monto_gasto"],
                "Factura (UUID)": s["factura"]["UUID"],
                "RFC Emisor": s["factura"]["RFC Emisor"],
                "Monto factura": s["factura"]["Monto Total"],
                "Diferencia": s["diferencia"],
                "Categoría": "",
                "Material": "",
            } for s in lista_sugerencias])

        if sug_exactas:
            st.markdown("##### ✅ Coincidencias exactas (diferencia ≤ $0.01)")
            df_exactas = _df_para_editor(sug_exactas, incluir_default=True)
            edited_exactas = st.data_editor(
                df_exactas,
                use_container_width=True,
                hide_index=True,
                disabled=["Fecha gasto", "Descripción gasto", "Monto gasto", "Factura (UUID)",
                          "RFC Emisor", "Monto factura", "Diferencia"],
                column_config={
                    "Monto gasto": st.column_config.NumberColumn(format="$%.2f"),
                    "Monto factura": st.column_config.NumberColumn(format="$%.2f"),
                    "Diferencia": st.column_config.NumberColumn(format="$%.2f"),
                },
                key="editor_exactas",
            )
            if st.button("➕ Aplicar coincidencias exactas seleccionadas", type="primary", key="btn_aplicar_exactas"):
                n = aplicar_sugerencias(edited_exactas, sug_exactas)
                if n:
                    st.success(f"{n} gasto(s) comprobado(s) automáticamente.")
                    st.rerun()
                else:
                    st.warning("No marcaste ninguna fila con 'Incluir'.")

        if sug_revision:
            st.markdown("##### 🔍 Requieren tu validación (diferencia mayor a $0.01)")
            df_revision = _df_para_editor(sug_revision, incluir_default=False)
            edited_revision = st.data_editor(
                df_revision,
                use_container_width=True,
                hide_index=True,
                disabled=["Fecha gasto", "Descripción gasto", "Monto gasto", "Factura (UUID)",
                          "RFC Emisor", "Monto factura", "Diferencia"],
                column_config={
                    "Monto gasto": st.column_config.NumberColumn(format="$%.2f"),
                    "Monto factura": st.column_config.NumberColumn(format="$%.2f"),
                    "Diferencia": st.column_config.NumberColumn(format="$%.2f"),
                },
                key="editor_revision",
            )
            if st.button("➕ Aplicar coincidencias validadas", key="btn_aplicar_revision"):
                n = aplicar_sugerencias(edited_revision, sug_revision)
                if n:
                    st.success(f"{n} gasto(s) comprobado(s) tras validación.")
                    st.rerun()
                else:
                    st.warning("Marca 'Incluir' en las filas que quieras confirmar antes de aplicar.")

        pool_sin_match = [f for f in st.session_state.pool_facturas if f["_id"] not in ids_sugeridos]
        if pool_sin_match:
            with st.expander(f"📥 Facturas sin coincidencia sugerida ({len(pool_sin_match)}) — quedan disponibles para asignar manualmente"):
                df_pool = pd.DataFrame(pool_sin_match).drop(columns=["_id"])
                st.dataframe(
                    df_pool.style.format({"IVA": money, "Monto Total": money}),
                    use_container_width=True, hide_index=True,
                )
                st.caption(
                    "Estas facturas quedan en espera. Puedes asignarlas manualmente subiéndolas de nuevo "
                    "en el panel de 'Trabajar un gasto' de abajo (se evita el duplicado automáticamente)."
                )

    if st.session_state.modo_trabajo == "manual":
        # ========================================================
        # 5. SELECCIÓN DE GASTO A TRABAJAR
        # ========================================================
        st.divider()
        st.subheader("Trabajar un gasto")

        pendientes_idx = [i for i, e in st.session_state.estados.items() if e == "pendiente"]

        if not pendientes_idx:
            st.info("No hay gastos pendientes por comprobar. 🎉")
        else:
            opciones = {
                i: f"#{i} · {df.loc[i, 'Fecha'] if 'Fecha' in df.columns else ''} · "
                   f"{df.loc[i, 'Descripción'] if 'Descripción' in df.columns else ''} · {money(df.loc[i, 'Monto'])}"
                for i in pendientes_idx
            }
            idx_sel = st.selectbox(
                "Selecciona el gasto que quieres comprobar",
                options=list(opciones.keys()),
                format_func=lambda i: opciones[i],
                key="selector_gasto",
            )
            st.session_state.selected_idx = idx_sel

        # ========================================================
        # 5. PANEL DE TRABAJO DEL GASTO SELECCIONADO
        # ========================================================
        idx = st.session_state.selected_idx
        if idx is not None and st.session_state.estados.get(idx) == "pendiente":
            gasto = df.loc[idx]
            monto_gasto = float(gasto["Monto"])

            st.markdown('<div class="card">', unsafe_allow_html=True)
            col_titulo_gasto, col_no_necesario = st.columns([4, 1])
            with col_titulo_gasto:
                st.markdown("#### 📌 Gasto seleccionado")
            with col_no_necesario:
                if st.button("🚫 No necesario", key=f"btn_no_necesario_{idx}", use_container_width=True):
                    clasif_actual = st.session_state.clasificacion_por_gasto.get(idx, {"categoria": "", "material": ""})
                    st.session_state.no_necesarios.append({
                        "Fecha Estado": gasto.get("Fecha", ""),
                        "Descripción Estado": gasto.get("Descripción", ""),
                        "Monto Estado": monto_gasto,
                        "Categoria": clasif_actual.get("categoria", ""),
                        "Material": clasif_actual.get("material", ""),
                    })
                    st.session_state.estados[idx] = "no_necesario"
                    st.session_state.facturas_por_gasto.pop(idx, None)
                    st.session_state.selected_idx = None
                    st.info("Gasto marcado como no necesario.")
                    st.rerun()
            c1, c2, c3 = st.columns(3)
            c1.metric("Fecha", str(gasto.get("Fecha", "")))
            c2.metric("Descripción", str(gasto.get("Descripción", ""))[:30])
            c3.metric("Monto", money(monto_gasto))
            st.markdown("</div>", unsafe_allow_html=True)

            # --- Clasificación (Categoría / Material) ---
            st.markdown("##### 🏷️ Clasificación del gasto")
            clasif = st.session_state.clasificacion_por_gasto.setdefault(idx, {"categoria": "", "material": ""})
            cl1, cl2 = st.columns(2)
            clasif["categoria"] = cl1.text_input(
                "Categoría", value=clasif.get("categoria", ""), key=f"categoria_{idx}",
                help="Por ahora es texto libre. Cuando subas tu catálogo de categorías/material, "
                     "este campo se volverá una lista desplegable.",
            )
            clasif["material"] = cl2.text_input(
                "Material", value=clasif.get("material", ""), key=f"material_{idx}",
            )

            st.session_state.facturas_por_gasto.setdefault(idx, [])
            facturas = st.session_state.facturas_por_gasto[idx]

            # --- Agregar XML(s) ---
            st.markdown("##### 📎 Agregar facturas (puedes subir varios XML a la vez)")
            xml_files = st.file_uploader(
                "Sube uno o más XML de CFDI (soporta versión 3.3 y 4.0)",
                type=["xml"],
                accept_multiple_files=True,
                key=f"xml_uploader_{idx}",
            )
            if xml_files:
                uuids_existentes = {f["UUID"] for f in facturas} | uuids_consumidos()
                for xf in xml_files:
                    try:
                        datos = parse_cfdi(xf.getvalue(), xf.name)
                    except Exception as e:
                        st.error(f"No se pudo leer el XML '{xf.name}': {type(e).__name__}: {e}")
                        continue
                    if datos["UUID"] in uuids_existentes and datos["UUID"] != "SIN-UUID":
                        continue  # ya agregada a este u otro gasto, evitar duplicado
                    datos["_id"] = next_factura_id()
                    facturas.append(datos)
                    uuids_existentes.add(datos["UUID"])
                    # si esta factura ya estaba libre en el pool automático, se retira de ahí
                    # para que no se sugiera de nuevo en el emparejamiento automático
                    if datos["UUID"] != "SIN-UUID":
                        st.session_state.pool_facturas = [
                            f for f in st.session_state.pool_facturas if f["UUID"] != datos["UUID"]
                        ]

            # --- Agregar factura manual ---
            with st.expander("✏️ Agregar comprobación manual (sin XML)"):
                with st.form(f"manual_form_{idx}", clear_on_submit=True):
                    mf_fecha = st.date_input("Fecha de factura", value=datetime.date.today())
                    mf_uuid = st.text_input("UUID")
                    mf_concepto = st.text_input("Concepto/Descripción")
                    mf_rfc = st.text_input("RFC Emisor")
                    mf_razon = st.text_input("Razón Social Emisor")
                    mf_iva = st.number_input("IVA", min_value=0.0, format="%.2f")
                    mf_monto = st.number_input("Monto Total", min_value=0.0, format="%.2f")
                    guardar_manual = st.form_submit_button("Agregar factura manual")
                    if guardar_manual:
                        facturas.append({
                            "_id": next_factura_id(),
                            "Fuente": "Manual",
                            "Fecha Factura": str(mf_fecha),
                            "UUID": mf_uuid or "SIN-UUID",
                            "Concepto": mf_concepto,
                            "RFC Emisor": mf_rfc,
                            "Razón Social": mf_razon,
                            "IVA": mf_iva,
                            "Monto Total": mf_monto,
                        })

            # --- Tabla de facturas agregadas a este gasto ---
            st.markdown("##### 🧾 Facturas agregadas a este gasto")
            if facturas:
                df_facturas = pd.DataFrame(facturas)
                df_mostrar = df_facturas.drop(columns=["_id"])
                st.dataframe(
                    df_mostrar.style.format({"IVA": money, "Monto Total": money}),
                    use_container_width=True,
                    hide_index=True,
                )

                quitar = st.multiselect(
                    "Quitar factura(s) de la lista",
                    options=[f["_id"] for f in facturas],
                    format_func=lambda fid: next(
                        f"{f['Fuente']} · {f['UUID']} · {money(f['Monto Total'])}"
                        for f in facturas if f["_id"] == fid
                    ),
                    key=f"quitar_{idx}",
                )
                if quitar and st.button("Quitar seleccionadas", key=f"btn_quitar_{idx}"):
                    st.session_state.facturas_por_gasto[idx] = [f for f in facturas if f["_id"] not in quitar]
                    st.rerun()

                suma_facturas = sum(f["Monto Total"] for f in facturas)
                diferencia = round(monto_gasto - suma_facturas, 2)

                st.write("")
                cc1, cc2, cc3 = st.columns(3)
                cc1.metric("Monto del gasto", money(monto_gasto))
                cc2.metric("Suma de facturas", money(suma_facturas))
                cc3.metric("Diferencia", money(diferencia))

                if abs(diferencia) <= 0.01:
                    st.markdown(
                        f"<div class='success-box'>✅ Gasto comprobado correctamente. "
                        f"Diferencia: {money(diferencia)}</div>",
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
                            "Facturas": facturas,  # se guarda la lista completa (anidada)
                        })
                        st.session_state.estados[idx] = "comprobado"
                        st.session_state.facturas_por_gasto.pop(idx, None)
                        st.session_state.selected_idx = None
                        st.success("Gasto añadido a los registros.")
                        st.rerun()
                else:
                    st.markdown(
                        f"<div class='error-box'>❌ La suma de facturas no coincide con el gasto. "
                        f"Diferencia: {money(diferencia)}</div>",
                        unsafe_allow_html=True,
                    )
            else:
                st.caption("Aún no has agregado ninguna factura para este gasto.")

        # ========================================================
    # 6. HISTORIAL Y DESCARGA
    # ========================================================
    st.divider()
    st.subheader("📊 Historial")

    tab_comprobados, tab_no_necesarios = st.tabs(["✅ Comprobados", "🚫 No necesarios"])

    def fila_excel_comprobado(no, registro):
        facturas = registro["Facturas"]
        suma_facturas = sum(f["Monto Total"] for f in facturas)
        suma_iva = sum(f["IVA"] for f in facturas)
        return {
            "No": no,
            "Bank date": registro["Fecha Estado"],
            "Bank amt": registro["Monto Estado"],
            "Category": registro.get("Categoria", ""),
            "Material": registro.get("Material", ""),
            "Concepto": "; ".join(f["Concepto"] for f in facturas if f.get("Concepto")),
            "Month": mes_es(registro["Fecha Estado"]),
            "UUID date": "; ".join(f["Fecha Factura"] for f in facturas),
            "UUID": "; ".join(f["UUID"] for f in facturas),
            "UUID amt": suma_facturas,
            "IVA": suma_iva,
            "Diff": round(float(registro["Monto Estado"]) - suma_facturas, 2),
        }

    df_comprobados_vista = None
    if st.session_state.concatenados:
        filas = [fila_excel_comprobado(n + 1, r) for n, r in enumerate(st.session_state.concatenados)]
        df_comprobados_vista = pd.DataFrame(filas)

    with tab_comprobados:
        if df_comprobados_vista is not None:
            st.dataframe(
                df_comprobados_vista.style.format(
                    {"Bank amt": money, "UUID amt": money, "IVA": money, "Diff": money}
                ),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.caption("Todavía no hay gastos comprobados.")

    with tab_no_necesarios:
        if st.session_state.no_necesarios:
            df_nn = pd.DataFrame(st.session_state.no_necesarios)
            st.dataframe(df_nn.style.format({"Monto Estado": money}), use_container_width=True, hide_index=True)
        else:
            st.caption("Todavía no hay gastos marcados como no necesarios.")

    if st.session_state.concatenados or st.session_state.no_necesarios:
        output = BytesIO()
        with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
            workbook = writer.book

            if df_comprobados_vista is not None:
                columnas_finales = [
                    "No", "Bank date", "Bank amt", "Category", "Material", "Concepto",
                    "Month", "UUID date", "UUID", "UUID amt", "IVA", "Diff",
                ]
                df_export = df_comprobados_vista[columnas_finales].copy()
                df_export.to_excel(writer, index=False, sheet_name="Comprobados")

                ws = writer.sheets["Comprobados"]
                money_fmt = workbook.add_format({"num_format": "$#,##0.00"})
                bold_fmt = workbook.add_format({"bold": True})
                for col_name in ["Bank amt", "UUID amt", "IVA", "Diff"]:
                    col_idx = columnas_finales.index(col_name)
                    ws.set_column(col_idx, col_idx, 14, money_fmt)

                # Fila de totales
                fila_total = len(df_export) + 1  # +1 por encabezado (0-index -> siguiente fila)
                ws.write(fila_total, 0, "Total", bold_fmt)
                for col_name in ["Bank amt", "UUID amt", "IVA", "Diff"]:
                    col_idx = columnas_finales.index(col_name)
                    ws.write(fila_total, col_idx, df_export[col_name].sum(), money_fmt)

                ws.set_column(1, 1, 12)   # Bank date
                ws.set_column(3, 4, 16)   # Category, Material
                ws.set_column(5, 5, 30)   # Concepto
                ws.set_column(7, 8, 22)   # UUID date, UUID

            if st.session_state.no_necesarios:
                pd.DataFrame(st.session_state.no_necesarios).to_excel(
                    writer, index=False, sheet_name="No necesarios"
                )

            df_pend = df[df.index.map(lambda i: st.session_state.estados.get(i) == "pendiente")]
            if not df_pend.empty:
                df_pend.drop(columns=["Monto"], errors="ignore").to_excel(writer, index=False, sheet_name="Sin comprobar")

        st.download_button(
            label="📥 Descargar Excel (todo el historial)",
            data=output.getvalue(),
            file_name=f"Comprobaciones_{datetime.datetime.now().strftime('%Y%m%d')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
else:
    st.info("Sube un estado de cuenta para comenzar.")
