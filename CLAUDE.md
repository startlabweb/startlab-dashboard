# CLAUDE.md

## Project Overview

Sistema que evalúa candidatos con IA. Vigila planillas de Google donde caen las
respuestas de formularios de postulación, le pone nota a las respuestas escritas y al
video de cada persona, y escribe esas notas de vuelta en la misma planilla. El equipo
de Startlab decide leyendo la planilla; el sistema solo evalúa y avisa.

Vive en Railway: `https://startlab-dashboard-production-4e59.up.railway.app`

## WAT Framework

### Workflow

Hay **cuatro monitores activos** (becas del Bootcamp AVO, Editor experto en IA, Setter
y Consultor de Negocios). Los tres primeros: el candidato manda el formulario → cae una
fila en la planilla → el sistema la descubre (mira cada 60 s) → le pone nota a las
respuestas escritas → baja el audio del video, lo transcribe y le pone nota → escribe
las dos notas y sus explicaciones en la planilla.

El monitor del **Consultor de Negocios** tiene dos etapas más (ver *Current State*):

- **Gate 1** — pasa quien saque ≥70 en preguntas y ≥10 en video. El sistema lo marca y
  avisa a Slack; **Paula aprueba a mano** después de mirar el video, porque presencia y
  tono no se puntúan con una rúbrica.
- **Sesión de Business IQ Test** — el aprobado agenda; de la sesión queda una grabación
  en Drive que el sistema transcribe y corrige contra los dos casos del playbook.
- **Gate 2** — pasa quien identifique las dos palancas correctas (la asistencia en el
  Caso 1, la publicidad en el Caso 2) y va a entrevista con el líder comercial.

### Agents

- **El worker** — el único agente automático, corre en Railway. Por cada monitor tiene
  un ciclo que *descubre* filas nuevas (barato, cada 60 s) y dos que *procesan*
  candidatos (caros, usan IA). Separados a propósito: descubrir tarda segundos,
  procesar puede tardar horas.
- **Paula** — aprueba o rechaza a mano tras el Gate 1 escribiendo Sí/No en una columna.
  Su decisión gana sobre el corte de puntaje en los dos sentidos, y la excepción queda
  escrita en la columna `Estado`.
- **Adriana** conduce hoy la sesión de IQ Test (la idea es reemplazarla por una IA con
  voz). **Álvaro** hace la entrevista final, fuera del sistema.

### Tools

**Google Sheets** (de dónde se lee y a dónde se escribe: es el entregable) ·
**Google Drive** (los videos y las grabaciones de Meet) · **AssemblyAI** (audio a texto,
con Gemini de respaldo) · **GPT-4o** (el único que pone notas) · **Supabase** (la fuente
de verdad; la planilla es una copia) · **Slack** (el aviso a Paula) · **Recall.ai**
(bots de reunión, solo para las pruebas del bot de voz).

---

## Architecture

```
app/api/        endpoints: monitors, criteria, candidates, sheets, events
app/worker/     manager.py (los ciclos) + processor.py (evaluar un candidato)
app/services/   gates.py (los dos cortes del embudo), criteria_parser, cost_tracker
app/templates/  dashboard (Jinja2 + Tailwind por CDN, sin build)
tools/          una herramienta por integración externa (sheets, drive, slack, ...)
prompts/        las rúbricas, versionadas como texto
migrations/     SQL que se corre A MANO en Supabase, en orden
```

**La decisión de diseño central:** la base de datos es la fuente de verdad y la planilla
es una copia. Toda nota vive primero en Supabase y se copia al Sheet en lote cada 90
segundos. Por eso casi cualquier desastre se arregla re-copiando
(`POST /api/monitors/{id}/sync-sheet?force=true`).

**La cola:** la tabla `candidates` ES la cola. Cada candidato se reserva con vencimiento
de 30 minutos; si el worker muere, la reserva vence y vuelve solo a la fila. Máximo 3
intentos, que también es el techo de gasto.

## Tech Stack

- **Runtime:** Python 3.12 (Docker)
- **Framework:** FastAPI + uvicorn
- **Base de datos:** Supabase (proyecto `uqtleaamjryuvigfsqcl`)
- **Frontend:** plantillas Jinja2 con Tailwind por CDN, sin build
- **Deploy:** Railway, desde la rama `master` de GitHub (`startlabweb/startlab-dashboard`)

## Development Commands

```bash
uvicorn app.main:app --reload          # levantar local (necesita el .env)
python tools/prueba_recall.py "<url>"  # ¿entra un bot a esa reunión sin que lo admitan?
```

Las migraciones **no corren solas**: se copian a mano en el editor SQL de Supabase, en
orden (`001`, `002`, `003`, `004`). Todas son idempotentes: se pueden correr dos veces.

## Important Files & Patterns

- `app/worker/processor.py` — el corazón. Cada función tiene escrito **por qué** es así
  y qué bug arregló. Leerlo antes de tocar la evaluación.
- `app/services/gates.py` — los dos cortes, el aviso a Slack y la búsqueda de la sesión
  en Drive. Todo barato y sin IA: corre en el ciclo de descubrir.
- `tools/meet_recordings.py` — cruza los archivos de Meet con los candidatos por el
  nombre entre paréntesis del título. **Si no matchea con nadie, o matchea con dos, no
  adivina**: ponerle la nota de una sesión a otra persona es el error más caro posible.
- `prompts/*.md` — las rúbricas. Se cargan **textuales** por API, nunca por el parser
  de IA (tiene como regla explícita inventar umbrales cuando no los encuentra).

## Environment Variables

Solo los nombres; los valores están en Railway y en el `.env` local.

- `SUPABASE_URL` / `SUPABASE_KEY` — la base de datos
- `GOOGLE_SERVICE_ACCOUNT_JSON` — la cuenta robot que lee planillas y Drive
  (`evaluador-pipeline@startlab-evaluaciones.iam.gserviceaccount.com`)
- `OPENAI_API_KEY` — quien pone las notas · `ASSEMBLYAI_API_KEY` / `GEMINI_API_KEY` —
  transcripción y su respaldo
- `SLACK_WEBHOOK_URL` — el aviso a Paula (vacío = no se avisa, lo demás sigue igual)
- `RECALL_API_KEY` / `RECALL_REGION` — pruebas del bot de voz (`us-east-1`)
- `WORKER_CONCURRENCY`, `POLL_INTERVAL_SECONDS`, `MAX_ATTEMPTS` — diales del worker

## What NOT to Do

- **No agregar columnas al Sheet desde el código.** El sistema nunca crea columnas: si
  falta un encabezado, la escritura **falla en silencio** y solo queda un error en el
  log. Los encabezados se crean a mano antes de encender nada.
- **No cargar rúbricas por el parser de IA.** Van con `prompt_template` textual. Y el
  confirm de la rúbrica de IQ va con body `{}`: mandarle `parsed_criteria` regenera el
  prompt con el parser y pisa la rúbrica fija.
- **No meter `waiting` ni `no_session` en el filtro de la cola.** Un candidato que
  espera su sesión sería reclamado en cada ciclo y quemaría sus 3 intentos contra algo
  que todavía no existe.
- **No cambiar `evaluator_type` de un monitor con candidatos ya ingeridos** — los manda
  a otro pipeline de evaluación. Está bloqueado a propósito en el PATCH.
- **No poner un botón de aprobar en el dashboard.** No tiene login y su URL es pública:
  la aprobación de Paula vive en la planilla, que sí tiene permisos por persona.
- **No reordenar filas de la planilla a mano** mientras el worker corre.

## Security Approvals

Reglas de cumplimiento obligatorio. Pedir confirmación explícita antes de cualquier
acción que toque uno de estos puntos.

**Secretos.** Nunca exponer claves ni tokens en el código, en commits ni en el chat:
van solo en `.env` local (que está en `.gitignore`) y en las variables de Railway. Si se
filtra una, rotarla de inmediato y avisar.

**Requiere aprobación explícita antes de ejecutar**
- [ ] Instalar, actualizar o eliminar dependencias.
- [ ] Correr migraciones en Supabase o cambiar el esquema.
- [ ] Cambiar variables de producción o la configuración de Railway.
- [ ] Comandos destructivos: `rm -rf`, `drop table`, `git push --force`, borrar monitores.
- [ ] Encender o apagar un monitor que esté evaluando candidatos reales.
- [ ] Cualquier cosa que le mande un mensaje a un candidato.

**Datos personales.** Las planillas y las transcripciones tienen nombres, mails y
teléfonos de postulantes: nunca subirlos al repo ni pegarlos en un chat. La cuenta de
servicio va con permisos mínimos: hoy solo lectura de Drive y escritura en las planillas
que se le comparten.

**Push al repo.** La cuenta de GitHub activa (`creatorai-stack`) no tiene permiso en los
repos de `startlabweb`: da 403. Pushear con el token de `startlabweb`
(`gh auth token --user startlabweb`), sin tocar la configuración global.

## Current State / WIP

**Funcionando en producción:** los 4 monitores. El del Consultor lleva ~93 candidatos,
76 evaluados. Rúbricas: preguntas sobre 80 puntos, video de presentación sobre 20.

**Desplegado pero APAGADO** (commit `117dd85` + migración `004`): la tercera etapa
(sesión de IQ), los dos gates y el aviso a Slack. Se enciende cuando el monitor tenga
`iq_recordings_folder_id` y los umbrales cargados. Falta antes:

1. Crear en la planilla, a la derecha de todo:
   `Puntaje IQ | Explicación IQ | Califica G1 | Aprobación Paula | Estado`
2. Compartir la carpeta de grabaciones de Meet (`1Y_8CmKXRAeBFBCWTrWPf8TPnj6HIO-7O`)
   con la cuenta de servicio, como lector.
3. Cargar la rúbrica `prompts/consultor_iq.md` por API y confirmarla con body `{}`.
4. `PATCH /api/monitors/0915150d-b496-4fee-b41b-233daef2dd46` con la carpeta y los
   umbrales (70 y 10). **Recién ahí el embudo empieza a escribir.**

**Sin construir — el bot de voz que conduce la sesión.** Probado el 28-ago-2026 con
`tools/prueba_recall.py`: un bot **no logueado NO entra** a un Meet creado por el
sistema, se queda en la sala de espera. Para que entre solo hace falta una cuenta de
Google logueada por SSO; la opción más barata es **SSO parcial** — una unidad
organizativa aparte dentro de `startlabweb.com` con un solo usuario
(`ia@startlabweb.com`, ~US$8,40/mes) y el perfil SAML de Recall asignado solo a esa
unidad. Pendiente de que Startlab lo configure. El plan completo, con costos y
alternativas, está en `~/.claude/plans/vamos-a-agregar-un-generic-lightning.md`.
