-- 003: cola durable con lease sobre `candidates`.
--
-- Problema que resuelve: hoy un candidato que queda en 'processing' se saltea para
-- siempre (processor.py saltea processing y el boton Reintentar solo cubre 'error').
-- Hay 8 candidatos asi desde hace 125 dias. Con una rafaga de 300 y cualquier
-- reinicio del contenedor, eso escala.
--
-- Correr a mano en el SQL Editor de Supabase. Es idempotente: se puede correr dos veces.

-- ---------------------------------------------------------------------------
-- 1. Columnas de lease y reintentos
-- ---------------------------------------------------------------------------
ALTER TABLE candidates
  ADD COLUMN IF NOT EXISTS attempts         INTEGER     NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS started_at       TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS worker_id        TEXT,
  ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ NOT NULL DEFAULT '1970-01-01T00:00:00Z',
  ADD COLUMN IF NOT EXISTS sheet_synced_at  TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- `lease_expires_at` es NOT NULL con default epoch (no nullable) a proposito:
--   * "libre para tomar" queda como un solo .lt() en PostgREST, sin OR anidado
--   * cumple doble funcion como next_attempt_at del backoff
-- Un campo, un indice, cero plpgsql.

-- ---------------------------------------------------------------------------
-- 2. Indices
-- ---------------------------------------------------------------------------
-- El claim ordena por sheet_row (FIFO por fila del formulario) filtrando por
-- monitor y lease vencido. El indice de 001 (monitor_id, written_status,
-- video_status) no sirve para esto.
CREATE INDEX IF NOT EXISTS idx_candidates_claim
  ON candidates (monitor_id, lease_expires_at, sheet_row);

-- El flusher busca lo terminal que todavia no se escribio en el Sheet.
CREATE INDEX IF NOT EXISTS idx_candidates_sheet_sync
  ON candidates (monitor_id, sheet_synced_at);

-- ---------------------------------------------------------------------------
-- 3. updated_at real
-- ---------------------------------------------------------------------------
-- La columna existia en `monitors` desde 001 pero sin trigger, o sea congelada
-- en el valor de insercion. Sin esto no se puede auditar nada de la corrida.
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := NOW();
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS candidates_touch_updated_at ON candidates;
CREATE TRIGGER candidates_touch_updated_at
  BEFORE UPDATE ON candidates
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- ---------------------------------------------------------------------------
-- 4. Rescate de los huerfanos que ya existen
-- ---------------------------------------------------------------------------
-- Los 8 candidatos en 'processing' desde hace 125 dias son invisibles para el
-- sistema: ningun claim los ve y el retry de la UI no los cubre. Se resetean una
-- sola vez, aca, para que entren a la cola nueva.
UPDATE candidates
   SET written_status   = CASE WHEN written_status = 'processing'
                              THEN 'pending' ELSE written_status END,
       video_status     = CASE WHEN video_status = 'processing'
                              THEN 'pending' ELSE video_status END,
       lease_expires_at = '1970-01-01T00:00:00Z',
       attempts         = 0,
       worker_id        = NULL,
       error_message    = NULL
 WHERE written_status = 'processing'
    OR video_status   = 'processing';

-- ---------------------------------------------------------------------------
-- 5. Verificacion (correr despues y revisar el resultado a ojo)
-- ---------------------------------------------------------------------------
-- No debe quedar ninguna fila en 'processing':
--
--   SELECT written_status, video_status, count(*)
--     FROM candidates GROUP BY 1, 2 ORDER BY 3 DESC;
--
-- Y las columnas nuevas deben existir:
--
--   SELECT column_name, data_type, is_nullable, column_default
--     FROM information_schema.columns
--    WHERE table_name = 'candidates'
--      AND column_name IN ('attempts','started_at','worker_id',
--                          'lease_expires_at','sheet_synced_at','updated_at')
--    ORDER BY column_name;
