import streamlit as st
import pandas as pd
import xml.etree.ElementTree as ET
import datetime
from io import BytesIO

# Paleta estilo Quickel
C_AZUL_MUY_OSCURO = "#1A4756"
C_TEAL_VIVO = "#038191"
C_CORAL_ALERTA = "#F84E65"
C_CREMA_FONDO = "#ff9f1c"

st.set_page_config(page_title="Comprobación Caja Chica", layout="wide")

# Estado inicial
if "concatenados" not in st.session_state:
    st.session_state.concatenados = []

# Estilos CSS
st.markdown(f"""
<style>
.stApp {{ background-color: {C_AZUL_MUY_OSCURO}; color: {C_CREMA_FONDO}; }}
table, th, td {{ border: 1px solid #ccc; border-collapse: collapse; padding: 6px; }}
th {{ background-color: {C_TEAL_VIVO}; color: white; }}
.success-box {{
    background-color: {C_TEAL_VIVO}; color: white; padding: 10px; border-radius: 5px;
}}
.error-box {{
    background-color: {C_CORAL_ALERTA}; color: white; padding: 10px; border-radius: 5px;
}}
</style>
""", unsafe_allow_html=True)

st.title("Comprobación de Caja Chica")

# --- Selector de banco ---
banco = st.selectbox(
    "Selecciona el banco del estado de cuenta",
    ["Santander 011-1", "BBVA 4546", "ICBC XXXX", "BBVA XXXX"]
)

# --- 1. Estado de cuenta ---
file = st.file_uploader("Sube el estado de cuenta", type=["csv","xlsx"])
if file:
    if file.name.endswith(".csv"):
        df_raw = pd.read_csv(file)
    else:
        df_raw = pd.read_excel(file)

    # --- DEFINICIÓN DE COLUMNAS POR BANCO ---
    if banco == "Santander 011-1":
        columnas_deseadas = {
            "fecha": "Fecha",
            "descripción": "Descripción",
            "concepto": "Concepto",
            "referencia": "Descripción",
            "cargo": "Cargo/Abono",
            "abono": "Cargo/Abono",
            "importe": "Importe",
            "monto": "Importe",
            "valor": "Importe",
            "saldo": "Saldo"
        }
    else:
        st.warning(f"Las columnas para {banco} aún no están definidas. Por favor proporciónalas.")
        st.stop()

    # --- FILTRAR Y RENOMBRAR ---
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

    # Agregar columna de selección
    if "Seleccionar" not in df.columns:
        df.insert(0, "Seleccionar", False)

    st.subheader("Movimientos")

    edited_df = st.data_editor(
        df,
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
        column_config={
            "Seleccionar": st.column_config.CheckboxColumn("Selección", default=False),
            "Fecha": st.column_config.TextColumn("Fecha", disabled=True),
            "Descripción": st.column_config.TextColumn("Descripción", disabled=True),
            "Cargo/Abono": st.column_config.NumberColumn("Cargo/Abono", format="$%.2f", disabled=True),
            "Importe": st.column_config.NumberColumn("Importe", format="$%.2f", disabled=True),
            "Saldo": st.column_config.NumberColumn("Saldo", format="$%.2f", disabled=True),
        },
        key="data_editor_gastos"
    )

    seleccionados = edited_df[edited_df["Seleccionar"] == True].index.tolist()

    if seleccionados:
        gasto_sel = df.loc[seleccionados[0]]
        st.subheader("Comprobación de gasto")

        xml_file = st.file_uploader("Sube el XML del CFDI", type=["xml"])
        if xml_file:
            tree = ET.parse(xml_file)
            root = tree.getroot()
            ns = {
                'cfdi': 'http://www.sat.gob.mx/cfd/4',
                'tfd': 'http://www.sat.gob.mx/TimbreFiscalDigital'
            }

            total = float(root.get('Total', 0))
            fecha_factura = root.get('Fecha', str(datetime.date.today()))[:10]
            timbre = root.find('.//tfd:TimbreFiscalDigital', ns)
            uuid = timbre.get('UUID') if timbre is not None else "SIN-UUID"
            emisor = root.find('.//cfdi:Emisor', ns)
            rfc_emisor = emisor.get('Rfc', 'N/A') if emisor is not None else "N/A"
            razon_social = emisor.get('Nombre', 'N/A') if emisor is not None else "N/A"

            valor_iva = 0.0
            impuestos_globales = root.find('./cfdi:Impuestos', ns)
            if impuestos_globales is not None:
                for traslado in impuestos_globales.findall('.//cfdi:Traslados/cfdi:Traslado', ns):
                    if traslado.get('Impuesto') == '002':
                        valor_iva = float(traslado.get('Importe', 0))

            concepto_nodo = root.find('.//cfdi:Concepto', ns)
            concepto = concepto_nodo.get('Descripcion', 'N/A') if concepto_nodo is not None else "N/A"

            comprobacion = {
                "Fecha Factura": fecha_factura,
                "UUID": uuid,
                "Concepto": concepto,
                "RFC Emisor": rfc_emisor,
                "Razón Social": razon_social,
                "IVA": valor_iva,
                "Monto Total": total
            }

            st.write(pd.DataFrame([comprobacion]))

            diferencia = round(float(gasto_sel["Importe"]) - total, 2)
            if abs(diferencia) <= 0.01:
                st.markdown(
                    f"<div class='success-box'>✅ Gasto comprobado correctamente. Diferencia: {diferencia}</div>",
                    unsafe_allow_html=True
                )

                if st.button("Guardar y concatenar"):
                    combinado = {
                        "Fecha Estado": gasto_sel["Fecha"],
                        "Descripción Estado": gasto_sel["Descripción"],
                        "Cargo Estado": gasto_sel["Cargo/Abono"],
                        "Fecha Factura": comprobacion["Fecha Factura"],
                        "UUID": comprobacion["UUID"],
                        "Concepto Factura": comprobacion["Concepto"],
                        "RFC Emisor": comprobacion["RFC Emisor"],
                        "Razón Social": comprobacion["Razón Social"],
                        "IVA": comprobacion["IVA"],
                        "Monto Factura": comprobacion["Monto Total"]
                    }
                    st.session_state.concatenados.append(combinado)
                    st.success("Concatenado correctamente.")
            else:
                st.markdown(
                    f"<div class='error-box'>❌ Diferencia mayor a 1 centavo: {diferencia} pesos</div>",
                    unsafe_allow_html=True
                )

        # --- Comprobación manual ---
        st.markdown("### Comprobación manual (sin XML)")
        with st.form("manual_form"):
            fecha_factura = st.date_input("Fecha de factura", value=datetime.date.today())
            uuid = st.text_input("UUID")
            concepto = st.text_input("Concepto/Descripción")
            rfc_emisor = st.text_input("RFC Emisor")
            razon_social = st.text_input("Razón Social Emisor")
            iva = st.number_input("IVA", min_value=0.0, format="%.2f")
            monto_total = st.number_input("Monto Total", min_value=0.0, format="%.2f")
            guardar_manual = st.form_submit_button("Guardar comprobación manual")

            if guardar_manual:
                combinado = {
                    "Fecha Estado": gasto_sel["Fecha"],
                    "Descripción Estado": gasto_sel["Descripción"],
                    "Cargo Estado": gasto_sel["Cargo/Abono"],
                    "Fecha Factura": fecha_factura,
                    "UUID": uuid,
                    "Concepto Factura": concepto,
                    "RFC Emisor": rfc_emisor,
                    "Razón Social": razon_social,
                    "IVA": iva,
                    "Monto Factura": monto_total
                }
                st.session_state.concatenados.append(combinado)
                st.success("Comprobación manual guardada.")

# --- Tabla final concatenada ---
if st.session_state.concatenados:
    st.subheader("Tabla de comprobaciones acumuladas")
    df_final = pd.DataFrame(st.session_state.concatenados)
    st.dataframe(df_final)

    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df_final.to_excel(writer, index=False, sheet_name="Comprobaciones")

    st.download_button(
        label="📥 Descargar Excel",
        data=output.getvalue(),
        file_name=f"Comprobaciones_{datetime.datetime.now().strftime('%Y%m%d')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
