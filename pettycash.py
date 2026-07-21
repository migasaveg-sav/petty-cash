import streamlit as st
import pandas as pd
import xml.etree.ElementTree as ET
from io import BytesIO

st.title("Comprobación de Caja Chica")

# 1. Cargar archivo bancario
file = st.file_uploader("Sube el estado de cuenta", type=["csv","xlsx"])
if file:
    if file.name.endswith(".csv"):
        df = pd.read_csv(file)
    else:
        df = pd.read_excel(file)
    st.dataframe(df)

    # 2. Panel lateral
    gasto = st.sidebar.selectbox("Selecciona el gasto", df["Descripción"])
    btn_comprobar = st.sidebar.button("Comprobar gasto")

    if btn_comprobar:
        xml_file = st.sidebar.file_uploader("Sube el XML del CFDI", type=["xml"])
        if xml_file:
            tree = ET.parse(xml_file)
            root = tree.getroot()

            # Ejemplo de extracción (ajusta según tu código Quickel)
            uuid = root.attrib.get("UUID","")
            fecha_factura = root.attrib.get("Fecha","")
            monto_xml = float(root.attrib.get("Total","0"))
            concepto = root.attrib.get("Descripcion","")

            # Datos del gasto seleccionado
            gasto_sel = df[df["Descripción"] == gasto].iloc[0]
            monto_gasto = float(gasto_sel["Cargo"])
            diferencia = round(monto_gasto - monto_xml,2)

            # Validación
            if 0.01 <= abs(diferencia) <= 0.05:
                st.warning(f"Diferencias encontradas: {diferencia} pesos")
            else:
                st.success("Gasto comprobado correctamente")

            # 3. Comentarios
            comentarios = st.sidebar.text_area("Comentarios")

            # 4. Guardar cambios y descargar Excel
            if st.sidebar.button("Guardar cambios"):
                salida = pd.DataFrame([{
                    "Fecha": gasto_sel["Fecha"],
                    "Descripción": gasto_sel["Descripción"],
                    "Cargo": monto_gasto,
                    "UUID": uuid,
                    "Fecha Factura": fecha_factura,
                    "Concepto": concepto,
                    "Monto comprobado": monto_xml,
                    "Diferencias": diferencia,
                    "Comentarios": comentarios
                }])

                # Generar Excel en memoria
                buffer = BytesIO()
                with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
                    salida.to_excel(writer, index=False, sheet_name="Comprobación")

                st.download_button(
                    label="Descargar Excel",
                    data=buffer,
                    file_name="comprobacion.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
