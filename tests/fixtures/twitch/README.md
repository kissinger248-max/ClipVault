# Twitch fixtures

These files follow the response shapes in the Twitch API reference (checked
2026-11-12) but the values are made up: no Twitch keys were available when they
were written. Replace them with real recorded responses by running
`python scripts/twitch_live_check.py LOGIN --dump tests/fixtures/twitch/recorded`
and note any field differences in docs/PROGRESS.md.
