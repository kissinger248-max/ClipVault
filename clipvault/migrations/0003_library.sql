-- Support for downloads and the library folder (M2).

-- When a failed step may be tried again (NULL = any time).
ALTER TABLE clips ADD COLUMN retry_at TEXT;

-- When the clip's file was downloaded, and how big it is in bytes.
ALTER TABLE clips ADD COLUMN fetched_at TEXT;
ALTER TABLE clips ADD COLUMN file_size INTEGER;

-- Set when the clip's files left the library: 'retention' (cleanup deleted them),
-- 'missing' (someone deleted them by hand) or 'user' (deleted from the control panel).
-- The clip row stays, so the clip is never downloaded again and keeps its history.
-- Every worker step must skip clips where removed_at is set.
ALTER TABLE clips ADD COLUMN removed_at TEXT;
ALTER TABLE clips ADD COLUMN removed_reason TEXT
    CHECK (removed_reason IS NULL OR removed_reason IN ('retention', 'missing', 'user'));

-- file_path, sidecar_path, transcript_path and portrait_path hold paths RELATIVE to
-- the library root, with forward slashes. That keeps the database valid when the
-- library moves to another drive or is synced to another computer.

CREATE INDEX clips_retry ON clips (state, retry_at);

-- At most one open alert of each kind + message.
CREATE UNIQUE INDEX alerts_open_unique ON alerts (kind, message) WHERE resolved_at IS NULL;
