-- How much busier chat was than its usual level when a chat moment was found
-- (score / mean of the 10 minutes before). Shown on the clip; NULL for viewer clips.
ALTER TABLE clips ADD COLUMN surge REAL;
