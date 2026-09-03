import sys
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import openpyxl
import pandas as pd
import pytest

from bank import (
    BankStatementError,
    _corregir_mojibake,
    _es_texto_concatenado,
    _quitar_comillas_envolventes,
    cargar_estado_cuenta,
    detectar_mapa_columnas,
    PLANTILLAS_BANCO,
)

CSV_CARGO_ABONO = (
    "Fecha,Descripcion,Cargo,Abono,Saldo\n"
    "2026-06-01,Compra papeleria,150.50,,1000.00\n"
    "2026-06-02,Deposito,,500.00,1500.00\n"
    "2026-06-03,Pago proveedor,300.00,,1200.00\n"
).encode("utf-8")

CSV_IMPORTE = (
    "Fecha,Descripcion,Importe,Saldo\n"
    "2026-06-01,Compra papeleria,-150.50,1000.00\n"
    "2026-06-02,Deposito,500.00,1500.00\n"
).encode("utf-8")

CSV_LATIN1 = (
    "Fecha,Descripción,Importe\n"
    "2026-06-01,Pago a José,-99.00\n"
).encode("cp1252")

# Algunos bancos exportan cada celda envuelta en comillas simples como texto
# literal (no como comilla de CSV), lo que antes rompía la lectura de fechas.
CSV_CON_COMILLAS = (
    "Fecha,Descripcion,Importe,Saldo\n"
    "'2026-06-01','Compra papeleria','-150.50','1000.00'\n"
    "'2026-06-02','Deposito','500.00','1500.00'\n"
).encode("utf-8")


def test_cargo_abono_no_se_pisan():
    df = cargar_estado_cuenta(CSV_CARGO_ABONO, "estado.csv", "Genérico (detectar automáticamente)")
    assert list(df["Monto"]) == [-150.50, 500.00, -300.00]


def test_importe_directo():
    df = cargar_estado_cuenta(CSV_IMPORTE, "estado.csv", "Genérico (detectar automáticamente)")
    assert list(df["Monto"]) == [-150.50, 500.00]


def test_fallback_codificacion_cp1252():
    df = cargar_estado_cuenta(CSV_LATIN1, "estado.csv", "Genérico (detectar automáticamente)")
    assert df.loc[0, "Monto"] == -99.00


def test_mapeo_manual_tiene_prioridad():
    mapeo = {"Fecha": "Fecha", "Cargo": "Cargo", "Abono": "Abono"}
    df = cargar_estado_cuenta(
        CSV_CARGO_ABONO, "estado.csv", "Santander 011-1", mapeo_manual=mapeo
    )
    assert "Monto" in df.columns
    assert len(df) == 3


def test_columnas_no_reconocidas_lanza_error():
    csv_raro = b"ColA,ColB\n1,2\n"
    with pytest.raises(BankStatementError):
        cargar_estado_cuenta(csv_raro, "raro.csv", "Genérico (detectar automáticamente)")


def test_quitar_comillas_envolventes():
    assert _quitar_comillas_envolventes("'2026-06-01'") == "2026-06-01"
    assert _quitar_comillas_envolventes('"2026-06-01"') == "2026-06-01"
    assert _quitar_comillas_envolventes("2026-06-01") == "2026-06-01"
    assert _quitar_comillas_envolventes(123.45) == 123.45


def test_fechas_y_montos_con_comillas_envolventes():
    df = cargar_estado_cuenta(CSV_CON_COMILLAS, "estado.csv", "Genérico (detectar automáticamente)")
    assert list(df["Fecha"]) == ["2026-06-01", "2026-06-02"]
    assert list(df["Monto"]) == [-150.50, 500.00]
    assert list(df["Saldo"]) == [1000.00, 1500.00]


def test_detectar_mapa_columnas_no_duplica_estandar():
    plantilla = PLANTILLAS_BANCO["Santander 011-1"]
    mapa = detectar_mapa_columnas(["Fecha", "Descripción", "Concepto"], plantilla)
    # "Descripción" y "Concepto" apuntan al mismo estándar -> sólo una debe ganar
    assert list(mapa.values()).count("Descripción") == 1


def _construir_xlsx_una_columna(encabezado: str, lineas: list[str]) -> bytes:
    """Arma un .xlsx de una sola columna, como el que exporta BBVA con el texto
    de fecha/concepto/cargo/abono/saldo pegado en cada celda (sin delimitador)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([encabezado])
    for linea in lineas:
        ws.append([linea])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


XLSX_BBVA_CONCATENADO = _construir_xlsx_una_columna(
    "Día    Concepto / Referencia   cargo   Abono   Saldo",
    [
        "03-01-2026   COMPRA X/******1234 RFC: ABC 123456AB1 10:00 AUT: 000001   200.00           800.00",
        "02-01-2026   DEPOSITO Y/******1234 RFC: ABC 123456AB1 09:00 AUT: 000002  500.00          1,000.00",
        "01-01-2026   COMPRA Z/******1234 RFC: ABC 123456AB1 08:00 AUT: 000003   150.00           500.00",
    ],
)


def test_detecta_formato_concatenado():
    df_crudo = pd.read_excel(BytesIO(XLSX_BBVA_CONCATENADO))
    assert _es_texto_concatenado(df_crudo)
    # un CSV normal de columnas separadas no debe activar la detección
    df_normal = pd.read_csv(BytesIO(CSV_CARGO_ABONO))
    assert not _es_texto_concatenado(df_normal)


def test_bbva_concatenado_separa_fecha_concepto_monto_saldo():
    df = cargar_estado_cuenta(XLSX_BBVA_CONCATENADO, "estado.xlsx", "BBVA")
    assert list(df["Fecha"]) == ["2026-01-03", "2026-01-02", "2026-01-01"]
    assert list(df["Saldo"]) == [800.00, 1000.00, 500.00]
    # el cargo (compra) se detecta negativo y el abono (depósito) positivo,
    # comparando el cambio de saldo contra el de la fila siguiente (más antigua)
    assert df.loc[0, "Monto"] == -200.00  # compra
    assert df.loc[1, "Monto"] == 500.00  # depósito
    assert df.loc[2, "Monto"] == -150.00  # última fila: sin siguiente, se asume cargo
    assert "COMPRA X" in df.loc[0, "Descripción"]
    assert "DEPOSITO Y" in df.loc[1, "Descripción"]


def test_corregir_mojibake():
    assert _corregir_mojibake("DÃ­a") == "Día"
    assert _corregir_mojibake("Pago a Jose") == "Pago a Jose"  # sin acentos: no toca nada
    assert _corregir_mojibake(123) == 123
