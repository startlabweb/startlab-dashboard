-- Adds evaluator_type to monitors so the dashboard can support roles beyond
-- sales (e.g. editor / content manager forms with no video roleplay).
-- Run this in the Supabase SQL Editor after 001_initial_schema.sql.

ALTER TABLE monitors
    ADD COLUMN evaluator_type TEXT NOT NULL DEFAULT 'sales'
    CHECK (evaluator_type IN ('sales', 'editor'));

-- Editor monitors don't use the video columns, so they can be NULL.
ALTER TABLE monitors ALTER COLUMN video_score_column DROP DEFAULT;
ALTER TABLE monitors ALTER COLUMN video_explanation_column DROP DEFAULT;

CREATE INDEX idx_monitors_evaluator_type ON monitors(evaluator_type);
