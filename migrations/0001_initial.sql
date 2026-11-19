-- 0001: initial schema for the Scotland Emergency Packet Radio BBS.
-- Applied by bbs-core in one transaction; recorded in schema_migrations.
-- All timestamps are UTC ISO-8601 text ('YYYY-MM-DDTHH:MM:SSZ').

CREATE TABLE users (
    id                INTEGER PRIMARY KEY,
    username          TEXT NOT NULL UNIQUE COLLATE NOCASE,
    callsign          TEXT COLLATE NOCASE,               -- a claim until callsign_verified = 1
    callsign_verified INTEGER NOT NULL DEFAULT 0 CHECK (callsign_verified IN (0, 1)),
    display_name      TEXT,
    contact           TEXT,                              -- private: admins only
    role              TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
    status            TEXT NOT NULL CHECK (status IN ('PENDING', 'ACTIVE', 'REJECTED', 'SUSPENDED')),
    status_reason     TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    approved_by       INTEGER REFERENCES users(id),
    approved_at       TEXT
);
-- A callsign can belong to one live account (rejected requests don't block it).
CREATE UNIQUE INDEX users_callsign_live ON users(callsign)
    WHERE callsign IS NOT NULL AND status IN ('PENDING', 'ACTIVE', 'SUSPENDED');
CREATE INDEX users_status ON users(status);

CREATE TABLE ssh_keys (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key_type     TEXT NOT NULL,
    key_data     TEXT NOT NULL,                          -- base64 public key blob
    fingerprint  TEXT NOT NULL UNIQUE,                   -- SHA256:...; a key maps to one account, ever
    comment      TEXT,
    created_at   TEXT NOT NULL,
    revoked_at   TEXT,
    revoked_by   INTEGER REFERENCES users(id),
    last_used_at TEXT
);
CREATE INDEX ssh_keys_user ON ssh_keys(user_id);

CREATE TABLE registrations (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    requested_at TEXT NOT NULL,
    client_ip   TEXT,
    transport   TEXT NOT NULL,
    decision    TEXT CHECK (decision IN ('APPROVED', 'REJECTED')),
    decided_by  INTEGER REFERENCES users(id),
    decided_at  TEXT,
    reason      TEXT
);
CREATE INDEX registrations_pending ON registrations(decision, requested_at);

CREATE TABLE sessions (
    id           INTEGER PRIMARY KEY,
    token_hash   TEXT NOT NULL UNIQUE,                   -- sha256 of the bearer token; token itself is never stored
    user_id      INTEGER REFERENCES users(id) ON DELETE CASCADE,   -- NULL = guest
    kind         TEXT NOT NULL CHECK (kind IN ('guest', 'user', 'admin')),
    service      TEXT NOT NULL CHECK (service IN ('ssh', 'admin', 'radio')),
    transport    TEXT NOT NULL,
    client_ip    TEXT,
    key_id       INTEGER REFERENCES ssh_keys(id),
    created_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    ended_at     TEXT
);
CREATE INDEX sessions_live ON sessions(ended_at, last_seen_at);

CREATE TABLE boards (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description  TEXT NOT NULL DEFAULT '',
    post_policy  TEXT NOT NULL DEFAULT 'users' CHECK (post_policy IN ('users', 'admins')),
    is_public    INTEGER NOT NULL DEFAULT 1 CHECK (is_public IN (0, 1)),
    is_emergency INTEGER NOT NULL DEFAULT 0 CHECK (is_emergency IN (0, 1)),
    archived     INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    sort_order   INTEGER NOT NULL DEFAULT 100,
    created_at   TEXT NOT NULL
);

-- Bulletins. id is the local message number (AUTOINCREMENT: numbers are never reused).
-- mid is the global identifier and stays the same when a message is forwarded.
CREATE TABLE messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    mid               TEXT NOT NULL UNIQUE,
    origin_node       TEXT NOT NULL,
    board_id          INTEGER NOT NULL REFERENCES boards(id),
    author_id         INTEGER REFERENCES users(id),     -- NULL for messages from other nodes
    author_name       TEXT NOT NULL COLLATE NOCASE,
    author_callsign   TEXT COLLATE NOCASE,
    subject           TEXT NOT NULL,
    body              TEXT NOT NULL,
    parent_mid        TEXT,
    priority          TEXT NOT NULL DEFAULT 'ROUTINE' CHECK (priority IN ('ROUTINE', 'PRIORITY', 'EMERGENCY')),
    area              TEXT,
    expires_at        TEXT,
    status            TEXT NOT NULL DEFAULT 'VISIBLE' CHECK (status IN ('VISIBLE', 'HIDDEN', 'DELETED')),
    moderated_by      INTEGER REFERENCES users(id),
    moderated_at      TEXT,
    moderation_reason TEXT,
    content_hash      TEXT NOT NULL,
    hop_count         INTEGER NOT NULL DEFAULT 0,
    route             TEXT NOT NULL DEFAULT '[]',         -- JSON list of node ids it passed through
    created_at        TEXT NOT NULL,
    received_at       TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX messages_board ON messages(board_id, status, id);
CREATE INDEX messages_author ON messages(author_name);
CREATE INDEX messages_callsign ON messages(author_callsign);
CREATE INDEX messages_hash ON messages(board_id, content_hash, created_at);
CREATE INDEX messages_priority ON messages(board_id, priority, id);

-- Full-text index over subjects and bodies, kept in step by triggers.
CREATE VIRTUAL TABLE messages_fts USING fts5(subject, body, content='messages', content_rowid='id');
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, subject, body) VALUES (new.id, new.subject, new.body);
END;
CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, subject, body) VALUES ('delete', old.id, old.subject, old.body);
END;
CREATE TRIGGER messages_au AFTER UPDATE OF subject, body ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, subject, body) VALUES ('delete', old.id, old.subject, old.body);
    INSERT INTO messages_fts(rowid, subject, body) VALUES (new.id, new.subject, new.body);
END;

CREATE TABLE message_reads (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    read_at    TEXT NOT NULL,
    PRIMARY KEY (user_id, message_id)
) WITHOUT ROWID;

CREATE TABLE private_messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    mid               TEXT NOT NULL UNIQUE,
    origin_node       TEXT NOT NULL,
    sender_id         INTEGER REFERENCES users(id),
    sender_name       TEXT NOT NULL,
    recipient_id      INTEGER REFERENCES users(id),
    recipient_address TEXT NOT NULL,                     -- username, or user@NODE for remote (Phase 2)
    subject           TEXT NOT NULL,
    body              TEXT NOT NULL,
    parent_id         INTEGER REFERENCES private_messages(id),
    delivery_state    TEXT NOT NULL CHECK (delivery_state IN ('DELIVERED', 'QUEUED', 'FAILED')),
    created_at        TEXT NOT NULL,
    delivered_at      TEXT,
    read_at           TEXT,
    sender_state      TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (sender_state IN ('ACTIVE', 'ARCHIVED', 'DELETED')),
    recipient_state   TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (recipient_state IN ('ACTIVE', 'ARCHIVED', 'DELETED'))
);
CREATE INDEX pm_inbox ON private_messages(recipient_id, recipient_state, id);
CREATE INDEX pm_sent ON private_messages(sender_id, sender_state, id);

CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    actor_id    INTEGER,
    actor_name  TEXT,
    service     TEXT,
    action      TEXT NOT NULL,
    target_type TEXT,
    target_id   TEXT,
    outcome     TEXT NOT NULL CHECK (outcome IN ('success', 'denied', 'error')),
    detail      TEXT,                                    -- JSON; never message bodies, keys or tokens
    client_ip   TEXT
);
CREATE INDEX audit_ts ON audit_log(ts);

-- Nodes known to this BBS (the local node plus configured peers).
CREATE TABLE nodes (
    node_id      TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    is_local     INTEGER NOT NULL DEFAULT 0,
    address      TEXT,
    enabled      INTEGER NOT NULL DEFAULT 0,
    last_seen_at TEXT,
    last_sync_at TEXT,
    status       TEXT NOT NULL DEFAULT 'NEVER_CONNECTED'
);

-- Outbound store-and-forward queue (filled by Phase 2 bbs-sync; empty in Phase 1).
CREATE TABLE forward_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mid             TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('bulletin', 'private')),
    peer_node       TEXT NOT NULL,
    priority        TEXT NOT NULL DEFAULT 'ROUTINE',
    state           TEXT NOT NULL DEFAULT 'QUEUED' CHECK (state IN ('QUEUED', 'SENDING', 'ACKED', 'FAILED', 'EXPIRED')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (mid, peer_node)
);
CREATE INDEX forward_queue_state ON forward_queue(state, next_attempt_at);
