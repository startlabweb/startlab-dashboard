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

## Config de Railway y avisos operativos

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
