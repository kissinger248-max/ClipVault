-- W3: Twitch links, streamer search, sign-in, opt-outs, takedowns and the download switch.
ALTER TABLE runs ADD COLUMN source_url TEXT;        -- the link that was pasted
ALTER TABLE runs ADD COLUMN twitch_user_id TEXT;    -- the channel the clips come from
ALTER TABLE runs ADD COLUMN twitch_login TEXT;
ALTER TABLE runs ADD COLUMN twitch_name TEXT;
ALTER TABLE runs ADD COLUMN is_live INTEGER NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN embed_only INTEGER NOT NULL DEFAULT 0;  -- downloads were off: Twitch's own players
ALTER TABLE runs ADD COLUMN median_views REAL;     -- the channel's usual clip ("{views} people watched THIS")

ALTER TABLE run_clips ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'upload';  -- upload | clip | section
ALTER TABLE run_clips ADD COLUMN source_url TEXT;
ALTER TABLE run_clips ADD COLUMN source_path TEXT;  -- this clip's own download (Twitch), relative
ALTER TABLE run_clips ADD COLUMN clip_slug TEXT;
ALTER TABLE run_clips ADD COLUMN title TEXT;
ALTER TABLE run_clips ADD COLUMN views INTEGER;
ALTER TABLE run_clips ADD COLUMN vod_id TEXT;
ALTER TABLE run_clips ADD COLUMN embed_url TEXT;
ALTER TABLE run_clips ADD COLUMN vod_start REAL;   -- a moment cut from a VOD: where, in the VOD
ALTER TABLE run_clips ADD COLUMN vod_end REAL;

-- A streamer's own sign-in, for "My channel" (official downloads) and opting out.
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,               -- random, in an HttpOnly cookie
    twitch_user_id TEXT NOT NULL,
    login TEXT NOT NULL,
    display_name TEXT,
    access_token TEXT,                 -- only for "My channel" (channel:manage:clips); NULL otherwise
    token_expires_at TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- The token a "My channel" run downloads with; deleted when the run ends.
CREATE TABLE grants (
    run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    editor_id TEXT NOT NULL,
    access_token TEXT NOT NULL
);

-- Streamers who asked not to be clipped here. Kept forever (until they allow it again).
CREATE TABLE optouts (
    twitch_user_id TEXT PRIMARY KEY,
    login TEXT NOT NULL,
    created_at TEXT NOT NULL,
    how TEXT NOT NULL                  -- signed-in | takedown
);

-- Problem reports and takedown requests, answered by a person.
CREATE TABLE reports (
    id INTEGER PRIMARY KEY,
    run_id TEXT,
    reason TEXT NOT NULL,              -- copyright | my-content | abuse | other
    details TEXT,
    contact TEXT,
    created_at TEXT NOT NULL,
    handled_at TEXT,
    outcome TEXT
);

-- Visitors who keep breaking the rules (salted address hashes).
CREATE TABLE blocked (
    visitor TEXT PRIMARY KEY,
    reason TEXT,
    created_at TEXT NOT NULL
);

-- Server settings changed while running (the download switch, the hashing salt).
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
