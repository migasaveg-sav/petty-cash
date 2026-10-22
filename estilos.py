"""Estilo visual de la app en la nube (misma paleta y apariencia de la app local).

Colores pedidos por el usuario para la bitácora: encabezado rojo #DC143C y borde #344B4A.
"""
from __future__ import annotations

import streamlit as st

C_FONDO = "#EEF1F6"
C_TARJETA = "#FFFFFF"
C_TARJETA_ALT = "#F8FAFC"
C_BORDE = "#7C8AA0"
C_BORDE_SUAVE = "#E3E8F0"
C_TEXTO_OSCURO = "#111827"
C_TEXTO_SECUNDARIO = "#475569"
C_BORDE_BITACORA = "#344B4A"
C_NAVY = "#192333"
C_NAVY_SUAVE = "#28334A"
C_SLATE = "#475569"
C_ACENTO = "#2F5FE0"
C_ACENTO_HOVER = "#2449B8"
C_ACENTO_SUAVE = "#E8EEFD"
C_CORAL_ALERTA = "#DC2626"
C_AMARILLO_ACENTO = "#B45309"
C_GRIS_INACTIVO = "#64748B"
C_VERDE_OK = "#15803D"
C_BITACORA_HEADER = "#DC143C"
C_DIFERENCIA_AMARILLO = "#FFF2CC"
C_PENDIENTE = "#7C3AED"

TONOS = {  # color del borde izquierdo de las tarjetas según el estado
    "pendiente": C_SLATE, "comprobado": C_VERDE_OK, "no_necesario": C_GRIS_INACTIVO, "en_espera": C_PENDIENTE,
    "alerta": C_AMARILLO_ACENTO, "acento": C_ACENTO, "rojo": C_BITACORA_HEADER,
}


def aplicar() -> None:
    st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
html, body, [class*="css"] {{ font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }}
.stApp {{ background-color: {C_FONDO}; color: {C_TEXTO_OSCURO}; }}
.block-container {{ padding-top: 3.4rem; max-width: 1400px; }}
h1, h2, h3, h4, h5 {{ color: {C_NAVY}; font-weight: 700; letter-spacing: -0.01em; }}
[data-testid="stCaptionContainer"], small {{ color: {C_TEXTO_SECUNDARIO} !important; }}

/* -------- Encabezado -------- */
.app-header {{ display: flex; align-items: center; gap: 14px; padding-bottom: 14px; margin-bottom: 8px; }}
.app-header-icon {{ width: 44px; height: 44px; border-radius: 12px; flex-shrink: 0; font-size: 1.4rem;
    background: linear-gradient(135deg, {C_ACENTO}, {C_NAVY}); display: flex; align-items: center; justify-content: center; }}
.app-header-text h1 {{ margin: 0; font-size: 1.5rem; line-height: 1.2; }}
.app-header-text p {{ margin: 2px 0 0 0; color: {C_TEXTO_SECUNDARIO}; font-size: 0.88rem; }}
.usuario-chip {{ text-align: right; line-height: 1.25; color: {C_NAVY}; }}
.usuario-chip small {{ color: {C_TEXTO_SECUNDARIO}; }}

/* -------- Navegación por secciones (segmented control) -------- */
[data-testid="stButtonGroup"] button[role="radio"] {{ border: 1.5px solid {C_BORDE}; font-weight: 600; color: {C_NAVY}; background: {C_TARJETA}; }}
[data-testid="stButtonGroup"] button[role="radio"] p {{ color: {C_NAVY}; font-weight: 600; }}
[data-testid="stButtonGroup"] button[role="radio"]:hover {{ background: {C_ACENTO_SUAVE}; }}
[data-testid="stButtonGroup"] button[role="radio"][aria-checked="true"] {{ background: {C_NAVY} !important; border-color: {C_NAVY} !important; }}
[data-testid="stButtonGroup"] button[role="radio"][aria-checked="true"] p, [data-testid="stButtonGroup"] button[role="radio"][aria-checked="true"] span {{ color: #FFFFFF !important; }}

/* -------- Expanders: encabezado oscuro (secciones) -------- */
[data-testid="stExpander"] {{ border: 1px solid {C_BORDE_SUAVE}; border-radius: 10px; overflow: hidden;
    background-color: {C_TARJETA}; box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04); }}
[data-testid="stExpander"] summary {{ background-color: {C_NAVY}; color: #FFFFFF !important; font-weight: 600; padding: 11px 16px; }}
[data-testid="stExpander"] summary p {{ color: #FFFFFF !important; }}
[data-testid="stExpander"] summary svg {{ fill: #FFFFFF; color: #FFFFFF; }}
[data-testid="stExpander"] summary:hover {{ background-color: {C_NAVY_SUAVE}; }}

/* Renglones compactos del desglose: encabezado claro con borde de color del estado */
[class*="st-key-mv-"] [data-testid="stExpander"] {{ border-radius: 6px; border-left: 5px solid {C_SLATE}; margin-bottom: -6px; }}
[class*="st-key-mv-"] [data-testid="stExpander"] summary {{ background-color: {C_TARJETA}; color: {C_TEXTO_OSCURO} !important; font-weight: 500;
    padding: 6px 12px; font-size: 0.86rem; }}
[class*="st-key-mv-"] [data-testid="stExpander"] summary p {{ color: {C_TEXTO_OSCURO} !important; font-size: 0.86rem; }}
[class*="st-key-mv-"] [data-testid="stExpander"] summary svg {{ fill: {C_NAVY}; color: {C_NAVY}; }}
[class*="st-key-mv-"] [data-testid="stExpander"] summary:hover {{ background-color: {C_ACENTO_SUAVE}; }}
[class*="st-key-mv-comprobado-"] [data-testid="stExpander"] {{ border-left-color: {C_VERDE_OK}; }}
[class*="st-key-mv-no_necesario-"] [data-testid="stExpander"] {{ border-left-color: {C_GRIS_INACTIVO}; }}
[class*="st-key-mv-pendiente-"] [data-testid="stExpander"] {{ border-left-color: {C_AMARILLO_ACENTO}; }}
[class*="st-key-mv-"][class*="-dif-"] [data-testid="stExpander"] summary {{ background-color: {C_DIFERENCIA_AMARILLO}; }}

/* -------- Botones -------- */
.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {{
    background-color: {C_TARJETA}; color: {C_NAVY}; border: 1.5px solid {C_BORDE}; border-radius: 8px; font-weight: 600; transition: all 0.12s ease; }}
.stButton > button:hover, .stDownloadButton > button:hover, .stFormSubmitButton > button:hover {{
    background-color: {C_ACENTO_SUAVE}; color: {C_ACENTO_HOVER}; border-color: {C_ACENTO}; }}
.stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"] {{
    background-color: {C_ACENTO}; color: #FFFFFF; border: 1.5px solid {C_ACENTO}; font-weight: 700; }}
.stButton > button[kind="primary"]:hover, .stFormSubmitButton > button[kind="primary"]:hover {{
    background-color: {C_ACENTO_HOVER}; color: #FFFFFF; border-color: {C_ACENTO_HOVER}; }}
.stButton > button[kind="primary"] p {{ color: #FFFFFF !important; }}

/* -------- Barra lateral oscura -------- */
section[data-testid="stSidebar"] {{ background-color: {C_NAVY}; }}
section[data-testid="stSidebar"] label, section[data-testid="stSidebar"] p, section[data-testid="stSidebar"] span,
section[data-testid="stSidebar"] h1, section[data-testid="stSidebar"] h2, section[data-testid="stSidebar"] h3,
section[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] {{ color: #FFFFFF; }}
section[data-testid="stSidebar"] [data-testid="stCaptionContainer"], section[data-testid="stSidebar"] small {{ color: #AEB8CC !important; }}
section[data-testid="stSidebar"] [data-baseweb="select"] * {{ color: {C_TEXTO_OSCURO}; }}
section[data-testid="stSidebar"] [data-baseweb="select"] > div {{ border-radius: 8px !important; }}
section[data-testid="stSidebar"] [data-testid="stWidgetLabel"] p {{ color: #FFFFFF; font-weight: 600; font-size: 0.88rem; }}
.sidebar-header {{ display: flex; align-items: center; gap: 10px; padding: 2px 0 16px 0; margin-bottom: 14px; border-bottom: 1px solid rgba(255,255,255,0.12); }}
.sidebar-header-icon {{ width: 34px; height: 34px; border-radius: 9px; background-color: {C_ACENTO}; display: flex; align-items: center; justify-content: center; font-size: 1.05rem; flex-shrink: 0; }}
.sidebar-header span {{ color: #FFFFFF; font-weight: 700; font-size: 1.02rem; }}

/* -------- Tarjetas de resumen -------- */
.summary-box {{ background-color: {C_TARJETA}; border-radius: 12px; padding: 14px 16px; border: 1px solid {C_BORDE_SUAVE};
    border-left: 4px solid var(--accent-color, {C_ACENTO}); box-shadow: 0 1px 3px rgba(16, 24, 40, 0.06); margin-bottom: 8px; }}
.summary-title {{ font-size: 0.74rem; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; color: {C_TEXTO_SECUNDARIO}; margin-bottom: 6px; }}
.summary-count {{ font-size: 1.8rem; font-weight: 800; color: {C_TEXTO_OSCURO}; line-height: 1; }}
.summary-amount {{ font-size: 0.9rem; color: {C_TEXTO_SECUNDARIO}; margin-top: 4px; font-weight: 500; }}
.progreso {{ height: 12px; background: {C_BORDE_SUAVE}; border-radius: 6px; overflow: hidden; display: flex; margin: 6px 0 4px 0; }}
.progreso > div {{ height: 100%; }}
.leyenda-progreso {{ font-size: 0.82rem; color: {C_TEXTO_SECUNDARIO}; margin-bottom: 16px; }}

/* -------- Bitácora: encabezado rojo y celdas con borde {C_BORDE_BITACORA} (colores pedidos por el usuario) -------- */
.bitacora-head {{ display: grid; grid-template-columns: 0.5fr 1.1fr 1.1fr 2.6fr 1.4fr 1.6fr; background-color: {C_BITACORA_HEADER}; color: #FFFFFF;
    border: 1px solid {C_BORDE_BITACORA}; border-bottom: 3px solid {C_BORDE_BITACORA}; border-radius: 8px 8px 0 0; margin-bottom: 4px; }}
.bitacora-head div {{ padding: 5px 8px; font-size: 0.78rem; font-weight: 600; text-align: center; }}
[class*="st-key-sol-"], [class*="st-key-sol-"] [data-testid="stVerticalBlockBorderWrapper"] {{ border: 1px solid {C_BORDE_BITACORA}; border-radius: 6px; background: {C_TARJETA}; }}

/* -------- Mensajes -------- */
[data-testid="stAlert"] {{ border-radius: 8px; }}
</style>
""", unsafe_allow_html=True)


def money_html(x) -> str:
    v = float(x or 0)
    t = f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"
    return t.replace("$", "&#36;")


def encabezado(subtitulo: str = "Concilia estados de cuenta contra CFDI y lleva el avance de comprobación") -> str:
    return ('<div class="app-header"><div class="app-header-icon">💰</div><div class="app-header-text">'
            f'<h1>Comprobación de Caja Chica</h1><p>{subtitulo}</p></div></div>')


def tarjeta(titulo: str, valor: str, sub: str = "", tono: str = "acento") -> str:
    color = TONOS.get(tono, C_ACENTO)
    return (f'<div class="summary-box" style="--accent-color:{color}"><div class="summary-title">{titulo}</div>'
            f'<div class="summary-count">{valor}</div><div class="summary-amount">{sub or "&nbsp;"}</div></div>')


def barra_progreso(comprobados: int, no_necesarios: int, sin_comprobar: int, pendientes: int = 0) -> str:
    total = max(comprobados + no_necesarios + sin_comprobar + pendientes, 1)
    p = lambda n: f"{n / total * 100:.2f}%"
    resuelto = (comprobados + no_necesarios) / total
    return (f'<div class="progreso"><div style="width:{p(comprobados)};background:{C_VERDE_OK}"></div>'
            f'<div style="width:{p(no_necesarios)};background:{C_GRIS_INACTIVO}"></div>'
            f'<div style="width:{p(pendientes)};background:{C_PENDIENTE}"></div>'
            f'<div style="width:{p(sin_comprobar)};background:{C_BORDE_SUAVE}"></div></div>'
            f'<div class="leyenda-progreso"><b>{resuelto:.0%} resuelto</b> · {comprobados} comprobados · '
            f'{no_necesarios} no necesarios · {pendientes} pendientes · {sin_comprobar} sin comprobar</div>')


def cuadre_html(c: dict) -> str:
    """Línea de validación: comprobados + no necesarios + sin comprobar + pendientes = total del estado de cuenta."""
    por = c["por_estado"]
    n = lambda k: por[k]["n"]
    suma = f'{n("comprobado")} + {n("no_necesario")} + {n("pendiente")} + {n("en_espera")}'
    if c["cuadra"]:
        return (f'<div style="border-left:5px solid {C_VERDE_OK};background:#F0FDF4;padding:8px 12px;border-radius:6px;margin:6px 0">'
                f'<b style="color:{C_VERDE_OK}">✔ El estado de cuenta cuadra</b> · comprobados + no necesarios + sin comprobar + pendientes = '
                f'{suma} = <b>{c["n_total"]}</b> movimientos · {money_html(c["monto_total"])}</div>')
    return (f'<div style="border-left:5px solid {C_CORAL_ALERTA};background:#FEF2F2;padding:8px 12px;border-radius:6px;margin:6px 0">'
            f'<b style="color:{C_CORAL_ALERTA}">✘ No cuadra</b> · {suma} = {c["n_suma"]} y el estado de cuenta tiene {c["n_total"]} movimientos '
            f'(diferencia {c["dif_n"]} mov. / {money_html(c["dif_monto"])})</div>')
