-- Support for discovery (M1).

-- Twitch clips carry a game id, not a name. Keeping the id lets a missing name be filled in later.
ALTER TABLE clips ADD COLUMN game_id TEXT;

-- When the streamer's Twitch profile (name, avatar) was last refreshed.
ALTER TABLE streamers ADD COLUMN profile_checked_at TEXT;

-- Cache of game id -> name.
CREATE TABLE games (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Keep clips.updated_at honest without every UPDATE having to remember it.
CREATE TRIGGER clips_touch_updated_at
AFTER UPDATE ON clips
FOR EACH ROW
WHEN NEW.updated_at IS OLD.updated_at
BEGIN
    UPDATE clips SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = NEW.id;
END;
