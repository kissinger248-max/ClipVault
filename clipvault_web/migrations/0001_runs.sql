-- ClipVault Web (W1): one row per run (a video someone gave us), one per clip made from it.
-- Every file of a run lives in runs/<id>/ under the data folder; expiry deletes that folder
-- and marks the run 'expired' (only files the website made: hard rule 8).
CREATE TABLE runs (
    id TEXT PRIMARY KEY,               -- unguessable: it is the private link
    kind TEXT NOT NULL,                -- upload (W1); twitch links arrive in W3
    status TEXT NOT NULL,              -- queued | finding | making | done | failed | expired
    source_name TEXT,                  -- the uploaded file's name, shown back to its owner
    source_path TEXT,                  -- relative to the data folder; NULL once deleted
    duration REAL,
    width INTEGER,
    height INTEGER,
    name TEXT,                         -- optional: who is in the video (headlines)
    cam_rect TEXT,                     -- a steady face cam found in the video (JSON), for split
    visitor TEXT,                      -- salted hash of the address (limits, W4)
    step TEXT,                         -- what is happening now, in words
    step_done INTEGER NOT NULL DEFAULT 0,
    step_total INTEGER NOT NULL DEFAULT 0,
    more_found INTEGER NOT NULL DEFAULT 0,  -- moments found beyond the ones made
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    expires_at TEXT NOT NULL
);
CREATE INDEX runs_status ON runs (status, created_at);
CREATE INDEX runs_expires ON runs (expires_at);

CREATE TABLE run_clips (
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    n INTEGER NOT NULL,                -- 1 = the best moment
    start REAL NOT NULL,               -- seconds into the source
    end REAL NOT NULL,
    score REAL NOT NULL,
    reasons TEXT,                      -- why it was picked (for tuning)
    text TEXT,                         -- what is said
    words_json TEXT,                   -- word timings, from start
    language TEXT,
    headline TEXT,
    key_word TEXT,
    headline_options TEXT,             -- JSON list of other headlines that fit
    layout TEXT,
    status TEXT NOT NULL,              -- waiting | making | done | failed
    path TEXT,                         -- the phone version, relative to the data folder
    thumb_path TEXT,
    qa_passed INTEGER,
    qa_report_json TEXT,
    seconds REAL,
    error TEXT,
    PRIMARY KEY (run_id, n)
);
