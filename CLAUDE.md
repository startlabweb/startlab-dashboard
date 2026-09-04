# CLAUDE.md

## Project Overview

Sistema que selecciona candidatos con IA, de punta a punta. Invita a postular,
evalúa las respuestas escritas y el video, marca quién pasa el corte, le avisa a
Paula, y a los que ella aprueba les toma un examen oral por Zoom conducido por
una IA con voz, lo corrige y escribe todo en la planilla.

Lo único que hace una persona es **aprobar o rechazar** después de mirar el video
y hacer la entrevista final. Todo lo demás corre solo.

Vive en Railway: `https://startlab-dashboard-production-4e59.up.railway.app`

## WAT Framework

### Workflow

Hay **cuatro monitores activos** (becas del Bootcamp AVO, Editor experto en IA,
Setter y Consultor de Negocios). Los tres primeros hacen solo esto: el candidato
manda el formulario → cae una fila en la planilla → el sistema la descubre (mira
cada 60 s) → le pone nota a las respuestas escritas → baja el audio del video, lo
transcribe y le pone nota → escribe las notas y sus explicaciones en la planilla.

El monitor del **Consultor de Negocios** tiene el embudo completo, y todo esto
corre sin intervención:

1. **Invitación al formulario.** Paula escribe nombre y mail en la planilla de
   asignados; el sistema le manda el correo con el link del formulario y anota la
   fecha de envío. Esa fecha es lo que evita mandarlo dos veces.
2. **Evaluación** de preguntas (sobre 80) y video de presentación (sobre 20).
3. **Gate 1** — pasa quien saque ≥70 en preguntas y ≥10 en video. El sistema lo
   marca, avisa a Slack, y **Paula aprueba a mano** después de mirar el video,
   porque presencia y tono no se puntúan con una rúbrica.
4. **Invitación al IQ Test.** En cuanto Paula escribe "Sí", sale solo el correo
   con un link único.
5. **Agendamiento.** El candidato elige turno de una grilla en **su** zona
   horaria y recibe una invitación de Google Calendar con recordatorio. Puede
   cambiar su horario desde el mismo link.
6. **La sesión.** A la hora, el mismo link crea la reunión de Zoom, lanza el bot
   y lo redirige. Una IA con voz le presenta dos casos de negocio, le repregunta
   y cierra. Queda grabada.
7. **Corrección** contra los dos casos del playbook, automática.
8. **Gate 2** — pasa quien identifique las dos palancas correctas (la asistencia
   en el Caso 1, la publicidad en el Caso 2) y va a entrevista con Álvaro.

**Un solo link para el candidato, de principio a fin.** El del correo hace algo
distinto según el momento: sin horario muestra los turnos, con horario dice
cuándo es, y a la hora lo mete en la sesión. Que no cambie nunca es a propósito:
el candidato lo guarda una vez.

### Agents

- **El worker** — el único agente automático, corre en Railway. Por cada monitor
  tiene un ciclo que *descubre* filas nuevas (barato, cada 60 s, y ahí viven los
  gates, los correos y los avisos) y dos que *procesan* candidatos (caros, usan
  IA). Separados a propósito: descubrir tarda segundos, procesar puede tardar
  horas.
- **La IA de la sesión** — una página web (`app/templates/iq_sala.html`) que
  habla con OpenAI Realtime. El bot de Recall solo la transporta a la reunión: la
  IA *es* la página, el bot es el caño. Su guion está en
  `prompts/consultor_iq_agente.md` y **no conoce las respuestas correctas**.
- **Paula** — aprueba o rechaza escribiendo Sí/No en la columna `Aprobación
  Paula`. Su decisión gana sobre el corte de puntaje en los dos sentidos, y la
  excepción queda escrita en la columna `Estado`.
- **Álvaro** hace la entrevista final, fuera del sistema. **Adriana** conducía las
  sesiones a mano hasta agosto de 2026; sus 4 grabaciones sirvieron para validar
  la rúbrica antes de automatizarla.

### Tools

**Google Sheets** (de dónde se lee y a dónde se escribe: es el entregable) ·
**Google Drive** (los videos de los candidatos) · **Gmail** (los dos correos, vía
la cuenta de servicio haciéndose pasar por `soporte@startlabweb.com`) ·
**Google Calendar** (la invitación con recordatorio) · **Zoom** (las salas, una
por sesión, creadas por API) · **Recall.ai** (mete la página en la reunión como
un participante más) · **OpenAI Realtime** (la voz de la IA) · **AssemblyAI**
(audio a texto, con Gemini de respaldo) · **GPT-4o** (el único que pone notas) ·
**Supabase** (la fuente de verdad; la planilla es una copia) · **Slack** (el
aviso a Paula).

---

## Architecture

```
app/api/        monitors, criteria, candidates, sheets, events,
                iq_agente (la clave efímera y la transcripción de la sala),
                iq_sesion (el link del candidato: agendar, reagendar, entrar)
app/worker/     manager.py (los ciclos) + processor.py (evaluar un candidato)
app/services/   gates.py (los cortes, avisos e invitaciones),
                turnos.py (la grilla de horarios), invitaciones.py (el correo
                del formulario), criteria_parser, cost_tracker
app/templates/  dashboard + iq_sala (la IA) + iq_agenda + iq_mensaje
tools/          una herramienta por integración externa (sheets, drive, slack,
                correo, calendario, zoom, recall, ...)
prompts/        las rúbricas y el guion hablado, versionados como texto
plantillas/     los correos, en texto, para que Paula los pueda corregir
migrations/     SQL que se corre A MANO en Supabase, en orden
```

**La decisión de diseño central:** la base de datos es la fuente de verdad y la
planilla es una copia. Toda nota vive primero en Supabase y se copia al Sheet en
lote cada 90 segundos. Por eso casi cualquier desastre se arregla re-copiando
(`POST /api/monitors/{id}/sync-sheet?force=true`).

**La cola:** la tabla `candidates` ES la cola. Cada candidato se reserva con
vencimiento de 30 minutos; si el worker muere, la reserva vence y vuelve solo a
la fila. Máximo 3 intentos, que también es el techo de gasto.

**Una sesión a la vez, garantizado en la base.** Una licencia de Zoom no puede
tener dos reuniones activas: con dos candidatos simultáneos el segundo no entra
y, peor, iniciar una segunda reunión con "entrar antes que el anfitrión" **puede
terminar la primera sin aviso**. De ahí vienen los turnos. La garantía es un
índice único sobre `(monitor_id, iq_slot_at)`: dos personas que aprietan el mismo
horario en el mismo segundo pasan cualquier validación escrita en Python, y lo
único que detiene al segundo es que la base rechace su escritura.

## Tech Stack

- **Runtime:** Python 3.12 (Docker)
- **Framework:** FastAPI + uvicorn
- **Base de datos:** Supabase (proyecto `uqtleaamjryuvigfsqcl`)
- **Frontend:** plantillas Jinja2 con Tailwind por CDN, sin build
- **Deploy:** Railway, proyecto `lucky-respect`, servicio `startlab-dashboard`,
  desde la rama `master` de GitHub (`startlabweb/startlab-dashboard`)

## Development Commands

```bash
ROLE=web uvicorn app.main:app --reload   # levantar local SIN el worker
python tools/zoom.py "<tema>"            # crear una sala de prueba
python tools/recall.py "<zoom>" "<url>"  # meter la sala del IQ en esa reunión
```

**Levantar local siempre con `ROLE=web`.** Sin eso el worker arranca con el
`.env` de producción y se pone a procesar candidatos reales desde tu máquina.

Las migraciones **no corren solas**: se copian a mano en el editor SQL de
Supabase, en orden (`001` a `005`). Todas son idempotentes. Si se copian con
comentarios, cuidado: un `--` que pierde un guion al pegar rompe todo el script.

## Important Files & Patterns

- `app/worker/processor.py` — el corazón. Cada función tiene escrito **por qué**
  es así y qué bug arregló. Leerlo antes de tocar la evaluación.
- `app/services/gates.py` — los cortes, el aviso a Slack y la invitación al IQ.
  Todo barato y sin IA: corre en el ciclo de descubrir.
- `app/templates/iq_sala.html` — la IA de la sesión. Los comentarios cuentan
  cuatro pruebas reales y qué se aprendió de cada una.
- `tools/meet_recordings.py` — cruza los archivos de Meet con los candidatos por
  el nombre entre paréntesis. **Si no matchea con nadie, o matchea con dos, no
  adivina**: ponerle la nota de una sesión a otra persona es el error más caro
  posible. La misma regla se aplica al buscar un video dentro de una carpeta.
- `prompts/consultor_iq.md` — la rúbrica. Tiene los datos del caso Y la respuesta
  correcta; la sala extrae **solo** el párrafo "Datos que se le dieron:" y
  revienta al arrancar si esa parte llegara a arrastrar la respuesta.
- `prompts/consultor_iq_agente.md` — el guion hablado. Sale del correo de Paula
  (las promesas) y de la sesión transcripta de Adriana (las frases). **Las dos
  palancas correctas no están ahí a propósito.**

## Environment Variables

Solo los nombres; los valores están en Railway y en el `.env` local.

- `SUPABASE_URL` / `SUPABASE_KEY` — la base de datos
- `GOOGLE_SERVICE_ACCOUNT_JSON` — la cuenta robot
  (`evaluador-pipeline@startlab-evaluaciones.iam.gserviceaccount.com`, client ID
  `110893438008296034287`). Tiene delegación de dominio para `gmail.send` y
  `calendar.events`, y las APIs de Gmail y Calendar habilitadas en el proyecto
  `startlab-evaluaciones`.
- `OPENAI_API_KEY` — pone las notas y es la voz de la sesión ·
  `ASSEMBLYAI_API_KEY` / `GEMINI_API_KEY` — transcripción y su respaldo
- `ZOOM_ACCOUNT_ID` / `ZOOM_CLIENT_ID` / `ZOOM_CLIENT_SECRET` — app
  Server-to-Server OAuth. Scopes: crear, leer y borrar reuniones. **Falta**
  `cloud_recording:read:list_recording_files:admin` (leer las grabaciones) y
  `meeting:read:list_meetings:admin` (limpiar salas huérfanas).
- `RECALL_API_KEY` / `RECALL_REGION` — el bot de reunión (`us-east-1`)
- `SLACK_WEBHOOK_URL` — el aviso a Paula en `#dirección-comercial` (vacío = no se
  avisa, lo demás sigue igual). También soporta `SLACK_BOT_TOKEN` + `SLACK_CANAL`.
- `IQ_SALA_TOKEN` — la puerta de `/iq/sala`: crear una clave efímera gasta plata
  y la URL es pública
- `IQ_CORREO_ACTIVO` / `IQ_INVITACION_ACTIVA` — los dos correos, por separado
- `IQ_CORREO_REMITENTE` (`soporte@startlabweb.com`) · `IQ_HOJA_ASIGNADOS` +
  `IQ_HOJA_ASIGNADOS_TAB` · `IQ_LINK_FORM` · `DASHBOARD_URL`
- `IQ_MODELO_VOZ` / `IQ_VOZ` — el modelo de voz y su timbre
- `WORKER_CONCURRENCY`, `POLL_INTERVAL_SECONDS`, `MAX_ATTEMPTS` — diales

## What NOT to Do

- **No agregar columnas al Sheet desde el código.** Si falta un encabezado, la
  escritura **falla en silencio** y solo queda un error en el log. Se crean a
  mano antes de encender nada.
- **No cargar rúbricas por el parser de IA.** Van con `prompt_template` textual.
  Y el confirm de la de IQ va con body `{}`: mandarle `parsed_criteria` regenera
  el prompt con el parser y pisa la rúbrica fija.
- **No meter `waiting` ni `no_session` en el filtro de la cola.** Un candidato
  que espera su sesión sería reclamado en cada ciclo y quemaría sus 3 intentos
  contra algo que todavía no existe.
- **No cambiar `evaluator_type` de un monitor con candidatos ya ingeridos.** Está
  bloqueado a propósito en el PATCH.
- **No poner un botón de aprobar en el dashboard.** No tiene login y su URL es
  pública: la aprobación de Paula vive en la planilla, que sí tiene permisos.
- **No reordenar filas de la planilla a mano** mientras el worker corre.
- **No editar ni reutilizar una fila que el sistema ya leyo.** Lee cada fila UNA
  vez, cuando aparece, y no la vuelve a mirar: el ingest saltea las filas que ya
  existen. Si se le cambian el nombre y el mail, para el sistema sigue siendo la
  persona original -- la nota, el `Estado` y hasta la invitacion por correo van a
  la persona vieja. Paso el 4-sep-2026 con dos candidatos reales: se probo
  escribiendo datos ficticios en una fila y despues se la reescribio con un
  candidato de verdad. **Para probar, fila nueva siempre**, o mejor un candidato
  de prueba directo en la base, que no ocupa ninguna fila.

  Si ya paso: hay que **borrar el registro de la base** para que el ingest vuelva
  a leer esa fila. No existe forma de "re-leerla". Y antes de borrarlo, limpiar
  la celda de `Aprobacion Paula`, o el candidato nuevo entra ya aprobado con una
  decision que era de otro.
- **No desplegar mientras hay una sesión de IQ en curso.** El reinicio se lleva
  la transcripción que la sala guarda en memoria. Pasó con la sesión de Paula y
  se perdió la única copia con hablantes separados.
- **No decidir nada por un evento cuando se puede decidir por un estado.** Dos de
  los peores bugs fueron de esa forma: la invitación al IQ se mandaba al *cambiar*
  la celda de Paula (si el envío estaba apagado en ese instante, el candidato
  quedaba sin invitación para siempre), y el aviso de Slack marcaba a los
  candidatos solo si el envío salía. Un estado (`aprobado y sin invitar`) se
  revisa en cada ciclo y se cura solo; un evento pasa una vez y no vuelve.
- **No hacer que la IA se pueda interrumpir mientras lee un caso.** Un "vamos" del
  candidato cancelaba la respuesta y le hacía repetir media lectura. Va con
  `interrupt_response: false`.

## Security Approvals

Reglas de cumplimiento obligatorio. Pedir confirmación explícita antes de
cualquier acción que toque uno de estos puntos.

**Secretos.** Nunca exponer claves ni tokens en el código, en commits ni en el
chat: van solo en `.env` local (que está en `.gitignore`) y en las variables de
Railway. Si se filtra una, rotarla de inmediato y avisar.

**Requiere aprobación explícita antes de ejecutar**
- [ ] Instalar, actualizar o eliminar dependencias.
- [ ] Correr migraciones en Supabase o cambiar el esquema.
- [ ] Cambiar variables de producción o la configuración de Railway.
- [ ] Comandos destructivos: `rm -rf`, `drop table`, `git push --force`, borrar
      monitores o candidatos.
- [ ] Encender o apagar un monitor que esté evaluando candidatos reales.
- [ ] **Cualquier cosa que le mande un correo o un mensaje a un candidato.**
      Los dos interruptores existen para eso.

**Datos personales.** Las planillas y las transcripciones tienen nombres, mails y
teléfonos de postulantes: nunca subirlos al repo ni pegarlos en un chat. Para
hablar de un candidato se usa el **número de fila**, que no identifica a nadie.

**Push al repo.** La cuenta de GitHub activa da 403 en los repos de
`startlabweb`. Pushear con el token de esa cuenta, sin tocar la configuración
global:

```bash
tk=$(gh auth token --user startlabweb)
git -c credential.helper= push "https://x-access-token:${tk}@github.com/startlabweb/startlab-dashboard.git" master
```

## Pedidos anotados, sin empezar

- **Reporte de estado a Slack.** Un aviso periódico al canal con cómo van las
  personas del proceso: cuántas invitadas, cuántas llenaron el formulario,
  cuántas esperan la aprobación de Paula, cuántas en IQ. Pedido el 2-sep-2026.
  Los datos ya están todos: la fecha de envío en la planilla de asignados y la
  columna `Estado` de la de respuestas alcanzan para armarlo sin tocar nada más.
  No confundir con el aviso del Gate 1, que ya existe y es por candidato.
- **Los 16 videos por recuperar.** La lista con el motivo de cada uno está en la
  hoja `Videos por recuperar` de la planilla del Consultor, lista para que Paula
  los contacte. 14 son videos borrados o inexistentes.
- **Revisar los otros tres monitores** (becas, Editor, Setter) por candidatos
  trabados por las mismas causas: link de carpeta en vez de archivo, video sin
  permiso, video borrado. Nunca se revisaron.

## Current State / WIP

**El embudo del Consultor de Negocios está completo y corriendo solo** desde el
3-sep-2026. El primer candidato real recibió su invitación al IQ Test ese día.

- 105 candidatos, 105 con las preguntas evaluadas, 33 pasan el Gate 1
- 16 con el video sin evaluar por causas del candidato (ver *Pedidos anotados*)
- Los dos correos **prendidos**. Los 106 candidatos que ya estaban quedaron
  marcados como avisados, así que Slack solo habla de los que entren nuevos;
  a los viejos los va resolviendo Paula a mano.
- Turnos: lunes a viernes, 9:00 a 18:00 hora de Chile, cada 30 minutos

**Validado con datos reales, no solo con pruebas:** la corrección se corrió
contra las 4 sesiones que condujo Adriana y coincidió con su criterio en el único
caso donde había verdad documentada (Aponte: acertó el Caso 1, falló el Caso 2).
Después dio 20/20 a dos sesiones donde el candidato acertó las dos palancas.

**Un dato incómodo que quedó abierto:** de las 4 personas que Adriana entrevistó,
3 **no** pasan el corte de 70/10 — dos por un solo punto. Y la que mejor razonó
los casos (14/20) es justamente la que el corte rechaza. Con 105 candidatos el
puntaje del formulario y del video no parece predecir quién razona bien un caso
de negocio. Vale revisar los umbrales con Paula.

**Costos por sesión:** ~US$1,50 de voz (`gpt-realtime-2.1`, bajable a mini con
`IQ_VOZ`), ~US$0,16 del bot de Recall en `web_4_core`, US$0,02 de corrección.
Más Zoom Pro (~US$17/mes) y el espacio de grabación en la nube, que con 40
sesiones de 15 minutos se llena y hay que bajar o borrar.
