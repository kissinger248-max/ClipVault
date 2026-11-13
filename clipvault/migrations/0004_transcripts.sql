-- Support for transcription and transcript search (M6).

ALTER TABLE clips ADD COLUMN language TEXT;
ALTER TABLE clips ADD COLUMN transcript_provider TEXT;   -- groq | local

-- Full-text search over what was said. One row per clip; text is stored as spoken
-- (masking applies to captions only).
CREATE VIRTUAL TABLE transcripts USING fts5(
    clip_id UNINDEXED,
    text,
    tokenize = 'unicode61 remove_diacritics 2'
);
