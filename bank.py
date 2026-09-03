"""Lectura de estados de cuenta bancarios (CSV/XLSX) con mapeo flexible de columnas.

Lógica pura, sin Streamlit: recibe bytes + nombre de archivo y regresa un DataFrame
normalizado, o lanza BankStatementError con un mensaje entendible por un usuario final.

A diferencia de la versión original, este módulo:
- No está atado a un banco fijo ("Santander 011-1"): el mapeo de columnas se pasa
  como parámetro (ver `PLANTILLAS_BANCO` y `detectar_mapa_columnas`) y siempre se
  puede sobreescribir a mano desde la interfaz (`mapeo_manual`).
- Maneja Cargo y Abono como dos columnas independientes (no una sola "Cargo/Abono"
  que se pisan entre sí): el monto final es abono - cargo, cada uno tratado con su
  propio signo, en vez de que la segunda columna encontrada sobreescriba a la primera.
- Intenta varias codificaciones (utf-8, cp1252, latin-1) para CSV en vez de tronar
  con bancos que exportan en Windows-1252.
"""
from __future__ import annotations

import re
from io import BytesIO
from typing import Any

import pandas as pd

# ============================================================
# PLANTILLAS DE MAPEO POR BANCO (wildcard en minúsculas -> nombre estándar)
# ============================================================
# Todas comparten la misma forma; se puede agregar un banco nuevo sin tocar código,
# nada más añadiendo una entrada aquí (o el usuario puede mapear a mano en la UI).
PLANTILLAS_BANCO: dict[str, dict[str, str]] = {
    "Santander 011-1": {
        "fecha": "Fecha",
        "descripción": "Descripción",
        "descripcion": "Descripción",
        "concepto": "Descripción",
        "referencia": "Descripción",
        "cargo": "Cargo",
        "abono": "Abono",
        "importe": "Importe",
        "monto": "Importe",
        "valor": "Importe",
        "saldo": "Saldo",
    },
    "BBVA": {
        "fecha": "Fecha",
        "descripción": "Descripción",
        "descripcion": "Descripción",
        "concepto": "Descripción",
        "cargo": "Cargo",
        "abono": "Abono",
        "importe": "Importe",
        "monto": "Importe",
        "saldo": "Saldo",
    },
    "ICBC": {
        "fecha": "Fecha",
        "descripción": "Descripción",
        "descripcion": "Descripción",
        "concepto": "Descripción",
        "cargo": "Cargo",
        "abono": "Abono",
        "importe": "Importe",
        "monto": "Importe",
        "saldo": "Saldo",
    },
    "Genérico (detectar automáticamente)": {
        "fecha": "Fecha",
        "descripción": "Descripción",
        "descripcion": "Descripción",
        "concepto": "Descripción",
        "referencia": "Descripción",
        "cargo": "Cargo",
        "débito": "Cargo",
        "debito": "Cargo",
        "abono": "Abono",
        "crédito": "Abono",
        "credito": "Abono",
        "importe": "Importe",
        "monto": "Importe",
        "valor": "Importe",
        "saldo": "Saldo",
    },
}

BANCOS_DISPONIBLES = list(PLANTILLAS_BANCO.keys())

CODIFICACIONES_CSV = ["utf-8", "utf-8-sig", "cp1252", "latin-1"]


class BankStatementError(Exception):
    """Se lanza cuando el estado de cuenta no se puede leer o mapear."""


def _quitar_comillas_envolventes(valor: Any) -> Any:
    """Quita comillas simples o dobles que envuelven un valor de texto (p. ej.
    "'2024-01-15'" -> "2024-01-15"). Algunos bancos exportan TODAS las celdas de
    su CSV envueltas en comillas simples como texto literal (no como el carácter
    de comillas de CSV), lo que impide que pandas reconozca fechas y números."""
    if not isinstance(valor, str):
        return valor
    v = valor.strip()
    while len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1].strip()
    return v


def _limpiar_comillas_df(df: pd.DataFrame) -> pd.DataFrame:
    """Aplica `_quitar_comillas_envolventes` a todas las columnas de texto de un
    DataFrame recién leído, antes de intentar convertir fechas o montos."""
    df = df.copy()
    for col in df.columns:
        # object (pandas < 3) o el nuevo dtype "str" (pandas >= 3) — nunca numérico.
        if pd.api.types.is_object_dtype(df[col]) or pd.api.types.is_string_dtype(df[col]):
            df[col] = df[col].map(_quitar_comillas_envolventes)
    return df


def _corregir_mojibake(texto: Any) -> Any:
    """Corrige texto UTF-8 que fue interpretado por error como Latin-1/cp1252 y
    reescrito así (patrón típico: 'DÃ­a' en vez de 'Día'). Pasa de largo si no
    detecta ese patrón, o si el intento de reparación falla."""
    if not isinstance(texto, str) or ("Ã" not in texto and "Â" not in texto):
        return texto
    try:
        return texto.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return texto


# Fecha al inicio de la línea (DD-MM-AAAA o DD/MM/AAAA) seguida del resto del texto.
_PATRON_FILA_CONCATENADA = re.compile(r"^\s*(\d{2}[-/]\d{2}[-/]\d{4})\s+(.*\S)\s*$")
# Un importe con dos decimales, con o sin separador de miles.
_PATRON_IMPORTE = re.compile(r"-?[\d,]+\.\d{2}")


def _es_texto_concatenado(df_crudo: pd.DataFrame) -> bool:
    """Detecta el caso de un estado de cuenta exportado como una sola columna de
    texto libre, con fecha/concepto/cargo/abono/saldo pegados en cada celda y
    separados sólo por espacios (típico cuando el estado de cuenta se genera a
    partir de un PDF, p. ej. algunos exportes de BBVA)."""
    if df_crudo.shape[1] != 1:
        return False
    encabezado = _corregir_mojibake(str(df_crudo.columns[0])).lower()
    campos_esperados = (
        ("concepto" in encabezado or "referencia" in encabezado)
        and ("cargo" in encabezado or "abono" in encabezado)
        and "saldo" in encabezado
    )
    if not campos_esperados:
        return False
    col = df_crudo.iloc[:, 0].dropna().astype(str)
    if col.empty:
        return False
    proporcion_con_fecha = col.map(lambda v: bool(_PATRON_FILA_CONCATENADA.match(v))).mean()
    return proporcion_con_fecha > 0.5


def _parsear_fila_texto_concatenado(linea: str) -> dict[str, Any] | None:
    """Separa una línea 'DD-MM-AAAA   concepto largo...   85.10   282.36' en sus
    partes. No se puede usar la posición del carácter como si fueran columnas de
    ancho fijo: el texto no viene monoespaciado, así que el concepto cambia de
    longitud entre filas y desplaza todo lo que sigue. En vez de eso se toman
    los últimos DOS números de la línea (importe y saldo) y todo lo anterior a
    esos números, después de la fecha, es el concepto."""
    m = _PATRON_FILA_CONCATENADA.match(linea)
    if not m:
        return None
    fecha_str, resto = m.groups()
    numeros = list(_PATRON_IMPORTE.finditer(resto))
    if len(numeros) < 2:
        return None
    importe_match, saldo_match = numeros[-2], numeros[-1]
    concepto = resto[: importe_match.start()].strip()
    try:
        importe_abs = float(importe_match.group().replace(",", ""))
        saldo = float(saldo_match.group().replace(",", ""))
    except ValueError:
        return None
    fecha = pd.to_datetime(fecha_str, dayfirst=True, errors="coerce")
    if pd.isna(fecha) or not concepto:
        return None
    return {"Fecha": fecha, "Descripción": concepto, "_ImporteAbs": importe_abs, "Saldo": saldo}


def _clasificar_cargo_abono(filas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Determina si cada importe es Cargo (egreso, negativo) o Abono (ingreso,
    positivo) comparando el saldo de cada fila contra el de la fila siguiente.

    El estado de cuenta concatenado no distingue Cargo de Abono por columna (esa
    información se perdió al aplanar el texto), así que se reconstruye con
    aritmética: como el archivo viene del movimiento más reciente al más
    antiguo (como lo exporta el banco), el saldo ANTES de la fila `i` es el
    saldo de la fila `i + 1`. Si `saldo[i] - saldo[i+1]` coincide en magnitud
    con el importe de la fila, ese signo es el correcto. La última fila (el
    movimiento más antiguo del archivo) no tiene una fila siguiente con la que
    comparar; se asume Cargo (el caso más común en caja chica) y conviene
    revisarla a mano si no aplica."""
    n = len(filas)
    for i in range(n):
        importe_abs = filas[i]["_ImporteAbs"]
        if i + 1 < n:
            delta = filas[i]["Saldo"] - filas[i + 1]["Saldo"]
            filas[i]["Monto"] = delta if abs(abs(delta) - importe_abs) <= 0.02 else -importe_abs
        else:
            filas[i]["Monto"] = -importe_abs
    return filas


def _procesar_texto_concatenado(df_crudo: pd.DataFrame) -> pd.DataFrame:
    """Convierte el DataFrame de una sola columna (ver `_es_texto_concatenado`)
    en el mismo formato normalizado que produce `cargar_estado_cuenta`."""
    lineas = df_crudo.iloc[:, 0].dropna().astype(str)
    filas: list[dict[str, Any]] = []
    for linea in lineas:
        fila = _parsear_fila_texto_concatenado(_corregir_mojibake(linea))
        if fila is not None:
            filas.append(fila)

    if not filas:
        raise BankStatementError(
            "No se pudo separar el texto concatenado en fecha/concepto/importe/saldo. "
            "Revisa que cada fila tenga el formato 'DD-MM-AAAA  CONCEPTO  IMPORTE  SALDO'."
        )

    filas = _clasificar_cargo_abono(filas)
    df = pd.DataFrame(filas)
    df["Fecha"] = df["Fecha"].dt.strftime("%Y-%m-%d")
    df = df.drop(columns=["_ImporteAbs"])[["Fecha", "Descripción", "Monto", "Saldo"]]
    df.reset_index(drop=True, inplace=True)
    return df


def _leer_crudo(file_bytes: bytes, filename: str) -> pd.DataFrame:
    """Lee el archivo crudo (sin mapear columnas) como DataFrame, probando varias
    codificaciones si es CSV."""
    nombre = filename.lower()
    if nombre.endswith(".csv"):
        ultimo_error: Exception | None = None
        for codificacion in CODIFICACIONES_CSV:
            try:
                return pd.read_csv(BytesIO(file_bytes), encoding=codificacion)
            except (UnicodeDecodeError, UnicodeError) as e:
                ultimo_error = e
                continue
            except Exception as e:  # otros errores de parseo de CSV
                ultimo_error = e
                continue
        raise BankStatementError(
            f"No se pudo leer el CSV con ninguna codificación probada "
            f"({', '.join(CODIFICACIONES_CSV)}). Último error: {ultimo_error}"
        )
    try:
        return pd.read_excel(BytesIO(file_bytes))
    except Exception as e:
        raise BankStatementError(f"No se pudo leer el archivo Excel: {e}") from e


def detectar_mapa_columnas(
    columnas_originales: list[str], plantilla: dict[str, str]
) -> dict[str, str]:
    """Regresa {columna_original: nombre_estándar} usando coincidencia de wildcard
    (substring, sin distinguir mayúsculas) contra la plantilla. Cada nombre estándar
    se usa a lo más una vez (la primera columna que coincide gana)."""
    rename_map: dict[str, str] = {}
    for col in columnas_originales:
        col_lower = str(col).strip().lower()
        for wildcard, nombre_estandar in plantilla.items():
            if wildcard in col_lower and nombre_estandar not in rename_map.values():
                rename_map[col] = nombre_estandar
                break
    return rename_map


def _construir_columna_monto(df: pd.DataFrame) -> pd.Series:
    """Construye la columna 'Monto' final a partir de las columnas normalizadas
    disponibles, sin que una le gane a la otra por accidente:
    - Si hay 'Importe': se usa tal cual (ya viene con signo).
    - Si hay 'Cargo' y/o 'Abono' por separado: Monto = Abono - Cargo (cada una
      tratada como número positivo; si el banco ya trae a Cargo en negativo se
      normaliza con valor absoluto antes de restar, para no duplicar el signo).
    """
    if "Importe" in df.columns:
        return pd.to_numeric(df["Importe"], errors="coerce").fillna(0.0).astype(float)

    cargo = (
        pd.to_numeric(df["Cargo"], errors="coerce").fillna(0.0).abs()
        if "Cargo" in df.columns
        else 0.0
    )
    abono = (
        pd.to_numeric(df["Abono"], errors="coerce").fillna(0.0).abs()
        if "Abono" in df.columns
        else 0.0
    )
    if isinstance(cargo, float) and isinstance(abono, float):
        # ninguna de las dos columnas existe
        return pd.Series(0.0, index=df.index)
    return (abono - cargo).astype(float)


def cargar_estado_cuenta(
    file_bytes: bytes,
    filename: str,
    banco: str,
    mapeo_manual: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Carga y normaliza un estado de cuenta.

    `banco` selecciona la plantilla de detección automática (ver PLANTILLAS_BANCO).
    `mapeo_manual`, si se da, es {columna_original: nombre_estándar} y tiene
    prioridad total sobre la detección automática (permite corregir a mano cuando
    el banco no está en la lista o cambió sus encabezados).

    Nombres estándar reconocidos: Fecha, Descripción, Cargo, Abono, Importe, Saldo.
    Siempre produce una columna 'Monto' (positivo = abono/ingreso, negativo =
    cargo/egreso) lista para comparar contra facturas.
    """
    df_crudo = _leer_crudo(file_bytes, filename)
    if df_crudo.empty:
        raise BankStatementError("El archivo no contiene filas.")

    if mapeo_manual is None and _es_texto_concatenado(df_crudo):
        return _procesar_texto_concatenado(df_crudo)

    df_crudo = _limpiar_comillas_df(df_crudo)

    plantilla = PLANTILLAS_BANCO.get(banco, PLANTILLAS_BANCO["Genérico (detectar automáticamente)"])
    rename_map = dict(mapeo_manual) if mapeo_manual else detectar_mapa_columnas(
        list(df_crudo.columns), plantilla
    )

    if not rename_map:
        raise BankStatementError(
            "No se encontraron columnas que coincidan con los nombres esperados "
            "(Fecha, Descripción, Cargo/Abono o Importe). Usa el mapeo manual de columnas."
        )

    columnas_originales_usadas = list(rename_map.keys())
    df = df_crudo[columnas_originales_usadas].rename(columns=rename_map).copy()

    # si el mapeo manual asignó el mismo nombre estándar a dos columnas originales
    # distintas (posible sólo en mapeo manual), nos quedamos con la primera
    df = df.loc[:, ~df.columns.duplicated()]

    df["Monto"] = _construir_columna_monto(df)

    if "Fecha" in df.columns:
        df["Fecha"] = pd.to_datetime(df["Fecha"], errors="coerce").dt.strftime("%Y-%m-%d")

    if "Saldo" in df.columns:
        df["Saldo"] = pd.to_numeric(df["Saldo"], errors="coerce")

    df.reset_index(drop=True, inplace=True)
    return df


def columnas_disponibles_para_mapeo(file_bytes: bytes, filename: str) -> list[str]:
    """Regresa las columnas originales del archivo, para construir la UI de mapeo manual."""
    return list(_leer_crudo(file_bytes, filename).columns)
