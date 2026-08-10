import streamlit as st
import pandas as pd
import xml.etree.ElementTree as ET
import datetime
from io import BytesIO

# ============================================================
# PALETA DE COLORES
# ============================================================
C_AZUL_MUY_OSCURO = "#1A4756"
C_TEAL_VIVO = "#038191"
C_CORAL_ALERTA = "#F84E65"
C_AMARILLO_ACENTO = "#ff9f1c"
C_GRIS_NEUTRO = "#5C7A89"
C_VERDE_OK = "#2ECC71"

st.set_page_config(page_title="Comprobación Caja Chica", layout="wide")

# ============================================================
# ESTADO DE SESIÓN
# ============================================================
def init_state():
    defaults = {
        "bank_df": None,
        "bank_file_id": None,          # (nombre, tamaño) del archivo cargado, para detectar cambios
        "estados": {},                 # idx -> "pendiente" | "comprobado" | "no_necesario"
        "facturas_por_gasto": {},      # idx -> lista de dicts (facturas agregadas a ese gasto)
        "selected_idx": None,
        "concatenados": [],            # registros finales de gastos comprobados
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
.stApp {{ background-color: {C_AZUL_MUY_OSCURO}; color: white; }}
table, th, td {{ border: 1px solid #ccc; border-collapse: collapse; padding: 6px; }}
th {{ background-color: {C_TEAL_VIVO}; color: white; }}

.card {{
    background-color: #234756; border-radius: 10px; padding: 16px 20px;
    margin-bottom: 14px; border: 1px solid #2f5b6c;
}}
.summary-box {{
    border-radius: 10px; padding: 14px 18px; text-align: center; color: white;
}}
.summary-title {{ font-size: 0.85rem; opacity: 0.9; margin-bottom: 4px; }}
.summary-count {{ font-size: 1.6rem; font-weight: 700; }}
.summary-amount {{ font-size: 1.0rem; opacity: 0.95; }}

.box-pendiente {{ background-color: {C_GRIS_NEUTRO}; }}
.box-comprobado {{ background-color: {C_TEAL_VIVO}; }}
.box-no-necesario {{ background-color: {C_AMARILLO_ACENTO}; color: #1A4756; }}

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
.badge-no-necesario {{ background-color: {C_AMARILLO_ACENTO}; color: #1A4756; }}
</style>
""", unsafe_allow_html=True)

st.title("💰 Comprobación de Caja Chica")

# ============================================================
# HELPERS
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


def parse_cfdi(xml_bytes, filename):
    """Extrae los datos relevantes de un CFDI. Regresa dict o lanza excepción."""
    tree = ET.parse(BytesIO(xml_bytes))
    root = tree.getroot()
    ns = {
        "cfdi": "http://www.sat.gob.mx/cfd/4",
        "tfd": "http://www.sat.gob.mx/TimbreFiscalDigital",
    }

    total = float(root.get("Total", 0))
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
                valor_iva += float(traslado.get("Importe", 0))

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


def next_factura_id():
    st.session_state.factura_counter += 1
    return st.session_state.factura_counter


def sums_por_estado(df):
    resumen = {}
    for estado in ["pendiente", "comprobado", "no_necesario"]:
        idxs = [i for i, e in st.session_state.estados.items() if e == estado]
        sub = df.loc[df.index.intersection(idxs)]
        resumen[estado] = {"count": len(idxs), "total": sub["Monto"].sum() if not sub.empty else 0.0}
    return resumen


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
        st.session_state.bank_file_id = file_id
        st.session_state.estados = {i: "pendiente" for i in st.session_state.bank_df.index}
        st.session_state.facturas_por_gasto = {}
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

    st.write("")

    # ========================================================
    # 3. TABLA DE MOVIMIENTOS
    # ========================================================
    st.subheader("Movimientos del estado de cuenta")

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

    # ========================================================
    # 4. SELECCIÓN DE GASTO A TRABAJAR
    # ========================================================
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
        st.markdown("#### 📌 Gasto seleccionado")
        c1, c2, c3 = st.columns(3)
        c1.metric("Fecha", str(gasto.get("Fecha", "")))
        c2.metric("Descripción", str(gasto.get("Descripción", ""))[:30])
        c3.metric("Monto", money(monto_gasto))
        st.markdown("</div>", unsafe_allow_html=True)

        st.session_state.facturas_por_gasto.setdefault(idx, [])
        facturas = st.session_state.facturas_por_gasto[idx]

        # --- Agregar XML(s) ---
        st.markdown("##### 📎 Agregar facturas (puedes subir varios XML a la vez)")
        xml_files = st.file_uploader(
            "Sube uno o más XML de CFDI",
            type=["xml"],
            accept_multiple_files=True,
            key=f"xml_uploader_{idx}",
        )
        if xml_files:
            uuids_existentes = {f["UUID"] for f in facturas}
            for xf in xml_files:
                try:
                    datos = parse_cfdi(xf.getvalue(), xf.name)
                except Exception as e:
                    st.error(f"No se pudo leer el XML '{xf.name}': {e}")
                    continue
                if datos["UUID"] in uuids_existentes and datos["UUID"] != "SIN-UUID":
                    continue  # ya agregada, evitar duplicado
                datos["_id"] = next_factura_id()
                facturas.append(datos)
                uuids_existentes.add(datos["UUID"])

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
                    for f in facturas:
                        st.session_state.concatenados.append({
                            "Fecha Estado": gasto.get("Fecha", ""),
                            "Descripción Estado": gasto.get("Descripción", ""),
                            "Monto Estado": monto_gasto,
                            "Fuente": f["Fuente"],
                            "Fecha Factura": f["Fecha Factura"],
                            "UUID": f["UUID"],
                            "Concepto Factura": f["Concepto"],
                            "RFC Emisor": f["RFC Emisor"],
                            "Razón Social": f["Razón Social"],
                            "IVA": f["IVA"],
                            "Monto Factura": f["Monto Total"],
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

        st.write("")
        if st.button("🚫 Marcar como no necesario", key=f"btn_no_necesario_{idx}"):
            st.session_state.no_necesarios.append({
                "Fecha Estado": gasto.get("Fecha", ""),
                "Descripción Estado": gasto.get("Descripción", ""),
                "Monto Estado": monto_gasto,
            })
            st.session_state.estados[idx] = "no_necesario"
            st.session_state.facturas_por_gasto.pop(idx, None)
            st.session_state.selected_idx = None
            st.info("Gasto marcado como no necesario.")
            st.rerun()

    # ========================================================
    # 6. HISTORIAL Y DESCARGA
    # ========================================================
    st.divider()
    st.subheader("📊 Historial")

    tab_comprobados, tab_no_necesarios = st.tabs(["✅ Comprobados", "🚫 No necesarios"])

    with tab_comprobados:
        if st.session_state.concatenados:
            df_final = pd.DataFrame(st.session_state.concatenados)
            st.dataframe(
                df_final.style.format({"Monto Estado": money, "IVA": money, "Monto Factura": money}),
                use_container_width=True,
            )
        else:
            st.caption("Todavía no hay gastos comprobados.")

    with tab_no_necesarios:
        if st.session_state.no_necesarios:
            df_nn = pd.DataFrame(st.session_state.no_necesarios)
            st.dataframe(df_nn.style.format({"Monto Estado": money}), use_container_width=True)
        else:
            st.caption("Todavía no hay gastos marcados como no necesarios.")

    if st.session_state.concatenados or st.session_state.no_necesarios:
        output = BytesIO()
        with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
            if st.session_state.concatenados:
                pd.DataFrame(st.session_state.concatenados).to_excel(writer, index=False, sheet_name="Comprobados")
            if st.session_state.no_necesarios:
                pd.DataFrame(st.session_state.no_necesarios).to_excel(writer, index=False, sheet_name="No necesarios")
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
