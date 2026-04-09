-- StartLab Dashboard Schema
-- Run this in Supabase SQL Editor

-- Monitors
CREATE TABLE monitors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sheet_id TEXT NOT NULL,
    sheet_url TEXT NOT NULL,
    sheet_name TEXT DEFAULT 'Form Responses 1',
    sheet_title TEXT,
    video_column TEXT,
    written_score_column TEXT DEFAULT 'Puntaje Preguntas',
    written_explanation_column TEXT DEFAULT 'Explicación',
    video_score_column TEXT DEFAULT 'Puntaje Roleplay',
    video_explanation_column TEXT DEFAULT 'Explicación',
    status TEXT DEFAULT 'paused' CHECK (status IN ('active', 'paused', 'error')),
    last_poll_at TIMESTAMPTZ,
    last_row_count INTEGER DEFAULT 0,
    total_cost_usd NUMERIC(8,4) DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Criteria (1 per type per monitor)
CREATE TABLE criteria (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    monitor_id UUID REFERENCES monitors(id) ON DELETE CASCADE,
    criteria_type TEXT NOT NULL CHECK (criteria_type IN ('written', 'video')),
    raw_text TEXT NOT NULL,
    parsed_criteria JSONB NOT NULL,
    total_points INTEGER NOT NULL,
    gpt_prompt_template TEXT NOT NULL,
    confirmed BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Candidates
CREATE TABLE candidates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    monitor_id UUID REFERENCES monitors(id) ON DELETE CASCADE,
    sheet_row INTEGER NOT NULL,
    name TEXT,
    email TEXT,
    video_url TEXT,
    video_source TEXT CHECK (video_source IN ('google_drive', 'loom', 'none')),
    written_answers JSONB,
    -- Written evaluation
    written_status TEXT DEFAULT 'pending' CHECK (written_status IN ('pending', 'processing', 'completed', 'error')),
    written_score NUMERIC,
    written_breakdown JSONB,
    written_explanation TEXT,
    -- Video evaluation
    video_status TEXT DEFAULT 'pending' CHECK (video_status IN ('pending', 'processing', 'completed', 'error', 'no_video')),
    video_score NUMERIC,
    video_breakdown JSONB,
    video_explanation TEXT,
    transcript TEXT,
    -- General
    error_message TEXT,
    cost_usd NUMERIC(6,4) DEFAULT 0,
    processed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(monitor_id, sheet_row)
);

-- Activity log
CREATE TABLE activity_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    monitor_id UUID REFERENCES monitors(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes
CREATE INDEX idx_candidates_monitor_status ON candidates(monitor_id, written_status, video_status);
CREATE INDEX idx_candidates_monitor_row ON candidates(monitor_id, sheet_row);
CREATE INDEX idx_activity_monitor ON activity_log(monitor_id, created_at DESC);
CREATE INDEX idx_criteria_monitor ON criteria(monitor_id, criteria_type);
