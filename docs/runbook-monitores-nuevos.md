# Runbook — Monitores nuevos y migración del closer (ago 2026)

Contexto: Paula cambió el proceso de closer/setter (video de PRESENTACIÓN de 5 min
en vez de roleplay, en español o inglés, subido a Drive) y se abren 3 cargos
nuevos: **Media Buyer**, **Copy Experto** y **Creativo Jr** — los 3 con preguntas
escritas + video de Loom. Este runbook cubre las dos operaciones.

Base URL de producción: `https://startlab-dashboard-production-4e59.up.railway.app`

---

## Parte A — Migrar el monitor de closer/setter al video de presentación

Hacer DESPUÉS del deploy que agrega el endpoint PATCH y la rúbrica
`prompts/video_presentacion.md`.

1. **Identificar el monitor**: `GET /api/monitors` y ubicar el del proceso
   closer/setter por `sheet_title` / `sheet_url`. Anotar sus `video_column` y
   `video_score_column` actuales.
2. **Pausar**: `POST /api/monitors/{id}/stop`. (El watchdog no revive monitores
   pausados.)
3. **En el Google Sheet**: renombrar el header `Puntaje Roleplay` → `Puntaje Video`.
   Las fórmulas `=SUM()` ya escritas no se rompen (referencian celdas, no headers).
   Verificar el header de la pregunta del video en el Form:
   - Si Paula **editó** la pregunta existente → el header cambió solo, anotarlo.
   - Si **agregó** una pregunta nueva → hay una columna nueva a la derecha y la
     vieja quedó huérfana. Anotar el header exacto de la nueva.
4. **Actualizar el monitor**:
   ```
   PATCH /api/monitors/{id}
   {"video_score_column": "Puntaje Video"}
   ```
   (agregar `"video_column": "<header exacto>"` si la pregunta del video cambió).
5. **Cargar la rúbrica nueva de video** (contenido de `prompts/video_presentacion.md`):
   ```
   POST /api/monitors/{id}/criteria
   {
     "raw_text": "Rúbrica video de presentación v1 (ago 2026)",
     "criteria_type": "video",
     "prompt_template": "<contenido completo del .md>",
     "total_points": 20,
     "parsed_criteria": [
       {"name": "Por qué es un gran candidato", "max_points": 4},
       {"name": "Metas con el rol", "max_points": 4},
       {"name": "Reacción a no quedar", "max_points": 3},
       {"name": "Claridad y comunicación", "max_points": 5},
       {"name": "Motivación y actitud", "max_points": 2},
       {"name": "Respeta el tiempo", "max_points": 2}
     ]
   }
   ```
   Después: `POST /api/monitors/{id}/criteria/video/confirm` **con body `{}`**.
   ⚠️ NUNCA mandar `parsed_criteria` en el confirm: regeneraría el prompt con el
   parser genérico y pisaría el prompt fijo.
6. **Reactivar**: `POST /api/monitors/{id}/start`.
7. **Cerrar la ventana de riesgo**: `POST /api/monitors/{id}/sync-sheet?force=true`.
   Reescribe todo y de paso verifica (sin gastar LLM) que la columna
   `Puntaje Video` se resuelve y se escribe.
8. **Prueba real**: 1 respuesta de prueba al Form con un video de presentación
   corto en Drive (~$0.17). Ideal: una en español y una en inglés (~$0.34) para
   validar que el inglés ya no dispara "⚠ REVISAR A MANO".

Los candidatos ya evaluados con la rúbrica de roleplay conservan su nota
(estado terminal, no se re-evalúan).

**Supuesto a confirmar con Paula**: la parte escrita NO cambió. Si el Form nuevo
cambió las preguntas escritas, recargar la rúbrica escrita por el mismo mecanismo
(`POST criteria` tipo `written` + confirm `{}`).

---

## Parte B — Crear los monitores de Media Buyer / Copy Experto / Creativo Jr

**Pendientes que bloquean**: links de los Google Sheets (los Forms existen) y
rúbricas de Paula (escrita + video por cargo).

Por cada cargo, cuando lleguen:

1. **Compartir el Sheet** con el `client_email` de la service account (está en el
   JSON de la env `GOOGLE_SERVICE_ACCOUNT_JSON` en Railway), permiso **Editor**
   (el sistema escribe notas).
2. **Pestaña**: el preview del wizard ahora funciona con cualquier nombre de
   pestaña (usa la primera si no encuentra "Form Responses 1") y el monitor se
   crea con el nombre real. Solo verificar que la pestaña del Form sea la primera.
3. **Crear a mano las columnas del sistema en el Sheet**, a la derecha de las
   preguntas del Form y en este orden:
   `Puntaje Preguntas` | `Explicación` | `Puntaje Video` | `Explicación` | `Puntaje total`
   ⚠️ El sistema NO crea columnas: si falta un header, las escrituras fallan EN
   SILENCIO (solo queda un error en logs). La segunda `Explicación` debe estar
   inmediatamente después de `Puntaje Video`.
4. **Crear el monitor**: wizard (`/monitors/new`, tarjeta "Escritas + Video") o:
   ```
   POST /api/monitors
   {"sheet_url": "...", "sheet_id": "...", "sheet_name": "<pestaña real>",
    "sheet_title": "Media Buyer — Meta Ads", "evaluator_type": "sales",
    "video_column": "<header exacto de la pregunta del Loom>"}
   ```
   Las columnas de puntaje pueden omitirse: el default ya es `Puntaje Video`.
5. **Cargar las rúbricas de Paula** (escrita y video) vía `POST criteria` con
   `prompt_template` fijo + `total_points` + `parsed_criteria`, igual que en la
   Parte A paso 5. El wizard solo soporta el camino del parser de IA (que inventa
   umbrales si faltan) — para rúbricas exactas usar siempre la API con prompt fijo.
   La rúbrica escrita debe pedir un JSON con `puntuacion_total` y `resumen`
   (ver `prompts/becas_escritas.md` como referencia de formato).
6. **Confirmar AMBAS rúbricas antes de activar**: `/start` solo valida la escrita.
   Si se activa sin la de video confirmada, los candidatos con video agotan sus
   3 intentos y quedan varados. (Si pasó: cargar la rúbrica, confirmarla y
   `POST /api/monitors/{id}/retry-all`.)
7. **Activar**: `POST /api/monitors/{id}/start` y verificar en el dashboard que
   `last_poll_at` se mueve.
8. **Prueba con 1 fila** antes de difundir el Form: respuesta de prueba con un
   Loom corto real (~$0.17). Verificar en el Sheet: nota escrita + explicación,
   nota de video + explicación, y la fórmula en `Puntaje total`.

---

---

## Parte C — La etapa de Business IQ Test (Consultor de Negocios)

El proceso del consultor tiene **tres** cortes, no dos:

```
formulario  →  GATE 1 (corte de puntaje + aprobación de Paula)  →  sesión de
Business IQ Test  →  GATE 2 (¿acertó las dos palancas?)  →  entrevista con Álvaro
```

Las tres etapas viven en el **mismo monitor** del formulario
(`Closer - Selection form (Responses)`, `0915150d-b496-4fee-b41b-233daef2dd46`),
que pasa a llamarse *Consultor de Negocios*. Dos monitores sobre la misma planilla
se pisarían las columnas y evaluarían —y cobrarían— dos veces cada respuesta.

Requiere la migración **`004_iq_stage.sql`** corrida en el SQL Editor de Supabase.
La etapa IQ está apagada hasta que el monitor tenga `iq_recordings_folder_id`: los
otros tres monitores (becas, editor, setter) no cambian en nada.

### C.1 — Columnas nuevas en el Sheet (primero, a mano)

Agregar estos headers a la derecha de todo lo que ya está, en este orden:

`Puntaje IQ` | `Explicación IQ` | `Califica G1` | `Aprobación Paula` | `Estado`

⚠ El sistema **NO crea columnas**: si falta un header, la escritura de esa columna
falla **en silencio** (solo queda el error en los logs). `Explicación IQ` tiene que
estar inmediatamente después de `Puntaje IQ`.

Esa planilla tampoco tiene `Puntaje total` (termina en `Column 1 | Column 2`), así
que hoy el cálculo del total loguea "columnas del total no encontradas" en cada
ciclo. Si se agrega ese header, el sistema empieza a escribir solo la fórmula
`=SUM(escritas, video)` — decidirlo a propósito.

### C.2 — Compartir la carpeta de grabaciones con la cuenta de servicio

La carpeta donde Meet deja las grabaciones (hoy
`1Y_8CmKXRAeBFBCWTrWPf8TPnj6HIO-7O`) tiene que estar compartida con el
`client_email` del JSON de `GOOGLE_SERVICE_ACCOUNT_JSON`, permiso **Lector**
(alcanza: el scope de la cuenta es `drive.readonly`).

Si no está compartida, el síntoma es "nunca matchea nada" y en los logs aparece
`No se pudo listar la carpeta ... Revisar que este compartida`.

### C.3 — Activar la transcripción en el evento de la sesión

En la plantilla del evento de agendamiento (Calendar → *Registros de la reunión*),
dejar tildado **Grabar** y **Transcribir**. Hoy solo están la grabación y las notas
de Gemini, y la transcripción es el camino barato:

| fuente | qué cuesta | por qué |
|---|---|---|
| `… - Transcript` | ~$0.02 | 1 request a Drive; trae quién dijo cada cosa |
| `… - Recording` | ~$0.17 | hay que bajar el audio y transcribirlo |
| `… - Notes by Gemini` | — | **no se usa nunca**: es un resumen interpretado, no lo que el candidato dijo |

El **título del evento** tiene que mantener el formato
`Entrevista - Consultor de negocios (<Nombre del candidato>)`: el matcheo
archivo → candidato sale del nombre entre paréntesis, y el prefijo es lo que evita
que una mentoría o una llamada de venta guardada en la misma carpeta matchee con
un candidato homónimo.

### C.4 — Cargar la rúbrica del IQ

Con el contenido textual de `prompts/consultor_iq.md`. **No pasa por el parser de
IA** (la API lo rechaza a propósito): son dos casos con una palanca correcta cada
uno, y el parser tiene como norma inventar umbrales cuando no los encuentra.

```
POST /api/monitors/{id}/criteria
{
  "raw_text": "Rubrica Business IQ v1 (ago 2026): los 2 casos del playbook",
  "criteria_type": "iq",
  "prompt_template": "<contenido completo de prompts/consultor_iq.md>",
  "total_points": 20,
  "parsed_criteria": [
    {"name": "Caso 1 — palanca: asistencia", "max_points": 10},
    {"name": "Caso 2 — palanca: publicidad", "max_points": 10}
  ]
}
```

Después: `POST /api/monitors/{id}/criteria/iq/confirm` **con body `{}`**.
Mandar `parsed_criteria` en el confirm ahora devuelve 400 en vez de pisar el
prompt fijo en silencio.

### C.5 — Configurar el monitor

```
PATCH /api/monitors/0915150d-b496-4fee-b41b-233daef2dd46
{
  "sheet_title": "Consultor de Negocios",
  "gate1_written_min": 70,
  "gate1_video_min": 10,
  "iq_recordings_folder_id": "1Y_8CmKXRAeBFBCWTrWPf8TPnj6HIO-7O",
  "iq_session_title": "Entrevista - Consultor de negocios"
}
```

Los nombres de columna (`Puntaje IQ`, `Explicación IQ`, `Califica G1`,
`Aprobación Paula`, `Estado`) ya vienen por default de la migración; solo hay que
mandarlos si en el Sheet se llaman distinto.

Para **apagar** la etapa IQ hay que poner `iq_recordings_folder_id` en NULL por
SQL: el PATCH ignora los campos nulos (no puede distinguir "no lo mandes" de
"ponelo en null").

### C.6 — Verificar antes de que corte a nadie

1. **Matcheo en seco** — no evalúa, no gasta, no escribe:
   `GET /api/monitors/{id}/iq/sesiones`
   Tiene que resolver las 4 sesiones del 26-ago (Aponte, Gianechini, Yoseff,
   Ortega) y **no** matchear las mentorías ni las llamadas de venta de la misma
   carpeta. `rubrica_iq_confirmada` tiene que decir `true`.
2. **Correr el ciclo ahora**, sin esperar los 60 s:
   `POST /api/monitors/{id}/gates/recompute`
   Devuelve `{"gates": N, "decisiones": N, "avisados": N, "sesiones": N}`.
   La primera vez calcula el corte de los 74 candidatos que ya estaban evaluados.
3. **Slack**: apuntar el webhook a un canal de prueba primero. El aviso sale de a
   tandas de 15 y **una sola vez** por candidato; reiniciar el worker y confirmar
   que no se repite.
4. **Calibrar la rúbrica con las 4 sesiones ya grabadas** antes de usarla para
   descartar: aprobar a esos 4 en la planilla, dejar que se evalúen y comparar el
   veredicto con el de Adriana. Del caso de Aponte ya se sabe la respuesta
   esperada: acertó el Caso 1 (asistencia) y **no** el Caso 2 (propuso no
   aumentar el gasto), así que su Gate 2 tiene que dar "no pasa".
5. **Sheet**: `POST /api/monitors/{id}/sync-sheet?force=true` y mirar que
   `Puntaje IQ`, `Explicación IQ`, `Califica G1` y `Estado` caigan en la fila
   correcta.

### Cómo se mueve un candidato (qué mirar cuando algo no avanza)

| `Estado` en la planilla | quién tiene la pelota | qué hace el sistema |
|---|---|---|
| En evaluacion | nadie, está corriendo | evalúa escritas y video |
| No califica | nadie | nada más; queda con su nota y el motivo |
| Revisar a mano: … | el equipo | el candidato **no** está descartado |
| Califica: esperando aprobacion de Paula | **Paula** | avisó a Slack y espera el Sí/No |
| Rechazado en la revision de Paula | nadie | cierra la etapa IQ (`no_session`) |
| Aprobado: esperando la sesion de IQ | **el candidato** | busca su grabación en Drive cada 60 s |
| Sesion de IQ en evaluacion | nadie | transcribe y puntúa |
| IQ aprobado: pasa a entrevista | **Álvaro** | terminó |

La decisión de Paula gana sobre el corte de puntaje en los dos sentidos: si escribe
`Sí` en alguien que no pasó, avanza igual y en el `Estado` queda
`Aprobado (excepcion al corte)`. En agosto entró a entrevista un candidato con
69/80 sobre un corte de 70, así que el caso es real.

### Lo que se rompe y cómo se ve

- **La rúbrica del IQ no está confirmada** → no se encola ninguna sesión (queda una
  línea `iq_sin_rubrica` en Actividad). Es a propósito: encolar sin rúbrica dejaría
  a cada candidato gastando sus 3 intentos contra una evaluación que no puede
  correr, que es lo que pasó con la rúbrica de video en la convocatoria pasada.
- **Una grabación no matchea con nadie** → línea `iq_sesion_sin_match` en Actividad
  (una vez, no cada ciclo). Suele ser el nombre del evento tipeado distinto al del
  formulario: se arregla renombrando el archivo en Drive.
- **Dos candidatos con el mismo nombre** → `iq_sesion_ambigua`, y **no se asigna a
  ninguno**. Poner la nota de una sesión en la fila de otra persona es el error más
  caro posible acá, así que el sistema prefiere no adivinar.
- **La grabación pesa más de `IQ_MAX_RECORDING_MB`** (default 3000) → error visible
  en la celda de `Puntaje IQ`. Ese tope es aparte de `MAX_VIDEO_SIZE_MB` (500, el
  del video de 5 min del candidato) porque de la sesión se baja solo el audio.
- **Transcripción de menos de 500 caracteres** → error en vez de nota. Una sesión de
  IQ dura 30-40 minutos: un texto corto significa que el archivo no es la sesión.

## Config de Railway y avisos operativos

- `SLACK_WEBHOOK_URL` (requerida para el Gate 1 del consultor): webhook entrante
  del canal de reclutamiento. **Vacia = no se avisa**, y en ese caso el aviso NO se
  marca como enviado: cuando se configure, el proximo ciclo manda los pendientes.
- `DASHBOARD_URL` (opcional): la URL publica del dashboard, solo para el link del
  aviso de Slack.
- `IQ_MAX_RECORDING_MB` (default 3000): tope para la grabacion de una sesion de IQ.
  Va aparte de `MAX_VIDEO_SIZE_MB` (500) porque una sesion de 40 min pesa 150-600 MB
  y de ella se baja unicamente el audio.
- `AUDIO_LANGS_OK` (opcional): idiomas de audio aceptados sin marcar
  "⚠ REVISAR A MANO". Default en código: `es,en`. Para volver a solo español:
  `AUDIO_LANGS_OK=es`.
- **Disco**: `WORKER_CONCURRENCY=2` es POR monitor → 4 monitores activos = hasta
  8 descargas simultáneas contra ~5 GB de disco. Mitigación recomendada mientras
  haya 3+ monitores con volumen: `MAX_VIDEO_SIZE_MB=1024` (alineado con el
  "máx 1 GB" del Form) y `WORKER_CONCURRENCY=1`.
- Rate limit de Sheets: 4 monitores ≈ 10-25 req/min pico contra un límite propio
  de 40/min — OK. Con 5-6 monitores, subir `POLL_INTERVAL_SECONDS`.
- **Badge "Top 5 / suplente"** en el detalle del monitor: está pensado para becas
  (5 cupos). En procesos de hiring de 1 persona, ignorarlo.
- Costos: ~$0.17 por candidato con video (~$0.02 solo escritas).
