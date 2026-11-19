-- Hook emoji: the person's own choice for a clip (NULL = pick automatically, '' = none, else the emoji),
-- and which emoji each phone version was drawn with.
ALTER TABLE clips ADD COLUMN hook_emoji TEXT;
ALTER TABLE renders ADD COLUMN hook_emoji TEXT;
