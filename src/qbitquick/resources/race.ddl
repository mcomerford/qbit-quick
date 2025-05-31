CREATE TABLE IF NOT EXISTS racing_torrents
(
    racing_torrent_hash TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS paused_torrents
(
    racing_torrent_hash TEXT,
    paused_torrent_hash TEXT,
    FOREIGN KEY (racing_torrent_hash) REFERENCES racing_torrents (racing_torrent_hash) ON DELETE CASCADE,
    PRIMARY KEY (racing_torrent_hash, paused_torrent_hash)
);