-- 005: la sesion de IQ conducida por la IA, en vivo, sin agendamiento.
--
-- Por que existe: hasta la 004 la sesion la conducia una persona y de ahi
-- quedaba una grabacion en Drive que el sistema encontraba por el nombre del
-- archivo. Ahora la sesion la crea y la conduce el propio sistema, asi que hay
-- que guardar de que reunion se trata, que bot la atendio y con que link entro
-- el candidato.
--
-- La decision de diseño detras de `iq_session_token`: NO hay agendamiento. El
-- correo le manda al candidato un link nuestro, unico e irrepetible; cuando lo
-- abre, recien ahi se crea la reunion de Zoom y se lanza el bot. Asi el
-- candidato entra cuando quiere, nunca hay dos en la misma sala, y no se paga
-- un bot esperando a alguien que no aparecio.
--
-- Correr a mano en el SQL Editor de Supabase, despues de 004. Es idempotente.

ALTER TABLE candidates
  -- El token del link del correo. Unico por candidato y de un solo uso util:
  -- una vez que la sesion arranco, volver a abrirlo devuelve la misma reunion.
  ADD COLUMN IF NOT EXISTS iq_session_token TEXT,
  -- La reunion de Zoom creada para esta persona.
  ADD COLUMN IF NOT EXISTS iq_meeting_id    TEXT,
  ADD COLUMN IF NOT EXISTS iq_session_url   TEXT,
  -- El bot de Recall que atendio la sesion.
  ADD COLUMN IF NOT EXISTS iq_bot_id        TEXT,
  ADD COLUMN IF NOT EXISTS iq_bot_status    TEXT,
  -- Cuando se le mando la invitacion y cuando entro de verdad. La diferencia
  -- entre las dos es la unica forma de saber cuantos invitados no aparecieron.
  ADD COLUMN IF NOT EXISTS iq_invited_at    TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS iq_started_at    TIMESTAMPTZ;

-- `recall` es una tercera fuente de la transcripcion, ademas del Doc de Meet y
-- el mp4: el bot la manda por webhook cuando la reunion termina, ya en texto.
-- El CHECK viejo se borra por lo que DICE y no por como se llama, por el mismo
-- motivo que en la 004: el nombre lo autogeneró Postgres y si no coincidiera, el
-- DROP no haria nada y seguiria rechazando 'recall' sin que nadie entienda por que.
DO $$
DECLARE
  nombre text;
BEGIN
  FOR nombre IN
    SELECT con.conname
      FROM pg_constraint con
      JOIN pg_class rel ON rel.oid = con.conrelid
     WHERE rel.relname = 'candidates'
       AND con.contype = 'c'
       AND pg_get_constraintdef(con.oid) ILIKE '%iq_source_kind%'
  LOOP
    EXECUTE format('ALTER TABLE candidates DROP CONSTRAINT %I', nombre);
  END LOOP;
END $$;

ALTER TABLE candidates ADD CONSTRAINT candidates_iq_source_kind_check
  CHECK (iq_source_kind IN ('transcript', 'recording', 'recall'));

-- El token se busca en cada visita al link: sin indice es un scan de la tabla
-- entera. Unico ademas de indexado, porque dos candidatos con el mismo token
-- serian dos personas entrando a la misma sesion.
CREATE UNIQUE INDEX IF NOT EXISTS idx_candidates_iq_token
  ON candidates (iq_session_token)
  WHERE iq_session_token IS NOT NULL;

-- El webhook de Recall llega con el id del bot y nada mas: hay que poder
-- encontrar al candidato por ahi.
CREATE INDEX IF NOT EXISTS idx_candidates_iq_bot
  ON candidates (iq_bot_id)
  WHERE iq_bot_id IS NOT NULL;
