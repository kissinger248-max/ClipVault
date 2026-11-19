-- W2: when each clip's current step started (the cards' progress bars), and counted events.
ALTER TABLE run_clips ADD COLUMN started_at REAL;  -- epoch seconds

-- Cookie-free counting: one number per day, event and detail. Nothing about who did it.
CREATE TABLE events (
    day TEXT NOT NULL,            -- YYYY-MM-DD (UTC)
    name TEXT NOT NULL,           -- page_view | run_started | run_finished | clip_downloaded | ...
    detail TEXT NOT NULL DEFAULT '',
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, name, detail)
);
