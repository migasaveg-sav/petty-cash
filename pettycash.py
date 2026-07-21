import streamlit as st
import pandas as pd
import xml.etree.ElementTree as ET
import datetime
from io import BytesIO

# Paleta de colores estilo Quickel
C_AZUL_MUY_OSCURO = "#1A4756"
C_TEAL_VIVO = "#038191"
C_CORAL_ALERTA = "#F84E65"
C_CREMA_FONDO = "#ff9f1c"

st.set_page_config(page_title="Comprobación Caja Chica", layout="wide")

st.markdown(f"""
<style>
.stApp {{ background-color: {C_AZUL_MUY_OSCURO}; color: {C_CREMA_FONDO}; }}
table, th, td {{ border: 1px solid #ccc; border-collapse: collapse; padding: 6px; }}
th {{ background-color: {C_TEAL_VIVO}; color: white; }}
</style>
""", unsafe_allow_html=True)

st.title("Comprobación de Caja Chica")

# --- 1. Estado de cuenta ---
file = st.file_uploader("Sube el estado de cuenta", type=["csv","xlsx"])
if file:
    if file.name.endswith(".csv"):
        df = pd.read_csv(file)
    else:
        df = pd.read_excel(file)

    st.subheader("Movimientos")
    st.write("Selecciona el gasto a comprobar:")

    # Añadir columna de selección
    if "Seleccionar" not in df.columns:
        df.insert(0, "Seleccionar", False)

    edited_df = st.data_editor(
        df,
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
        column_config={
            "Seleccionar": st.column_config.CheckboxColumn("Selección", default=False),
            "Fecha": st.column_config.TextColumn("Fecha", disabled=True),
            "Descripción": st.column_config.TextColumn("Descripción", disabled=True),
            "Cargo": st.column_config.NumberColumn("Cargo", format="$%.2f", disabled=True)
        },
        key="data_editor_gastos"
    )

    seleccionados = edited_df[edited_df["Seleccionar"] == True].index.tolist()

    if seleccionados:
        st.subheader("Comprobación de gasto")
        gasto_sel = df.loc[seleccionados[0]]  # tomar el primero marcado

        xml_file = st.file_uploader("Sube el XML del CFDI", type=["xml"])
        if xml_file:
            # --- Extracción estilo Quickel ---
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

            # IVA
            valor_iva = 0.0
            impuestos_globales = root.find('./cfdi:Impuestos', ns)
            if impuestos_globales is not None:
                for traslado in impuestos_globales.findall('.//cfdi:Traslados/cfdi:Traslado', ns):
                    if traslado.get('Impuesto') == '002':
                        valor_iva = float(traslado.get('Importe', 0))

            # Concepto
            concepto_nodo = root.find('.//cfdi:Concepto', ns)
            concepto = concepto_nodo.get('Descripcion', 'N/A') if concepto_nodo is not None else "N/A"

            comprobacion = pd.DataFrame([{
                "Fecha Factura": fecha_factura,
                "UUID": uuid,
                "Concepto": concepto,
                "RFC Emisor": rfc_emisor,
                "Razón Social": razon_social,
                "IVA": valor_iva,
                "Monto Total": total
            }])

            st.dataframe(comprobacion)

            # Validación de diferencia
            monto_gasto = float(gasto_sel["Cargo"])
            diferencia = round(monto_gasto - total, 2)

            if abs(diferencia) <= 0.01:
                st.success(f"Gasto comprobado correctamente. Diferencia: {diferencia}")
            else:
                st.error(f"Diferencia mayor a 1 centavo: {diferencia} pesos")
