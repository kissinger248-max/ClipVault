-- Finished phone versions in the "Edited clips" folders (library/edited.py): one row per
-- file (a hard link, or a copy on drives without hard links), so making the clip again,
-- deleting it or retention removes it too (hard rule 8).
CREATE TABLE edited_files (
    path TEXT PRIMARY KEY,  -- relative to the library root
    clip_id TEXT NOT NULL REFERENCES clips(id) ON DELETE CASCADE
);
CREATE INDEX edited_files_clip ON edited_files (clip_id);
