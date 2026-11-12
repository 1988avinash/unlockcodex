"""Explicitly authorized compaction exception; ordinary routing is unchanged.

Shares the existing per-chat attempts ledger. Credits are usable only for this
one warned compaction, never evidence of ordinary quota recovery or spend-control
permission. No inference, token reads, retries or ledger reconciliation here.
"""
from dataclasses import dataclass
import hashlib
import math
import re
import sqlite3
import time

from quota_policy import Decision, Snapshot, parse_snapshot

EXHAUSTED = 'rate_limit_reached'
HARD_BLOCKS = {'workspace_owner_credits_depleted','workspace_member_credits_depleted',
    'workspace_owner_usage_limit_reached','workspace_member_usage_limit_reached'}


@dataclass(frozen=True)
class CompactionSnapshot:
    quota: Snapshot
    spend_control_reached: bool | None
    rate_limit_reached_type: str | None
    credits_available: bool | None
    valid: bool
    reason: str


def parse_compaction_snapshot(payload, *, account_id, observed_monotonic, clock_epoch,
                              required_limits=None):
    required_limits = {'codex':{10080}} if required_limits is None else required_limits
    quota=parse_snapshot(payload,account_id=account_id,required_limits=required_limits,
        observed_monotonic=observed_monotonic,clock_epoch=clock_epoch,full_read=True)
    def invalid(reason):return CompactionSnapshot(quota,None,None,None,False,reason)
    if not quota.valid:return invalid(quota.reason)
    if set(required_limits)!={'codex'}:return invalid('unqualified_compaction_buckets')
    buckets=payload.get('rateLimitsByLimitId')
    bucket=payload['rateLimits'] if buckets is None else buckets['codex']
    spend=bucket.get('spendControlReached')
    reached=bucket.get('rateLimitReachedType')
    if type(spend) is not bool:return invalid('spend_control_unknown')
    if reached is not None and (not isinstance(reached,str) or reached not in HARD_BLOCKS|{EXHAUSTED}):
        return invalid('unqualified_rate_limit_reason')
    credits=bucket.get('credits');available=None
    if isinstance(credits,dict) and type(credits.get('hasCredits')) is bool and type(credits.get('unlimited')) is bool:
        available=credits['hasCredits'] or credits['unlimited']
    return CompactionSnapshot(quota,spend,reached,available,True,'full_compaction_snapshot')


def _finite(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


class PaidCompactionPolicy:
    warning_purpose='paid_compaction'
    warning_text=('Codex needs an OpenAI compaction to preserve this chat. This one compaction and its outgoing-model '
        'handoff may consume ChatGPT credits, even while ordinary work stays on your Token Plan. '
        'You allowed this exception. No automatic retry will occur if the outcome is uncertain.')
    def __init__(self, ordinary_policy, *, clock=time.monotonic):
        if not callable(clock):raise ValueError('Trusted monotonic clock required')
        self.policy=ordinary_policy
        self.clock=clock

    def _reason(self,evidence,now):
        if not _finite(now) or now<0:return 'invalid_clock'
        if not isinstance(evidence,CompactionSnapshot) or evidence.valid is not True:return 'invalid_compaction_snapshot'
        snapshot=evidence.quota
        if not isinstance(snapshot,Snapshot) or snapshot.valid is not True:return 'invalid_compaction_snapshot'
        if snapshot.account_id!=self.policy.account_id:return 'account_mismatch'
        if snapshot.clock_epoch!=self.policy.clock_epoch:return 'clock_epoch_mismatch'
        if not _finite(snapshot.observed_monotonic) or not 0<=now-snapshot.observed_monotonic<=self.policy.max_age:
            return 'stale_or_future_compaction_snapshot'
        if not _finite(snapshot.remaining_percent) or not 0<=snapshot.remaining_percent<=100:return 'invalid_remaining_allowance'
        if evidence.spend_control_reached is not False:return 'spend_control_block_or_unknown'
        reached=evidence.rate_limit_reached_type
        if reached in HARD_BLOCKS:return 'workspace_credit_or_usage_limit_block'
        if reached is not None and reached!=EXHAUSTED:return 'unqualified_rate_limit_reason'
        if type(snapshot.ordinary_allowed) is not bool:return 'ordinary_usage_unknown'
        exhausted=snapshot.remaining_percent==0 or reached==EXHAUSTED
        if snapshot.ordinary_allowed is not True and not exhausted:return 'ordinary_permission_denied_without_verified_exhaustion'
        if exhausted and evidence.credits_available is not True:return 'credits_unavailable_or_unknown'
        return None

    def _ledger_reason(self,db,ident):
        scope=self.policy.scope
        if db.execute('SELECT 1 FROM attempts WHERE scope=? AND id=?',(scope,ident)).fetchone():
            return 'request_already_recorded'
        row=db.execute("SELECT state FROM attempts WHERE scope=? AND state IN ('reserved','uncertain') LIMIT 1",(scope,)).fetchone()
        if row:return 'active_request' if row[0]=='reserved' else 'uncertain_request'
        row=db.execute('SELECT mode FROM policy WHERE scope=?',(scope,)).fetchone()
        if row is None or row[0] not in getattr(self.policy,'known_routes',('openai','minimax')):return 'invalid_durable_state'
        return None

    def check_dispatch(self, request_id, evidence, *, now):
        """Recheck after handoff, before compact POST, without a second reserve.

        The caller supplies newly refreshed native evidence after a long handoff.
        A failed check does not clear the reservation: that handoff may already
        have billed, so the owner must record uncertain and reconcile it.
        """
        if not isinstance(request_id,str) or not re.fullmatch('[0-9a-f]{64}',request_id):
            return Decision('pause','invalid_request_id')
        reason=self._reason(evidence,now)
        if reason:return Decision('pause',reason)
        warning_id=hashlib.sha256((self.policy.scope+':'+self.warning_purpose+':'+request_id+':'+self.policy.clock_epoch).encode()).hexdigest()
        try:
            with self.policy._transaction() as db:
                row=db.execute('SELECT state,route FROM attempts WHERE scope=? AND id=?',(self.policy.scope,request_id)).fetchone()
                if row!=('reserved','openai'):return Decision('pause','own_compaction_reservation_required')
                if db.execute("SELECT 1 FROM attempts WHERE scope=? AND id!=? AND state IN ('reserved','uncertain')",
                        (self.policy.scope,request_id)).fetchone():return Decision('pause','another_active_or_uncertain_request')
                warning=db.execute('SELECT kind,receipt FROM warnings WHERE scope=? AND id=?',(self.policy.scope,warning_id)).fetchone()
                if warning is None or warning[0]!=self.warning_purpose or not isinstance(warning[1],str) or not warning[1].strip():
                    return Decision('pause','compaction_credit_warning_required')
            return Decision('openai','reserved_compaction_dispatch_rechecked')
        except (sqlite3.Error,OSError):return Decision('pause','durable_storage_unavailable')

    def admit_compaction(self, request_id, evidence, *, now, notifier):
        """Host calls only when explicit paid-compaction authorization is enabled.

        Render a per-request credit warning, then reserve atomically in the SAME
        ledger as ordinary requests. Warning delays/races are rechecked. Caller
        settles through ordinary_policy.settle; no retry can reuse the identity.
        """
        if not isinstance(request_id,str) or not re.fullmatch('[0-9a-f]{64}',request_id):return Decision('pause','invalid_request_id')
        if not callable(notifier):return Decision('pause','trusted_notifier_required')
        reason=self._reason(evidence,now)
        if reason:return Decision('pause',reason)
        started=self.clock()
        if not _finite(started) or started<0:return Decision('pause','invalid_clock')
        warning_id=hashlib.sha256((self.policy.scope+':'+self.warning_purpose+':'+request_id+':'+self.policy.clock_epoch).encode()).hexdigest()
        text=self.warning_text
        try:
            with self.policy._transaction() as db:
                reason=self._ledger_reason(db,request_id)
                if reason:return Decision('pause',reason)
                db.execute('INSERT OR IGNORE INTO warnings VALUES(?,?,?,?,NULL)',
                    (self.policy.scope,warning_id,self.warning_purpose,text))
                warning=db.execute('SELECT kind,text,receipt FROM warnings WHERE scope=? AND id=?',
                    (self.policy.scope,warning_id)).fetchone()
            if warning[2] is None:
                try:receipt=notifier({'id':warning_id,'kind':warning[0],'text':warning[1]})
                except Exception:return Decision('pause','compaction_credit_warning_failed')
                if not isinstance(receipt,str) or not receipt.strip() or len(receipt)>1000:
                    return Decision('pause','compaction_credit_warning_failed')
                self.policy.acknowledge_warning(warning_id,receipt)
            elapsed=self.clock()-started
            if not _finite(elapsed) or elapsed<0:return Decision('pause','invalid_clock')
            with self.policy._transaction() as db:
                reason=self._reason(evidence,now+elapsed) or self._ledger_reason(db,request_id)
                if reason:return Decision('pause',reason)
                db.execute('INSERT INTO attempts VALUES(?,?,?,?)',(self.policy.scope,request_id,'reserved','openai'))
            return Decision('openai','explicit_compaction_with_credit_warning')
        except (sqlite3.Error,OSError):
            return Decision('pause','durable_storage_unavailable')
