# Petty Cash en la nube — Etapa 1 (aplicante)

Qué incluye esta etapa: inicio de sesión por usuario, estado de cuenta en la nube (solo agrega movimientos nuevos),
bitácora con tarjetas de día/semana/acumulado, solicitudes, asistente de comprobación (elige movimiento → XML/factura manual
→ PDF o foto comprimido → guardar con tolerancia de $0.50), envío a revisión, pendientes devueltos con comentarios y
consulta de lotes/pagos de su cuenta. El revisor ve por ahora solo un resumen; su pantalla completa es la Etapa 2.

## 1. Base de datos (Supabase)
En SQL Editor, en este orden, cada script **una sola vez**: 001 → 002 (parte 1) → crear usuarios → 002 (parte 2) → 003 → **004_catalogos_y_funciones.sql** → **005_envios_estado_cuenta.sql** (nuevo: envío del estado de cuenta a revisión).
Si algún script marca "already exists", ese ya se corrió: sigue con el siguiente.

## 2. Archivos para GitHub
En un repositorio (puede ser privado) deben ir, en la raíz:
`app_nube.py, nube_db.py, adjuntos.py, estilos.py, importar.py, bank.py, cfdi.py, catalog.py, matching.py, persistence.py, requirements.txt, .gitignore`
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

## Envío del estado de cuenta a revisión
Pestaña **📤 Enviados a comprobación**: muestra todo el estado de cuenta del mes (comprobados, no necesarios y pendientes) con barra de avance.
El botón *Enviar estado de cuenta a revisión* manda de una vez todas las solicitudes del mes con gastos comprobados y guarda una foto del avance
(se puede reenviar conforme avanza). Cada movimiento del desglose se abre con un clic y muestra su solicitud, gasto, facturas, PDF y lote.
Desde *Estado de cuenta* también puedes seleccionar movimientos y pulsar **Comprobar seleccionados**; la solicitud se crea al guardar el gasto.

## Importar el avance de la app local (.json)
En **Estado de cuenta → 📥 Importar avance anterior (.json)**: sube el archivo, revisa la vista previa (movimientos, gastos, facturas, PDF) y pulsa *Importar ahora*.
Carga el estado de cuenta (cada movimiento al mes de su fecha), cada gasto comprobado como una solicitud en **borrador** con sus facturas y PDF comprimido,
y junta en un solo gasto los cargos que compartían facturas. Repetirlo no duplica nada (lo ya comprobado se omite). No migra facturas «en proceso» sin guardar.
Si el archivo trae solicitudes de la bitácora que siguen *pendientes* (sin gasto vinculado), también se crean como solicitudes en borrador, cada una en el mes de su fecha; el archivo puede no traer estado de cuenta. Las facturas manuales con texto libre en UUID («sin factura») se guardan como manuales, sin UUID. Se detectan duplicados por fecha, monto, categoría, empleado, descripción y request.
El *Applicant* de las solicitudes es el usuario con el que inicias sesión (no el nombre guardado en el .json). No subas el .json a GitHub.

## Notas
- Los PDF se guardan **solo comprimidos** (PDF normal para ver/descargar). Plan gratuito de Supabase: 1 GB de archivos; el proyecto se pausa tras 7 días sin actividad.
- Streamlit Community Cloud duerme la app tras inactividad; despierta con un clic.
- Los datos viven en Supabase, no en la app: actualizar el código no borra nada.
