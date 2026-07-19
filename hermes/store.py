import os
import json
import time
import sqlite3
import datetime
import threading
import logging

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    lead_key TEXT PRIMARY KEY,
    peer_id INTEGER,
    account TEXT,
    status TEXT NOT NULL,
    date TEXT,
    timestamp REAL,
    reply_count INTEGER NOT NULL DEFAULT 0,
    last_reply_date TEXT,
    last_stage TEXT,
    last_action TEXT,
    manager_notified_at REAL,
    stop_reason TEXT
);
CREATE TABLE IF NOT EXISTS queue (
    lead_key TEXT PRIMARY KEY,
    context TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    assigned_account TEXT,
    last_error TEXT,
    enqueued_at REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_key TEXT NOT NULL,
    account TEXT,
    direction TEXT NOT NULL,
    text TEXT,
    meta TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transcripts_lead ON transcripts(lead_key, created_at);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task TEXT,
    provider TEXT,
    model TEXT,
    ok INTEGER,
    latency_ms INTEGER,
    error TEXT,
    lead_key TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS account_state (
    account TEXT PRIMARY KEY,
    cooldown_until REAL NOT NULL DEFAULT 0,
    cooldown_reason TEXT,
    healthy INTEGER NOT NULL DEFAULT 1,
    last_error TEXT,
    last_dispatch_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS account_control (
    account TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    daily_limit INTEGER
);
CREATE TABLE IF NOT EXISTS admin_notifications (
    lead_key TEXT PRIMARY KEY,
    account TEXT,
    recipient TEXT,
    message TEXT NOT NULL,
    target_status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

COUNTED_DAILY_STATUSES_EXCLUDED = ("manual_required", "skipped")


def _today():
    return str(datetime.date.today())


def _lead_key(value):
    return str(value).strip().lower()


class Store:
    """SQLite-backed state: durable lead queue, dialog transcripts, per-account state.

    Writes are tiny and rare (tens per day), so synchronous sqlite guarded by a
    threading.Lock is deliberately used instead of an async driver. The lock makes
    the store safe both from the event loop and from asyncio.to_thread workers.
    """

    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    # --- migration -------------------------------------------------------

    def migrate_legacy_json(self, json_path, account_names):
        if not json_path or not os.path.exists(json_path):
            return False
        with self._lock:
            done = self._conn.execute(
                "SELECT value FROM meta WHERE key='migrated_from_json'"
            ).fetchone()
            if done:
                return False
            try:
                with open(json_path) as f:
                    legacy = json.load(f)
            except Exception as e:
                logger.error(f"Cannot read legacy DB {json_path}: {e}")
                return False

            contacted = legacy.get("contacted") or {}
            for lead_key, data in contacted.items():
                lead_key = _lead_key(lead_key)
                self._conn.execute(
                    """INSERT OR IGNORE INTO leads
                       (lead_key, account, status, date, timestamp, reply_count,
                        last_reply_date, last_stage, last_action, manager_notified_at, stop_reason)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(lead_key),
                        None,
                        data.get("status") or "sent",
                        data.get("date"),
                        data.get("timestamp"),
                        int(data.get("reply_count") or 0),
                        data.get("last_reply_date"),
                        data.get("last_stage"),
                        data.get("last_action"),
                        data.get("manager_notified_at"),
                        data.get("stop_reason"),
                    ),
                )

            cooldown_until = float(legacy.get("cooldown_until") or 0)
            if cooldown_until > time.time():
                reason = legacy.get("cooldown_reason") or "migrated from legacy DB"
                for account in account_names:
                    self._set_cooldown_locked(account, cooldown_until, reason)

            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES('migrated_from_json', ?)", (json_path,)
            )
            self._conn.commit()
            logger.info(f"Migrated {len(contacted)} leads from {json_path}")
            return True

    # --- leads -----------------------------------------------------------

    def get_lead(self, lead_key):
        lead_key = _lead_key(lead_key)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM leads WHERE lead_key=?", (lead_key,)
            ).fetchone()
            return dict(row) if row else None

    def find_lead(self, username, peer_id):
        username = _lead_key(username or "")
        peer_key = _lead_key(peer_id or "")
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM leads WHERE lead_key=? OR lead_key=? OR peer_id=? LIMIT 1",
                (username, peer_key, peer_id),
            ).fetchone()
            return dict(row) if row else None

    def add_contacted(self, lead_key, account, status):
        lead_key = _lead_key(lead_key)
        with self._lock:
            self._conn.execute(
                """INSERT INTO leads (lead_key, account, status, date, timestamp, reply_count, last_reply_date)
                   VALUES (?,?,?,?,?,0,?)
                   ON CONFLICT(lead_key) DO UPDATE SET
                     account=excluded.account, status=excluded.status,
                     date=excluded.date, timestamp=excluded.timestamp""",
                (str(lead_key), account, status, _today(), time.time(), _today()),
            )
            self._conn.commit()

    def remove_lead_if_status(self, lead_key, account, status):
        lead_key = _lead_key(lead_key)
        with self._lock:
            self._conn.execute(
                "DELETE FROM leads WHERE lead_key=? AND account=? AND status=?",
                (lead_key, account, status),
            )
            self._conn.commit()

    def claim_lead_account(self, lead_key, account):
        """Bind a legacy (pre-multi-account) lead to the account that got the reply."""
        lead_key = _lead_key(lead_key)
        with self._lock:
            self._conn.execute(
                "UPDATE leads SET account=? WHERE lead_key=? AND account IS NULL",
                (account, lead_key),
            )
            self._conn.commit()

    def set_lead_peer(self, lead_key, peer_id):
        lead_key = _lead_key(lead_key)
        with self._lock:
            self._conn.execute(
                "UPDATE leads SET peer_id=? WHERE lead_key=?", (peer_id, lead_key)
            )
            self._conn.commit()

    def reset_reply_count_if_new_day(self, lead_key):
        lead_key = _lead_key(lead_key)
        with self._lock:
            row = self._conn.execute(
                "SELECT reply_count, last_reply_date FROM leads WHERE lead_key=?",
                (lead_key,),
            ).fetchone()
            if row is None:
                return 0
            if row["last_reply_date"] != _today():
                self._conn.execute(
                    "UPDATE leads SET last_reply_date=?, reply_count=0 WHERE lead_key=?",
                    (_today(), lead_key),
                )
                self._conn.commit()
                return 0
            return int(row["reply_count"] or 0)

    def apply_decision(self, lead_key, decision, replied):
        """Mirror of the legacy post-reply bookkeeping."""
        lead_key = _lead_key(lead_key)
        action = decision.get("action")
        status = decision.get("status")
        with self._lock:
            row = self._conn.execute(
                "SELECT reply_count, manager_notified_at FROM leads WHERE lead_key=?",
                (lead_key,),
            ).fetchone()
            if row is None:
                return False
            reply_count = int(row["reply_count"] or 0)
            if action in {"handoff_to_manager", "silent_stop", "manual_review"}:
                reply_count = 999
            elif replied:
                reply_count += 1
            stop_reason = None
            if action == "silent_stop":
                stop_reason = decision.get("reason") or decision.get("action_reason")
            self._conn.execute(
                """UPDATE leads SET last_stage=?, last_action=?, reply_count=?,
                       status=COALESCE(?, status),
                       stop_reason=COALESCE(?, stop_reason)
                   WHERE lead_key=?""",
                (
                    decision.get("stage"),
                    action,
                    reply_count,
                    status,
                    stop_reason,
                    lead_key,
                ),
            )
            self._conn.commit()
            return bool(row["manager_notified_at"])

    def mark_manager_notified(self, lead_key, status=None):
        lead_key = _lead_key(lead_key)
        with self._lock:
            self._conn.execute(
                "UPDATE leads SET manager_notified_at=?, status=COALESCE(?, status) WHERE lead_key=?",
                (time.time(), status, lead_key),
            )
            self._conn.commit()

    # --- durable admin notification outbox ------------------------------

    def enqueue_admin_notification(
        self, lead_key, account, recipient, message, target_status
    ):
        lead_key = _lead_key(lead_key)
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO admin_notifications
                       (lead_key, account, recipient, message, target_status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(lead_key) DO UPDATE SET
                       account=excluded.account,
                       recipient=excluded.recipient,
                       message=excluded.message,
                       target_status=excluded.target_status,
                       updated_at=excluded.updated_at""",
                (lead_key, account, recipient, message, target_status, now, now),
            )
            self._conn.commit()

    def pending_admin_notifications(self, limit=20):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM admin_notifications ORDER BY created_at LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def fail_admin_notification(self, lead_key, error):
        lead_key = _lead_key(lead_key)
        with self._lock:
            self._conn.execute(
                """UPDATE admin_notifications
                   SET attempts=attempts+1, last_error=?, updated_at=?
                   WHERE lead_key=?""",
                (str(error)[:500], time.time(), lead_key),
            )
            self._conn.commit()

    def complete_admin_notification(self, lead_key):
        lead_key = _lead_key(lead_key)
        with self._lock:
            row = self._conn.execute(
                "SELECT target_status FROM admin_notifications WHERE lead_key=?",
                (lead_key,),
            ).fetchone()
            if row is None:
                return False
            now = time.time()
            self._conn.execute(
                "UPDATE leads SET manager_notified_at=?, status=? WHERE lead_key=?",
                (now, row["target_status"], lead_key),
            )
            self._conn.execute(
                "DELETE FROM admin_notifications WHERE lead_key=?",
                (lead_key,),
            )
            self._conn.commit()
            return True

    def sent_today(self, account):
        placeholders = ",".join("?" for _ in COUNTED_DAILY_STATUSES_EXCLUDED)
        with self._lock:
            row = self._conn.execute(
                f"""SELECT COUNT(*) AS c FROM leads
                    WHERE account=? AND date=? AND status NOT IN ({placeholders})""",
                (account, _today(), *COUNTED_DAILY_STATUSES_EXCLUDED),
            ).fetchone()
            return int(row["c"])

    # --- queue -----------------------------------------------------------

    def enqueue_lead(self, lead_key, context):
        """Idempotent enqueue; returns True only for a brand-new lead."""
        lead_key = _lead_key(lead_key)
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM leads WHERE lead_key=?", (lead_key,)
            ).fetchone()
            if exists:
                return False
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO queue (lead_key, context, status, enqueued_at, updated_at)
                   VALUES (?,?, 'pending', ?, ?)""",
                (lead_key, context, time.time(), time.time()),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def claim_next_pending(self, account, max_attempts):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM queue WHERE status='pending' ORDER BY enqueued_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            if row["attempts"] >= max_attempts:
                self._conn.execute(
                    "UPDATE queue SET status='failed', updated_at=? WHERE lead_key=?",
                    (time.time(), row["lead_key"]),
                )
                self._conn.commit()
                return {**dict(row), "status": "failed"}
            self._conn.execute(
                """UPDATE queue SET status='processing', assigned_account=?,
                       attempts=attempts+1, updated_at=? WHERE lead_key=?""",
                (account, time.time(), row["lead_key"]),
            )
            self._conn.commit()
            claimed = dict(row)
            claimed.update(status="processing", assigned_account=account, attempts=row["attempts"] + 1)
            return claimed

    def release_lead(self, lead_key, error=None):
        lead_key = _lead_key(lead_key)
        with self._lock:
            self._conn.execute(
                """UPDATE queue SET status='pending', assigned_account=NULL,
                       last_error=?, updated_at=? WHERE lead_key=?""",
                (error, time.time(), lead_key),
            )
            self._conn.commit()

    def finish_queue(self, lead_key, status, error=None):
        lead_key = _lead_key(lead_key)
        with self._lock:
            self._conn.execute(
                "UPDATE queue SET status=?, last_error=?, updated_at=? WHERE lead_key=?",
                (status, error, time.time(), lead_key),
            )
            self._conn.commit()

    def requeue_stuck(self, older_than_seconds=1800):
        """Crash recovery: leads stuck in 'processing' after a restart go back to pending."""
        cutoff = time.time() - older_than_seconds
        with self._lock:
            cur = self._conn.execute(
                "UPDATE queue SET status='pending', assigned_account=NULL WHERE status='processing' AND updated_at<?",
                (cutoff,),
            )
            self._conn.commit()
            return cur.rowcount

    def pending_count(self):
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM queue WHERE status='pending'"
            ).fetchone()
            return int(row["c"])

    def get_queue_attempts(self, lead_key):
        lead_key = _lead_key(lead_key)
        with self._lock:
            row = self._conn.execute(
                "SELECT attempts FROM queue WHERE lead_key=?", (lead_key,)
            ).fetchone()
            return int(row["attempts"]) if row else 0

    # --- account state ----------------------------------------------------

    def ensure_account(self, account):
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO account_state (account) VALUES (?)", (account,)
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO account_control (account) VALUES (?)", (account,)
            )
            self._conn.commit()

    def account_enabled(self, account):
        with self._lock:
            row = self._conn.execute(
                "SELECT enabled FROM account_control WHERE account=?", (account,)
            ).fetchone()
            return row is None or bool(row["enabled"])

    def set_account_enabled(self, account, enabled):
        with self._lock:
            self._conn.execute(
                """INSERT INTO account_control (account, enabled) VALUES (?,?)
                   ON CONFLICT(account) DO UPDATE SET enabled=excluded.enabled""",
                (account, 1 if enabled else 0),
            )
            self._conn.commit()

    def daily_limit(self, account, default):
        with self._lock:
            row = self._conn.execute(
                "SELECT daily_limit FROM account_control WHERE account=?", (account,)
            ).fetchone()
            return int(row["daily_limit"]) if row and row["daily_limit"] is not None else default

    def set_daily_limit(self, account, limit):
        with self._lock:
            self._conn.execute(
                """INSERT INTO account_control (account, daily_limit) VALUES (?,?)
                   ON CONFLICT(account) DO UPDATE SET daily_limit=excluded.daily_limit""",
                (account, int(limit)),
            )
            self._conn.commit()

    def get_cooldown_remaining(self, account):
        with self._lock:
            row = self._conn.execute(
                "SELECT cooldown_until FROM account_state WHERE account=?", (account,)
            ).fetchone()
            until = float(row["cooldown_until"]) if row else 0
            return max(0, int(until - time.time()))

    def _set_cooldown_locked(self, account, until, reason):
        self._conn.execute(
            """INSERT INTO account_state (account, cooldown_until, cooldown_reason)
               VALUES (?,?,?)
               ON CONFLICT(account) DO UPDATE SET
                 cooldown_until=MAX(account_state.cooldown_until, excluded.cooldown_until),
                 cooldown_reason=excluded.cooldown_reason""",
            (account, until, reason),
        )

    def activate_cooldown(self, account, seconds, reason):
        with self._lock:
            self._set_cooldown_locked(account, time.time() + seconds, reason)
            self._conn.commit()
        logger.warning(f"[{account}] outbound cooldown for {seconds}s: {reason}")

    def set_account_health(self, account, healthy, error=None):
        with self._lock:
            self._conn.execute(
                """INSERT INTO account_state (account, healthy, last_error) VALUES (?,?,?)
                   ON CONFLICT(account) DO UPDATE SET healthy=excluded.healthy, last_error=excluded.last_error""",
                (account, 1 if healthy else 0, error),
            )
            self._conn.commit()

    def mark_dispatched(self, account):
        with self._lock:
            self._conn.execute(
                "UPDATE account_state SET last_dispatch_at=? WHERE account=?",
                (time.time(), account),
            )
            self._conn.commit()

    def last_dispatch_at(self, account):
        with self._lock:
            row = self._conn.execute(
                "SELECT last_dispatch_at FROM account_state WHERE account=?", (account,)
            ).fetchone()
            return float(row["last_dispatch_at"]) if row else 0.0

    # --- transparency ------------------------------------------------------

    def record_message(self, lead_key, account, direction, text, meta=None):
        lead_key = _lead_key(lead_key)
        with self._lock:
            self._conn.execute(
                "INSERT INTO transcripts (lead_key, account, direction, text, meta, created_at) VALUES (?,?,?,?,?,?)",
                (
                    lead_key,
                    account,
                    direction,
                    text,
                    json.dumps(meta, ensure_ascii=False) if meta else None,
                    time.time(),
                ),
            )
            self._conn.commit()

    def record_llm_call(self, task, provider, model, ok, latency_ms, error=None, lead_key=None):
        with self._lock:
            self._conn.execute(
                """INSERT INTO llm_calls (task, provider, model, ok, latency_ms, error, lead_key, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (task, provider, model, 1 if ok else 0, latency_ms, error, lead_key, time.time()),
            )
            self._conn.commit()

    def list_leads(self, status=None, limit=100):
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM leads WHERE status=? ORDER BY timestamp DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM leads ORDER BY timestamp DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(r) for r in rows]

    def get_transcript(self, lead_key, limit=200):
        lead_key = _lead_key(lead_key)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM transcripts WHERE lead_key=? ORDER BY created_at LIMIT ?",
                (lead_key, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def recent_llm_calls(self, limit=50):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM llm_calls ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def queue_snapshot(self, limit=100):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM queue ORDER BY enqueued_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def dashboard_snapshot(self):
        with self._lock:
            accounts = self._conn.execute(
                """SELECT s.account, s.healthy, s.cooldown_until, s.cooldown_reason,
                          s.last_error, COALESCE(c.enabled, 1) AS enabled, c.daily_limit
                   FROM account_state s LEFT JOIN account_control c ON c.account=s.account
                   ORDER BY s.account"""
            ).fetchall()
            stages = self._conn.execute(
                """SELECT COALESCE(last_stage, 'без этапа') AS name, COUNT(*) AS count
                   FROM leads GROUP BY COALESCE(last_stage, 'без этапа') ORDER BY count DESC"""
            ).fetchall()
            statuses = dict(self._conn.execute(
                "SELECT status, COUNT(*) FROM leads GROUP BY status"
            ).fetchall())
            queued = self._conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
            pending = self._conn.execute(
                "SELECT COUNT(*) FROM queue WHERE status IN ('pending', 'processing')"
            ).fetchone()[0]
            return {
                "accounts": [dict(row) for row in accounts],
                "stages": [dict(row) for row in stages],
                "received": int(queued),
                "pending": int(pending),
                "dropped": int(statuses.get("stopped", 0)),
                "warm": int(statuses.get("warm_notified", 0)),
            }
