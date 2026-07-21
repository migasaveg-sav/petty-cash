import streamlit as st
import pandas as pd
import xml.etree.ElementTree as ET

st.title("Comprobación de Caja Chica")

# --- Apartado 1: Cargar estado de cuenta ---
st.header("1. Estado de cuenta bancario")
file = st.file_uploader("Sube el archivo del estado de cuenta", type=["csv","xlsx"])

if file:
    # Leer archivo
    if file.name.endswith(".csv"):
        df = pd.read_csv(file)
    else:
        df = pd.read_excel(file)

    # Mostrar tabla con checkbox por cada gasto
    st.subheader("Movimientos")
    df["Seleccionar"] = False  # columna auxiliar
    selected_rows = []

    for i, row in df.iterrows():
        checked = st.checkbox(f"{row['Fecha']} | {row['Descripción']} | ${row['Cargo']}", key=f"chk_{i}")
        if checked:
            selected_rows.append(row)

    # --- Apartado 2: Comprobar gasto con XML ---
    if selected_rows:
        st.header("2. Comprobación de gasto")
        st.write("Seleccionaste los siguientes movimientos:")
        st.dataframe(pd.DataFrame(selected_rows))

        xml_file = st.file_uploader("Sube el XML del CFDI", type=["xml"])
        if xml_file:
            tree = ET.parse(xml_file)
            root = tree.getroot()

            # Ejemplo de extracción (ajusta según tu código Quickel)
            uuid = root.attrib.get("UUID","")
            fecha_factura = root.attrib.get("Fecha","")
            concepto = root.attrib.get("Descripcion","")
            emisor = root.attrib.get("Emisor","")
            iva = root.attrib.get("IVA","")
            monto_xml = float(root.attrib.get("Total","0"))

            # Crear tabla nueva con datos del XML
            comprobacion = pd.DataFrame([{
                "Fecha Factura": fecha_factura,
                "UUID": uuid,
                "Concepto": concepto,
                "Emisor": emisor,
                "IVA": iva,
                "Monto Total": monto_xml
            }])

            st.subheader("Datos extraídos del XML")
            st.dataframe(comprobacion)

            # Validación de diferencias
            for row in selected_rows:
                diferencia = round(float(row["Cargo"]) - monto_xml, 2)
                if 0.01 <= abs(diferencia) <= 0.05:
                    st.warning(f"Diferencia encontrada: {diferencia} pesos en {row['Descripción']}")
                else:
                    st.success(f"Gasto comprobado correctamente: {row['Descripción']}")
