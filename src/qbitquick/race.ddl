CREATE TABLE IF NOT EXISTS race
(
    racing_torrent_hash   TEXT PRIMARY KEY,
    paused_torrent_hashes TEXT,
    has_finished          INTEGER NOT NULL CHECK (has_finished IN (0, 1))
)