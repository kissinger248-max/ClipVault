# Twitch fixtures

Recorded from the real Twitch API on 2026-11-12 with
`python scripts/twitch_live_check.py valkyrae xqc kaicenat --dump DIR`.

- `users.json`: Get Users response for the three test streamers, unchanged.
- `clips.json`: three clips from one Get Clips response (the most viewed, and two
  whose VOD was already gone, so `video_id` is `""` and `vod_offset` is `null`).
  The clip creators' ids and names were replaced with `viewer_a` etc.; everything
  else is exactly as Twitch returned it.
- `games.json`: Get Games response for the game on those clips, unchanged.
- `token.json`: made up. A real access token must never be stored in the repository.
