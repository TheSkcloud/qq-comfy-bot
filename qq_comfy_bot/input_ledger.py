"""Durable input receipt ledger; ambiguous interrupted work is never auto-replayed."""
from contextlib import contextmanager
import hashlib
import json
import sqlite3
import time
import uuid

class InputLedger:
    def __init__(self, path):
        self.path = path
        with self.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS inputs (id TEXT PRIMARY KEY, scope TEXT, payload TEXT, status TEXT, reason TEXT, updated REAL)')
            db.execute("UPDATE inputs SET status='outcome_unknown', reason='PROCESS_INTERRUPTED' WHERE status IN ('queued','processing')")
            db.execute('CREATE TABLE IF NOT EXISTS revisions (scope TEXT PRIMARY KEY, version INTEGER NOT NULL)')
            db.commit()

    def next_version(self, scope):
        with self.connection() as db:
            db.execute('INSERT INTO revisions VALUES (?,1) ON CONFLICT(scope) DO UPDATE SET version=version+1', (scope,))
            return db.execute('SELECT version FROM revisions WHERE scope=?', (scope,)).fetchone()[0]

    def receive(self, scope, event, text):
        message_id = event.get('message_id')
        identifier = hashlib.sha256(f'{scope}:{message_id}'.encode()).hexdigest() if message_id is not None else uuid.uuid4().hex
        payload = json.dumps({'text': text, 'message_id': message_id}, ensure_ascii=False)
        with self.connection() as db:
            cursor = db.execute('INSERT OR IGNORE INTO inputs VALUES (?,?,?,?,?,?)',
                                     (identifier, scope, payload, 'received', 'INPUT_RECEIVED', time.time()))
        return identifier if cursor.rowcount else None

    def transition(self, identifier, status, reason):
        with self.connection() as db:
            db.execute('UPDATE inputs SET status=?,reason=?,updated=? WHERE id=?', (status, reason, time.time(), identifier))

    def diagnostics(self, limit=100):
        # No original message or user identifiers in diagnostic exports.
        with self.connection() as db:
            return [dict(zip(('input_id','status','reason','updated'), row)) for row in
                db.execute('SELECT id,status,reason,updated FROM inputs ORDER BY updated DESC LIMIT ?', (min(500,max(1,limit)),))]

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()
