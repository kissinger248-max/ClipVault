-- Auto-upload: connected accounts, routing, captions, the upload queue and its log.
-- Only adds tables; nothing existing is touched.
CREATE TABLE social_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL, label TEXT NOT NULL, handle TEXT, external_id TEXT,
    status TEXT NOT NULL DEFAULT 'connected', account_type TEXT, error TEXT, scopes TEXT,
    token_expires_at REAL, created_at REAL NOT NULL
);
CREATE TABLE oauth_states (state TEXT PRIMARY KEY, platform TEXT NOT NULL, label TEXT, created_at REAL NOT NULL);
CREATE TABLE routing_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT, streamer TEXT NOT NULL DEFAULT '*', clip_type TEXT NOT NULL DEFAULT '*',
    account_id INTEGER NOT NULL
);
CREATE TABLE posting_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE clip_captions (clip_id TEXT PRIMARY KEY, data TEXT NOT NULL, facts TEXT, created_at REAL NOT NULL);
CREATE TABLE upload_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, clip_id TEXT NOT NULL, account_id INTEGER NOT NULL, platform TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued', not_before REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
    result_url TEXT, reason TEXT, caption TEXT, dry_run INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE (clip_id, platform, account_id)
);
CREATE TABLE upload_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, platform TEXT, account_id INTEGER, clip_id TEXT,
    result TEXT, url TEXT, detail TEXT
);
