-- M4: past broadcasts (VODs) whose chat is searched for moments nobody clipped.

CREATE TABLE vods (
    id TEXT PRIMARY KEY,                 -- Twitch video id
    streamer_id INTEGER NOT NULL REFERENCES streamers (id),
    stream_id TEXT,
    title TEXT,
    url TEXT NOT NULL,
    created_at TEXT NOT NULL,            -- when the broadcast started
    duration_s INTEGER,
    -- found -> done (moments saved as clips) or skipped (no chat, sub-only, gone...)
    state TEXT NOT NULL DEFAULT 'found' CHECK (state IN ('found', 'done', 'skipped')),
    skip_reason TEXT,
    chat_path TEXT,                      -- relative to the library root
    messages INTEGER,
    spikes INTEGER,
    moments INTEGER,
    fail_count INTEGER NOT NULL DEFAULT 0,
    retry_at TEXT,
    last_error TEXT,
    processed_at TEXT,
    found_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX vods_state ON vods (state, created_at);
CREATE INDEX vods_streamer ON vods (streamer_id, created_at);
