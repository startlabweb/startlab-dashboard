-- 004: tercera etapa de evaluacion (Business IQ Test) + gates del proceso de
-- Consultor de Negocios.
--
-- Por que existe: el embudo del consultor tiene tres cortes, no dos. Despues del
-- formulario (escritas /80 + video /20) el candidato agenda una sesion donde se
-- le toma el Business IQ Test, y de esa sesion queda una grabacion/transcripcion
-- en Drive que hay que evaluar contra los dos casos del playbook. El esquema 001
-- solo tiene dos casilleros por candidato.
--
-- Correr a mano en el SQL Editor de Supabase, despues de 003. Es idempotente:
-- se puede correr dos veces.

-- ---------------------------------------------------------------------------
-- 1. Una rubrica mas por monitor: la del IQ
-- ---------------------------------------------------------------------------
-- Se borra el CHECK viejo por lo que DICE, no por como se llama: el nombre lo
-- autogenero Postgres en la migracion 001 y si no coincidiera, el DROP no haria
-- nada y el constraint viejo seguiria rechazando 'iq' -- con el sintoma de que
-- cargar la rubrica falla y nadie sabe por que.
DO $$
DECLARE
  nombre text;
BEGIN
  FOR nombre IN
    SELECT con.conname
      FROM pg_constraint con
      JOIN pg_class rel ON rel.oid = con.conrelid
     WHERE rel.relname = 'criteria'
       AND con.contype = 'c'
       AND pg_get_constraintdef(con.oid) ILIKE '%criteria_type%'
  LOOP
    EXECUTE format('ALTER TABLE criteria DROP CONSTRAINT %I', nombre);
  END LOOP;
END $$;

ALTER TABLE criteria ADD CONSTRAINT criteria_criteria_type_check
  CHECK (criteria_type IN ('written', 'video', 'iq'));

-- ---------------------------------------------------------------------------
-- 2. Config de la etapa IQ en el monitor
-- ---------------------------------------------------------------------------
-- La etapa se activa cuando `iq_recordings_folder_id` NO es null Y hay una
-- rubrica 'iq' confirmada. A proposito NO se usa un `evaluator_type` nuevo:
-- ese campo es inmutable por diseño (UpdateMonitorRequest lo bloquea, porque
-- cambiarlo con candidatos ya ingeridos los manda a otro pipeline). Asi el
-- monitor que ya corre se configura en caliente por PATCH, y los otros tres
-- (becas, editor, setter) siguen comportandose exactamente igual que hoy.
ALTER TABLE monitors
  ADD COLUMN IF NOT EXISTS iq_recordings_folder_id TEXT,
  ADD COLUMN IF NOT EXISTS iq_session_title        TEXT,
  ADD COLUMN IF NOT EXISTS iq_score_column         TEXT DEFAULT 'Puntaje IQ',
  ADD COLUMN IF NOT EXISTS iq_explanation_column   TEXT DEFAULT 'Explicación IQ',
  ADD COLUMN IF NOT EXISTS approval_column         TEXT DEFAULT 'Aprobación Paula',
  ADD COLUMN IF NOT EXISTS gate1_column            TEXT DEFAULT 'Califica G1',
  ADD COLUMN IF NOT EXISTS estado_column           TEXT DEFAULT 'Estado',
  ADD COLUMN IF NOT EXISTS gate1_written_min       NUMERIC,
  ADD COLUMN IF NOT EXISTS gate1_video_min         NUMERIC;

-- `iq_session_title` es el prefijo del titulo de las grabaciones de Meet
-- ("Entrevista - Consultor de negocios"). El matcheo archivo -> candidato sale
-- del nombre entre parentesis en ese titulo, asi que sin prefijo no se matchea
-- nada: es lo que evita confundir una mentoria o una llamada de venta guardada
-- en la misma carpeta con la sesion de un candidato.

-- ---------------------------------------------------------------------------
-- 3. El tercer casillero del candidato + la decision manual del Gate 1
-- ---------------------------------------------------------------------------
-- `iq_status` arranca en 'waiting' A PROPOSITO, y ni 'waiting' ni 'no_session'
-- entran al filtro de la cola (CLAIMABLE en database.py). Si entraran, cada
-- candidato que espera su sesion seria reclamado en cada ciclo y quemaria sus
-- 3 intentos sin que exista todavia nada que evaluar.
ALTER TABLE candidates
  ADD COLUMN IF NOT EXISTS iq_status TEXT NOT NULL DEFAULT 'waiting'
    CHECK (iq_status IN ('waiting','pending','processing','completed','error','no_session')),
  ADD COLUMN IF NOT EXISTS iq_score          NUMERIC,
  ADD COLUMN IF NOT EXISTS iq_breakdown      JSONB,
  ADD COLUMN IF NOT EXISTS iq_explanation    TEXT,
  ADD COLUMN IF NOT EXISTS iq_transcript     TEXT,
  ADD COLUMN IF NOT EXISTS iq_source_file_id TEXT,
  ADD COLUMN IF NOT EXISTS iq_source_kind    TEXT
    CHECK (iq_source_kind IN ('transcript','recording')),
  ADD COLUMN IF NOT EXISTS gate1_pass        BOOLEAN,
  ADD COLUMN IF NOT EXISTS gate1_decision    TEXT NOT NULL DEFAULT 'pendiente'
    CHECK (gate1_decision IN ('pendiente','aprobado','rechazado')),
  ADD COLUMN IF NOT EXISTS gate1_notified_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS gate1_decided_at  TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS gate2_pass        BOOLEAN;

-- Un mismo archivo de Drive no puede quedar asignado a dos candidatos: si el
-- matcheo por nombre se equivoca, es mejor que falle la escritura que evaluar
-- la sesion de una persona y ponerle la nota a otra.
CREATE UNIQUE INDEX IF NOT EXISTS idx_candidates_iq_source
  ON candidates (monitor_id, iq_source_file_id)
  WHERE iq_source_file_id IS NOT NULL;

-- El matcheo busca aprobados esperando sesion; el aviso a Slack busca los que
-- pasaron el Gate 1 y todavia no se avisaron.
CREATE INDEX IF NOT EXISTS idx_candidates_iq
  ON candidates (monitor_id, iq_status, gate1_decision);
CREATE INDEX IF NOT EXISTS idx_candidates_gate1_aviso
  ON candidates (monitor_id, gate1_notified_at);
