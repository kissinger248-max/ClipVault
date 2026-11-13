-- Phone versions are made automatically for every new clip; a clip that can't be
-- rendered is remembered here so it isn't tried again on every round.
ALTER TABLE clips ADD COLUMN render_error TEXT;
