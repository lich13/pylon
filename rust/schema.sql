PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, profile TEXT NOT NULL, job_id TEXT NOT NULL, kind TEXT NOT NULL,
 payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 receipt TEXT, error TEXT, module TEXT NOT NULL DEFAULT 'legacy'
);
CREATE INDEX IF NOT EXISTS ready_events_v2 ON events(profile,status,next_attempt,created_at);
CREATE INDEX IF NOT EXISTS events_receipt_last ON events(profile,json_extract(receipt,'$[#-1].message_id')) WHERE receipt IS NOT NULL;
CREATE INDEX IF NOT EXISTS events_receipt_one ON events(profile,json_extract(receipt,'$.message_id')) WHERE receipt IS NOT NULL;
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS updates (
 profile TEXT NOT NULL, update_id INTEGER NOT NULL, payload TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', error TEXT, kind TEXT NOT NULL DEFAULT 'message',
 chat_id TEXT, message_id INTEGER, PRIMARY KEY(profile,update_id)
);
CREATE TABLE IF NOT EXISTS runs (
 id TEXT PRIMARY KEY,job_id TEXT NOT NULL,started_at REAL NOT NULL,finished_at REAL,status TEXT NOT NULL,detail TEXT
);
CREATE INDEX IF NOT EXISTS updates_pending ON updates(profile,update_id) WHERE status='pending';
CREATE INDEX IF NOT EXISTS runs_job_started ON runs(job_id,started_at DESC);
CREATE INDEX IF NOT EXISTS runs_status_started ON runs(status,started_at DESC);
CREATE TABLE IF NOT EXISTS run_events (run_id TEXT NOT NULL,event_id TEXT NOT NULL,PRIMARY KEY(run_id,event_id));
CREATE TABLE IF NOT EXISTS contexts (
 hash TEXT PRIMARY KEY,module TEXT NOT NULL,bot TEXT NOT NULL,capabilities TEXT NOT NULL,expires_at REAL NOT NULL,
 origin TEXT NOT NULL,workspace TEXT NOT NULL
);
