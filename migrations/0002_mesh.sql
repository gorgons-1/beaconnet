-- 0002: node-to-node mesh (bbs-sync).
-- forward_queue was always empty before this release, so it is rebuilt with the
-- envelope stored on each row and a 'receipt' kind for remote-mail delivery reports.

ALTER TABLE boards ADD COLUMN forward INTEGER NOT NULL DEFAULT 1 CHECK (forward IN (0, 1));

DROP TABLE forward_queue;
CREATE TABLE forward_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mid             TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('bulletin', 'private', 'receipt')),
    peer_node       TEXT NOT NULL,
    priority        TEXT NOT NULL DEFAULT 'ROUTINE' CHECK (priority IN ('ROUTINE', 'PRIORITY', 'EMERGENCY')),
    envelope        TEXT NOT NULL,                       -- JSON, exactly as it will be sent
    expires_at      TEXT,
    state           TEXT NOT NULL DEFAULT 'QUEUED' CHECK (state IN ('QUEUED', 'ACKED', 'FAILED', 'EXPIRED')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (mid, peer_node)
);
CREATE INDEX forward_queue_peer ON forward_queue(peer_node, state, next_attempt_at);
CREATE INDEX forward_queue_state ON forward_queue(state, updated_at);

-- Every envelope id this node has accepted (bulletins, relayed mail, receipts), so a
-- message that comes back round a loop or from a second peer is recognised and dropped.
CREATE TABLE sync_seen (
    mid       TEXT PRIMARY KEY,
    kind      TEXT NOT NULL,
    from_node TEXT,
    seen_at   TEXT NOT NULL
) WITHOUT ROWID;
CREATE INDEX sync_seen_at ON sync_seen(seen_at);

ALTER TABLE nodes ADD COLUMN public_key TEXT;
ALTER TABLE nodes ADD COLUMN last_error TEXT;
