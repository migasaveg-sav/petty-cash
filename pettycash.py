import streamlit as st
import pandas as pd
import xml.etree.ElementTree as ET

st.title("Comprobación de Caja Chica")

# --- Apartado 1: Estado de cuenta ---
st.header("1. Estado de cuenta bancario")
file = st.file_uploader("Sube el archivo del estado de cuenta", type=["csv","xlsx"])

if file:
    # Leer archivo
    if file.name.endswith(".csv"):
        df = pd.read_csv(file)
    else:
        df = pd.read_excel(file)

    # Mostrar columnas detectadas
    st.write("Columnas detectadas:", df.columns.tolist())

    # Renombrar si es necesario (ajusta según tu archivo real)
    df.rename(columns={
        "Concepto":"Descripción",
        "Importe":"Cargo",
        "FECHA":"Fecha"
    }, inplace=True)

    # Mostrar tabla con estilo (contenida en cuadro)
    st.markdown("### Movimientos")
    st.dataframe(df.style.set_table_styles(
        [{'selector': 'th', 'props': [('background-color', '#f0f0f0'),
                                      ('border', '1px solid #ccc'),
                                      ('padding', '5px')]},
         {'selector': 'td', 'props': [('border', '1px solid #ccc'),
                                      ('padding', '5px')]}]
    ))

    # Selección de gasto
    selected_index = st.selectbox("Selecciona el gasto a comprobar", df.index, format_func=lambda i: f"{df.loc[i,'Fecha']} | {df.loc[i,'Descripción']} | ${df.loc[i,'Cargo']}")
    gasto_sel = df.loc[selected_index]

    # --- Apartado 2: Comprobar gasto con XML ---
    st.header("2. Comprobación de gasto")
    xml_file = st.file_uploader("Sube el XML del CFDI", type=["xml"])
    if xml_file:
        tree = ET.parse(xml_file)
        root = tree.getroot()

        # Ejemplo de extracción (ajusta según Quickel)
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

        st.markdown("### Datos extraídos del XML")
        st.dataframe(comprobacion.style.set_table_styles(
            [{'selector': 'th', 'props': [('background-color', '#e0f7fa'),
                                          ('border', '1px solid #ccc'),
                                          ('padding', '5px')]},
             {'selector': 'td', 'props': [('border', '1px solid #ccc'),
                                          ('padding', '5px')]}]
        ))

        # Validación de diferencias
        monto_gasto = float(gasto_sel["Cargo"])
        diferencia = round(monto_gasto - monto_xml, 2)

        if abs(diferencia) <= 0.01:
            st.success(f"Gasto comprobado correctamente. Diferencia: {diferencia}")
        else:
            st.error(f"Diferencia mayor a 1 centavo: {diferencia} pesos")
