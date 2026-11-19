-- Hook headlines (process/headlines.py): the word shown in the accent colour, where the
-- headline came from ("kind|pattern": ai, title, reaction:shock:0, moment:2 ...; custom =
-- typed by the user) and the version of the system that wrote it. Phone versions whose
-- headline an older version wrote are made again.
ALTER TABLE clips ADD COLUMN headline_key TEXT;
ALTER TABLE clips ADD COLUMN headline_source TEXT;
ALTER TABLE clips ADD COLUMN headline_version INTEGER;
