-- Tables from SPEC §2.

CREATE TABLE streamers (
    id INTEGER PRIMARY KEY,
    login TEXT NOT NULL UNIQUE,
    twitch_user_id TEXT,
    display_name TEXT,
    avatar_url TEXT,
    save_enabled INTEGER NOT NULL DEFAULT 1,
    post_enabled INTEGER NOT NULL DEFAULT 0,
    permission_type TEXT NOT NULL DEFAULT 'none'
        CHECK (permission_type IN ('none', 'campaign', 'written')),
    permission_ref TEXT,
    twitch_authorized INTEGER NOT NULL DEFAULT 0,
    top_n INTEGER NOT NULL DEFAULT 5,
    min_views INTEGER,
    vod_moments INTEGER NOT NULL DEFAULT 3,
    layout_override TEXT,
    cam_rect TEXT,
    content_rect TEXT,
    layout_checked_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    -- Hard rule 1, enforced by the database as a second line of defense:
    -- posting can only be switched on when permission is recorded.
    CONSTRAINT posting_needs_permission CHECK (
        post_enabled = 0
        OR (
            permission_type IN ('campaign', 'written')
            AND permission_ref IS NOT NULL
            AND trim(permission_ref) <> ''
        )
    )
);

CREATE TABLE clips (
    id TEXT PRIMARY KEY,            -- Twitch clip id, or 'vod:<vod_id>:<offset_s>' for VOD cuts
    streamer_id INTEGER NOT NULL REFERENCES streamers (id),
    source TEXT NOT NULL CHECK (source IN ('viewer', 'auto', 'vod_spike', 'live_spike')),
    title TEXT,
    created_at TEXT,
    view_count INTEGER,
    duration REAL,
    vod_id TEXT,
    vod_offset INTEGER,
    game_name TEXT,
    creator_name TEXT,
    url TEXT,
    state TEXT NOT NULL DEFAULT 'found',
    reject_reason TEXT,
    fail_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    file_path TEXT,
    portrait_path TEXT,
    sidecar_path TEXT,
    transcript_path TEXT,
    music_score REAL,
    speech_wps REAL,
    score REAL,
    starred INTEGER NOT NULL DEFAULT 0,
    downloaded_via TEXT CHECK (downloaded_via IN ('official', 'unofficial')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX clips_state ON clips (state);
CREATE INDEX clips_streamer_created ON clips (streamer_id, created_at);
CREATE INDEX clips_vod ON clips (vod_id);

CREATE TABLE renders (
    id INTEGER PRIMARY KEY,
    clip_id TEXT NOT NULL REFERENCES clips (id),
    layout TEXT NOT NULL,
    path TEXT,
    headline TEXT,
    post_caption TEXT,
    hashtags_json TEXT,
    qa_passed INTEGER,
    qa_report_json TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX renders_clip ON renders (clip_id);

CREATE TABLE posts (
    id INTEGER PRIMARY KEY,
    render_id INTEGER NOT NULL REFERENCES renders (id),
    platform TEXT NOT NULL,
    account TEXT NOT NULL,
    scheduled_at TEXT,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'approved', 'posted', 'failed', 'skipped')),
    service_post_id TEXT,
    post_url TEXT,
    posted_at TEXT,
    views_1h INTEGER,
    views_24h INTEGER,
    views_7d INTEGER,
    campaign_submitted_at TEXT,
    campaign_submission_id TEXT,
    -- One post per render per account: a retry can never create a duplicate.
    UNIQUE (render_id, platform, account)
);

CREATE INDEX posts_status_scheduled ON posts (status, scheduled_at);

CREATE TABLE runs (
    job TEXT PRIMARY KEY,
    last_started TEXT,
    last_success TEXT,
    cursor TEXT,
    last_error TEXT
);

CREATE TABLE alerts (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    fix_url TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    resolved_at TEXT
);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT               -- non-secret settings only
);
