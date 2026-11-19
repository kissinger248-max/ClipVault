-- TikTok desktop sign-in uses PKCE: the one-time code verifier is kept with the sign-in's state
-- until the callback arrives, then deleted with it.
ALTER TABLE oauth_states ADD COLUMN code_verifier TEXT;
