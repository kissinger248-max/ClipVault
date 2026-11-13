# Groq fixtures

`transcription_verbose.json` follows the shape described in Groq's speech-to-text
docs (checked 2026-11-12) for `response_format=verbose_json` with
`timestamp_granularities[]=word`, but the values are made up: no Groq key was
available when it was written. The docs show a segment object but no complete
example with words, so the parser accepts words at the top level or inside
segments. Replace this file with a real recorded response once a key exists and
note any difference in docs/PROGRESS.md.
