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
