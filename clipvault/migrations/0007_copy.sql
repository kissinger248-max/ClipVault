-- M8: the headline, post caption and hashtags written for a clip (SPEC §9).
-- Kept on the clip so every render and every post of it uses the same words.

ALTER TABLE clips ADD COLUMN headline TEXT;
ALTER TABLE clips ADD COLUMN post_caption TEXT;
ALTER TABLE clips ADD COLUMN hashtags_json TEXT;
ALTER TABLE clips ADD COLUMN copy_provider TEXT;
