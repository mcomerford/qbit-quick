CREATE TABLE IF NOT EXISTS race
(
    id                    INTEGER PRIMARY KEY,
    racing_torrent_hash   VARCHAR(32) NOT NULL,
    paused_torrent_hashes TEXT        NULL,
    has_finished          INTEGER     NOT NULL
)