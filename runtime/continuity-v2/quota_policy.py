"""Best-effort quota policy; this is not an atomic billing reservation.

Only full account/rateLimits/read data is admissible. Callers supply receipt-time
monotonic timestamps from this boot, never provider wall-clock timestamps. Bind
them to a trusted process-independent clock_epoch, such as Windows
Win32_OperatingSystem.LastBootUpTime. A per-process random identifier is wrong:
restarting a process does not reset the system monotonic clock.

Snapshot is a trusted internal value, not an authentication mechanism. Never
deserialize client-supplied Snapshot(valid=True) data or accept client-supplied
account/clock identities; the host adapter owns those inputs.
"""
from dataclasses import dataclass
import math
from contextlib import closing, contextmanager
import hashlib
import json
import re
import sqlite3


@dataclass(frozen=True)
class Snapshot:
    account_id: str
    observed_monotonic: float
    clock_epoch: str
    valid: bool
    ordinary_allowed: bool
    remaining_percent: float | None
    reason: str


def _number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _epoch(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:+-]{1,128}', value) is not None


@dataclass(frozen=True)
class Decision:
    route: str
    reason: str
    warning: dict | None = None


class QuotaPolicy:
    """Durable admission for one account/thread, independent of model execution.

    Call admit only BEFORE dispatch. A route result reserves request_id. A
    pause never authorizes dispatch. Show a returned warning through a trusted
    notifier, then persist its successful delivery receipt with acknowledge_warning
    and call admit again. Never acknowledge merely because the event was logged.

    The router must provide a stable logical request ID and genuine snapshot
    receipt timestamps. It must not change IDs to evade uncertain-outcome blocks.
    manual_fallback is set only by the trusted router after observing the native
    selection of the qualified MiniMax alias; client request metadata cannot
    authorize it. It does not override active or uncertain work. Returning to
    OpenAI uses request_recovery and its ordinary fresh-evidence/notice gates.
    """
    def __init__(self, path, *, account_id, thread_id, clock_epoch, warn_remaining=20,
                 switch_remaining=10, recover_remaining=25, max_age=60):
        if not all(isinstance(x, str) and x for x in (account_id, thread_id)):
            raise ValueError('Explicit account and thread identity required')
        if not _epoch(clock_epoch):
            raise ValueError('A trusted process-independent clock epoch is required')
        if not all(_number(x) for x in (warn_remaining, switch_remaining, recover_remaining, max_age)):
            raise ValueError('Finite policy parameters required')
        if not 0 <= switch_remaining < warn_remaining < recover_remaining < 100 or max_age <= 0:
            raise ValueError('Invalid policy thresholds')
        self.path, self.account_id = str(path), account_id
        self.clock_epoch = clock_epoch
        self.scope = hashlib.sha256(json.dumps([account_id, thread_id]).encode()).hexdigest()
        self.warn_remaining, self.switch_remaining = warn_remaining, switch_remaining
        self.recover_remaining, self.max_age = recover_remaining, max_age
        config_hash = hashlib.sha256(json.dumps([warn_remaining, switch_remaining, recover_remaining, max_age]).encode()).hexdigest()
        with self._transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS policy(scope TEXT PRIMARY KEY,config TEXT NOT NULL,mode TEXT NOT NULL,cycle INTEGER NOT NULL,trigger_at REAL,trigger_epoch TEXT)')
            # Preserve a prior pilot's fallback/attempt state through this additive migration.
            if 'trigger_epoch' not in {row[1] for row in db.execute('PRAGMA table_info(policy)')}:
                db.execute('ALTER TABLE policy ADD COLUMN trigger_epoch TEXT')
            db.execute('CREATE TABLE IF NOT EXISTS warnings(scope TEXT NOT NULL,id TEXT PRIMARY KEY,kind TEXT NOT NULL,text TEXT NOT NULL,receipt TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS attempts(scope TEXT NOT NULL,id TEXT NOT NULL,state TEXT NOT NULL,route TEXT NOT NULL,PRIMARY KEY(scope,id))')
            db.execute('INSERT OR IGNORE INTO policy VALUES(?,?,?,0,NULL,NULL)', (self.scope, config_hash, 'openai'))
            stored = db.execute('SELECT config FROM policy WHERE scope=?', (self.scope,)).fetchone()
            if stored[0] != config_hash:
                raise ValueError('Stored policy parameters differ; explicit migration required')

    @contextmanager
    def _transaction(self):
        with closing(sqlite3.connect(self.path, timeout=2)) as db, db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db

    def _warning(self, db, cycle, kind, reason):
        ident = hashlib.sha256(f'{self.scope}:{cycle}:{kind}:{self.clock_epoch}'.encode()).hexdigest()
        messages = {
            'low': 'Included allowance is at or below the warning margin. A long request or other active chats may use credits. Automatic MiniMax switching is planned before the lower margin.',
            'switch': 'Upcoming requests are switching to MiniMax Token Plan. In-flight requests and other clients may still use ChatGPT credits. Unsupported history will pause instead of being discarded.',
            'recovery': 'Fresh account evidence permits returning upcoming requests to OpenAI. This is best-effort admission, not a guarantee that no credits will be used.',
        }
        text = messages[kind] + ' Reason: ' + reason
        db.execute('INSERT OR IGNORE INTO warnings VALUES(?,?,?,?,NULL)', (self.scope, ident, kind, text))
        row = db.execute('SELECT kind,text,receipt FROM warnings WHERE scope=? AND id=?', (self.scope, ident)).fetchone()
        if row[2] is not None:
            return None
        return {'id': ident, 'kind': row[0], 'text': row[1]}

    def acknowledge_warning(self, event_id, delivery_receipt):
        if not isinstance(delivery_receipt, str) or not delivery_receipt.strip() or len(delivery_receipt) > 1000:
            raise ValueError('A bounded successful notifier-delivery receipt is required')
        with self._transaction() as db:
            row = db.execute('SELECT receipt FROM warnings WHERE scope=? AND id=?', (self.scope, event_id)).fetchone()
            if row is None:
                raise ValueError('Unknown warning for this scope')
            if row[0] is None:
                db.execute('UPDATE warnings SET receipt=? WHERE scope=? AND id=?', (delivery_receipt, self.scope, event_id))

    def _snapshot_reason(self, snapshot, now):
        if not _number(now) or now < 0:
            return 'invalid_clock'
        if not isinstance(snapshot, Snapshot) or not snapshot.valid:
            return 'missing_or_invalid_snapshot'
        if snapshot.account_id != self.account_id:
            return 'account_mismatch'
        if snapshot.clock_epoch != self.clock_epoch:
            return 'clock_epoch_mismatch'
        if not _number(snapshot.observed_monotonic) or not 0 <= now-snapshot.observed_monotonic <= self.max_age:
            return 'stale_or_future_snapshot'
        if not _number(snapshot.remaining_percent) or not 0 <= snapshot.remaining_percent <= 100:
            return 'invalid_remaining_allowance'
        if snapshot.ordinary_allowed is not True:
            return 'ordinary_usage_not_allowed'
        if snapshot.remaining_percent <= self.switch_remaining:
            return 'switch_margin_reached'
        return None

    def admit(self, request_id, snapshot, *, now, fallback_ready, request_recovery=False, manual_fallback=False):
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9:_-]{1,200}', request_id):
            return Decision('pause', 'invalid_request_id')
        if any(type(flag) is not bool for flag in (fallback_ready, request_recovery, manual_fallback)):
            return Decision('pause', 'invalid_admission_flags')
        if manual_fallback and request_recovery:
            return Decision('pause', 'conflicting_route_selection')
        try:
            with self._transaction() as db:
                if db.execute('SELECT 1 FROM attempts WHERE scope=? AND id=?', (self.scope, request_id)).fetchone():
                    return Decision('pause', 'request_already_recorded')
                pending = db.execute("SELECT state FROM attempts WHERE scope=? AND state IN ('reserved','uncertain') LIMIT 1", (self.scope,)).fetchone()
                if pending:
                    return Decision('pause', 'active_request' if pending[0] == 'reserved' else 'uncertain_request')
                row = db.execute('SELECT mode,cycle,trigger_at,trigger_epoch FROM policy WHERE scope=?', (self.scope,)).fetchone()
                if row is None or row[0] not in ('openai', 'minimax'):
                    return Decision('pause', 'invalid_durable_state')
                mode, cycle, trigger_at, trigger_epoch = row
                reason = self._snapshot_reason(snapshot, now)
                if reason == 'invalid_clock':
                    return Decision('pause', reason)
                if manual_fallback:
                    reason = 'manual_model_selection'
                if reason and mode == 'openai':
                    mode, trigger_at, trigger_epoch = 'minimax', now, self.clock_epoch
                    db.execute('UPDATE policy SET mode=?,trigger_at=?,trigger_epoch=? WHERE scope=?', (mode, trigger_at, trigger_epoch, self.scope))
                if mode == 'minimax':
                    if (request_recovery and reason is None and snapshot.remaining_percent > self.recover_remaining
                            and trigger_at is not None
                            and (trigger_epoch != self.clock_epoch or snapshot.observed_monotonic > trigger_at)):
                        warning = self._warning(db, cycle, 'recovery', 'new_full_account_evidence')
                        if warning:
                            return Decision('pause', 'warning_pending', warning)
                        mode, cycle = 'openai', cycle+1
                        db.execute('UPDATE policy SET mode=?,cycle=?,trigger_at=NULL,trigger_epoch=NULL WHERE scope=?', (mode, cycle, self.scope))
                    else:
                        warning = self._warning(db, cycle, 'switch', reason or 'sticky_fallback')
                        if warning:
                            return Decision('pause', 'warning_pending', warning)
                        if not fallback_ready:
                            return Decision('pause', 'fallback_unavailable_or_incompatible')
                if mode == 'openai' and snapshot.remaining_percent <= self.warn_remaining:
                    warning = self._warning(db, cycle, 'low', 'warning_margin_reached')
                    if warning:
                        return Decision('pause', 'warning_pending', warning)
                db.execute('INSERT INTO attempts VALUES(?,?,?,?)', (self.scope, request_id, 'reserved', mode))
                return Decision(mode, reason or ('sticky_fallback' if mode == 'minimax' else 'included_allowance_available'))
        except (sqlite3.Error, OSError):
            return Decision('pause', 'durable_storage_unavailable')

    def settle(self, request_id, outcome):
        if outcome not in ('completed', 'uncertain'):
            raise ValueError('Outcome must be completed or uncertain')
        with self._transaction() as db:
            row = db.execute('SELECT state FROM attempts WHERE scope=? AND id=?', (self.scope, request_id)).fetchone()
            if row is None or row[0] != 'reserved':
                raise ValueError('Only a reserved request can be settled once')
            db.execute('UPDATE attempts SET state=? WHERE scope=? AND id=?', (outcome, self.scope, request_id))

    def reconcile(self, request_id, *, evidence_receipt):
        """Explicit trusted reconciliation; never automatically called on timeout.

        Records that no automatic replay is permitted, but future distinct work
        may proceed. The receipt must point to independently reconciled state.
        """
        if not isinstance(evidence_receipt, str) or not evidence_receipt.strip() or len(evidence_receipt)>1000:
            raise ValueError('A bounded reconciliation receipt is required')
        with self._transaction() as db:
            row = db.execute('SELECT state FROM attempts WHERE scope=? AND id=?', (self.scope, request_id)).fetchone()
            if row is None or row[0] not in ('reserved', 'uncertain'):
                raise ValueError('Request is not pending reconciliation')
            db.execute('UPDATE attempts SET state=? WHERE scope=? AND id=?', ('reconciled:'+evidence_receipt, self.scope, request_id))


def parse_snapshot(payload, *, account_id, required_limits, observed_monotonic, clock_epoch, full_read=True):
    """Parse the installed Codex protocol; reject partial or unknown quota shapes.

    required_limits is explicitly qualified configuration such as
    {'codex': {300, 10080}}. New bucket IDs/durations need qualification rather
    than inheriting another bucket's healthy allowance. Credits never admit.
    """
    def invalid(reason):
        return Snapshot(account_id, observed_monotonic, clock_epoch, False, False, None, reason)
    if not _epoch(clock_epoch):
        return invalid('invalid_clock_epoch')
    if not full_read or not isinstance(payload, dict):
        return invalid('full_snapshot_required')
    if not isinstance(account_id, str) or not account_id or payload.get('accountId') != account_id:
        return invalid('account_mismatch_or_missing')
    if not _number(observed_monotonic) or observed_monotonic < 0:
        return invalid('invalid_observation_time')
    if not isinstance(required_limits, dict) or not required_limits:
        return invalid('unqualified_limit_configuration')
    for key, durations in required_limits.items():
        if not isinstance(key, str) or not key or not isinstance(durations, (set, frozenset, list, tuple)) or not durations:
            return invalid('unqualified_limit_configuration')
        if any(not isinstance(x, int) or isinstance(x, bool) or x <= 0 for x in durations):
            return invalid('unqualified_limit_configuration')
    ordinary = payload.get('ordinaryUsageAllowed')
    if type(ordinary) is not bool:
        return invalid('ordinary_usage_unknown')
    buckets = payload.get('rateLimitsByLimitId')
    if buckets is None:
        legacy = payload.get('rateLimits')
        if not isinstance(legacy, dict) or not isinstance(legacy.get('limitId'), str):
            return invalid('missing_bucket_identity')
        buckets = {legacy['limitId']: legacy}
    if not isinstance(buckets, dict) or set(buckets) != set(required_limits):
        return invalid('missing_or_unqualified_buckets')
    remaining = []
    for limit_id, bucket in buckets.items():
        if not isinstance(bucket, dict) or bucket.get('limitId') != limit_id:
            return invalid('bucket_identity_mismatch')
        if type(bucket.get('spendControlReached')) is not bool:
            return invalid('spend_control_unknown')
        if bucket['spendControlReached'] or bucket.get('rateLimitReachedType') is not None:
            ordinary = False
        seen = set()
        for name in ('primary', 'secondary'):
            window = bucket.get(name)
            if window is None:
                continue
            if not isinstance(window, dict):
                return invalid('invalid_window')
            duration, used = window.get('windowDurationMins'), window.get('usedPercent')
            if not isinstance(duration, int) or isinstance(duration, bool) or duration in seen:
                return invalid('invalid_window_duration')
            if duration not in required_limits[limit_id] or not _number(used) or not 0 <= used <= 100:
                return invalid('invalid_or_unqualified_window')
            reset = window.get('resetsAt')
            if reset is not None and (not isinstance(reset, int) or isinstance(reset, bool) or reset < 0):
                return invalid('invalid_reset_time')
            seen.add(duration)
            remaining.append(100-used)
        if seen != set(required_limits[limit_id]):
            return invalid('missing_required_window')
    return Snapshot(account_id, observed_monotonic, clock_epoch, True, ordinary, min(remaining), 'full_snapshot')
