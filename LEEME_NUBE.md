# Petty Cash en la nube — Etapa 1 (aplicante)

Qué incluye esta etapa: inicio de sesión por usuario, estado de cuenta en la nube (solo agrega movimientos nuevos),
bitácora con tarjetas de día/semana/acumulado, solicitudes, asistente de comprobación (elige movimiento → XML/factura manual
→ PDF o foto comprimido → guardar con tolerancia de $0.50), envío a revisión, pendientes devueltos con comentarios y
consulta de lotes/pagos de su cuenta. El revisor ve por ahora solo un resumen; su pantalla completa es la Etapa 2.

## 1. Base de datos (Supabase)
En SQL Editor, en este orden, cada script **una sola vez**: 001 → 002 (parte 1) → crear usuarios → 002 (parte 2) → 003 → **004_catalogos_y_funciones.sql** (nuevo).
Si algún script marca "already exists", ese ya se corrió: sigue con el siguiente.

## 2. Archivos para GitHub
En un repositorio (puede ser privado) deben ir, en la raíz:
`app_nube.py, nube_db.py, adjuntos.py, bank.py, cfdi.py, catalog.py, matching.py, persistence.py, requirements.txt, .gitignore`
(los últimos cinco ya los tienes de la app local).

## 3. Streamlit Community Cloud
1. share.streamlit.io → *Create app* → elige el repositorio, rama principal y **Main file path: `app_nube.py`**.
2. *Advanced settings → Secrets*: pega el contenido de `secrets.toml.example` con tu clave **anon/publishable**
   (Supabase → Project Settings → API Keys). Nunca la `service_role`, y no la pegues en chats ni en GitHub.
3. Deploy. La primera vez tarda unos minutos instalando dependencias.

## 4. Usuarios
Authentication → Users → Add user (Auto Confirm), y después la parte 2 de `002_semilla.sql` para ligar cada correo con su rol y cuenta.
Desactiva "Allow new users to sign up". Para pruebas puedes usar correos inventados.

## 5. Lista de prueba sugerida (Etapa 1)
1. Entrar como aplicante de Santander → Estado de cuenta → cargar el archivo → ver los movimientos del mes.
2. Bitácora → Nueva solicitud → Comprobar → elegir movimiento → subir XML y PDF → Guardar. Probar una diferencia de ≤ $0.50 (sale en 🟡) y otra mayor (no deja guardar).
3. Subir el mismo XML otra vez (debe rechazarlo). Revertir un gasto y reutilizarlo.
4. Enviar a revisión. Entrar con otro aplicante: no debe ver nada de la otra cuenta.
5. Para simular al revisor sin su pantalla: SQL Editor → `select public.cerrar_lote(<cuenta_id>, <periodo_id>, array['<solicitud_id>']::uuid[]);` y luego `select public.pagar_lote(<lote_id>, '2026-10-30', 'SPEI 123');` — el aplicante lo verá en *Lotes y pagos*.

## Notas
- Los PDF se guardan **solo comprimidos** (PDF normal para ver/descargar). Plan gratuito de Supabase: 1 GB de archivos; el proyecto se pausa tras 7 días sin actividad.
- Streamlit Community Cloud duerme la app tras inactividad; despierta con un clic.
- Los datos viven en Supabase, no en la app: actualizar el código no borra nada.
