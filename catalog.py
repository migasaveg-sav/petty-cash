"""Catálogo de Categorías y Materiales, para reemplazar los campos de texto libre
originales por listas desplegables (con opción de agregar valores nuevos).

Se guarda en session_state como listas simples de strings; este módulo sólo trae
las funciones puras de manipulación (agregar, quitar, valores por default) para que
la interfaz no tenga que reimplementarlas.
"""
from __future__ import annotations

CATEGORIAS_DEFAULT = [
    "Papelería y oficina",
    "Alimentos y bebidas",
    "Transporte y viáticos",
    "Mantenimiento",
    "Limpieza",
    "Servicios",
    "Herramientas",
    "Otros",
]

MATERIALES_DEFAULT = [
    "N/A",
]

# Catálogo de "Category" para la bitácora de solicitudes de reembolso, tomado tal
# cual de la hoja "Categories" del Excel de plantilla que compartió el usuario.
CATEGORIAS_SOLICITUD_DEFAULT = [
    "Office Supplies",
    "Office Utilities (Water & Electricity & Internet)",
    "Cleaning Expenses",
    "Accommodation",
    "Flight",
    "Car Rental",
    "Car Rental Gas",
    "Travel Meal",
    "Client Entertainment",
    "Others",
    "Reimbursement Inflow",
    "Returned by Employee",
]

# Catálogo de "Applicant" para la bitácora de solicitudes, tomado de la hoja
# "Applicant" del Excel de plantilla que compartió el usuario.
APLICANTES_DEFAULT = [
    "Ximena",
    "Antonieta",
    "Karen",
]

# Catálogo de "Employee name" para la bitácora de solicitudes, tomado de la hoja
# "Employee name" del Excel de plantilla (se quitaron los tabs finales de cada
# nombre que traía el archivo original).
EMPLEADOS_DEFAULT = [
    "ADRIAN NUNEZ XIMENA JOCELYN",
    "AGUILAR HERRERA AXEL",
    "ALVAREZ HERNANDEZ ANGEL ADRIAN",
    "ALVAREZ MORENO NOEMI MARINA",
    "Antonieta Guevara Tapia",
    "ARANDA UC NORKA YAMILE",
    "ARRIAGA SEGOVIA OLAM YHOSHUA",
    "CARDOZA CARDOZA MARIA CRISTINA",
    "CERRITOS HERNANDEZ JORGE",
    "CHAVEZ PEREZ GABRIELA",
    "CHEN KUAN CHANG",
    "CORTES MORALES ISAAC ALFONSO",
    "CRUZ LINO JOSE DANIEL",
    "DANIEL CHAVEZ IBARRA",
    "DE HARO GRIMALDO MARIA DE LOS ANGELES",
    "ERNESTO FONSECA RIOS",
    "FONSECA RIOS ERNESTO",
    "GARCIA LOPEZ MONSERRAT",
    "GUTIERREZ RIOS KAREN SAMANETH",
    "HEREDIA MARQUEZ MIGUEL ANGEL",
    "HUGO ARMANDO AHUMADA ARMENTA",
    "JUAREZ RUIZ LUIS ARTURO",
    "LIU LETIAN",
    "LOPEZ HERNANDEZ NORMA LESLIE",
    "LUNA MARTAGON OMAR",
    "MARQUEZ CRUZ MARIA GUADALUPE",
    "ROGELIO GONZALEZ MORALES",
    "SHANCHEZ VEGA MIGUEL ANGEL",
    "SHENG YONG",
    "WANG CHAO",
    "X TIAN GESI",
    "YU CHEN",
    "Yuping Deng Luna",
    "ZHANG PEI",
    "ZHANG YONGWEN",
    "ZHAO YING",
    "ZUNYA FENG",
    "GUTIERREZ CENTENO XAVIER HUMBERTO",
]


def agregar_valor(catalogo: list[str], valor: str) -> list[str]:
    """Regresa una copia del catálogo con `valor` agregado si no existía ya
    (comparación insensible a mayúsculas/espacios). No modifica la lista original."""
    valor = valor.strip()
    if not valor:
        return list(catalogo)
    existentes_lower = {v.strip().lower() for v in catalogo}
    if valor.lower() in existentes_lower:
        return list(catalogo)
    return [*catalogo, valor]


def quitar_valor(catalogo: list[str], valor: str) -> list[str]:
    return [v for v in catalogo if v != valor]


def catalogo_inicial(defaults: list[str], desde_archivo: list[str] | None = None) -> list[str]:
    """Combina los valores por default con una lista opcional cargada de un archivo
    (por ejemplo un Excel de bootstrap), sin duplicar y preservando orden de aparición."""
    resultado = list(defaults)
    for v in (desde_archivo or []):
        resultado = agregar_valor(resultado, v)
    return resultado
