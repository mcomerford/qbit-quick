CREATE TABLE IF NOT EXISTS pause_events
(
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS paused_torrents
(
    id TEXT NOT NULL,
    torrent_hash TEXT NOT NULL,
    FOREIGN KEY (id) REFERENCES pause_events (id) ON DELETE CASCADE,
    PRIMARY KEY (id, torrent_hash)
);