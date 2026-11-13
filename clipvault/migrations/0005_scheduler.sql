-- M3: failure streaks per scheduled step, notifications outbox, alerts sent to the phone.

ALTER TABLE runs ADD COLUMN fail_streak INTEGER NOT NULL DEFAULT 0;

ALTER TABLE alerts ADD COLUMN notified_at TEXT;

-- Phone notifications wait here until they are sent, so nothing is lost while the
-- computer is offline or during quiet hours.
CREATE TABLE notifications (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 5),
    tags_json TEXT NOT NULL DEFAULT '[]',
    click TEXT,
    actions_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    send_after TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'sent', 'dropped')),
    attempts INTEGER NOT NULL DEFAULT 0,
    sent_at TEXT,
    last_error TEXT
);

CREATE INDEX notifications_due ON notifications (status, send_after);
