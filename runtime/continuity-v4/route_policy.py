"""Per-chat plan selection using the proven shared warning/attempt ledger.

Routes are host-qualified immutable configuration. A plan change is never a retry
of an accepted or uncertain request, and no plan balance is inferred from GPT.
"""
from dataclasses import dataclass
import hashlib
import json
import re
import sqlite3
from types import MappingProxyType
from quota_policy import QuotaPolicy


@dataclass(frozen=True)
class Decision:
    route_id: str
    reason: str
    warning: dict | None = None
    @property
    def route(self):return self.route_id


class PlanPolicy(QuotaPolicy):
    def __init__(self,path,*,account_id,thread_id,clock_epoch,routes,auto_fallback_id,**kwargs):
        if (not isinstance(routes,dict) or not routes or auto_fallback_id not in routes
                or any(not isinstance(k,str) or not re.fullmatch('[a-z0-9][a-z0-9:_-]{0,127}',k)
                    or k in ('openai','pause') or not isinstance(v,str) or not 0<len(v)<=200 for k,v in routes.items())):
            raise ValueError('Exact qualified route display mapping required')
        self.routes=MappingProxyType(dict(routes));self.known_routes=frozenset({'openai',*routes})
        self.auto_fallback_id=auto_fallback_id
        super().__init__(path,account_id=account_id,thread_id=thread_id,clock_epoch=clock_epoch,**kwargs)
        config=hashlib.sha256(json.dumps([routes,auto_fallback_id],sort_keys=True).encode()).hexdigest()
        with self._transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS plan_configuration(scope TEXT PRIMARY KEY,sha256 TEXT NOT NULL)')
            row=db.execute('SELECT sha256 FROM plan_configuration WHERE scope=?',(self.scope,)).fetchone()
            if row is None:
                if db.execute('SELECT 1 FROM attempts WHERE scope=?',(self.scope,)).fetchone():
                    raise ValueError('Existing plan ledger requires explicit migration')
                db.execute('INSERT INTO plan_configuration VALUES(?,?)',(self.scope,config))
            elif row[0]!=config:raise ValueError('Route configuration changed; explicit migration required')

    @staticmethod
    def migrate_route_expansion(path, *, old_routes, new_routes, auto_fallback_id):
        from contextlib import closing
        if (not isinstance(old_routes,dict) or not isinstance(new_routes,dict) or not old_routes
                or not set(old_routes)<=set(new_routes) or auto_fallback_id not in old_routes
                or auto_fallback_id not in new_routes):
            raise ValueError('Additive route expansion requires preserving every existing route ID')
        old_cfg=hashlib.sha256(json.dumps([old_routes,auto_fallback_id],sort_keys=True).encode()).hexdigest()
        new_cfg=hashlib.sha256(json.dumps([new_routes,auto_fallback_id],sort_keys=True).encode()).hexdigest()
        with closing(sqlite3.connect(path,timeout=2)) as db, db:
            db.execute('PRAGMA synchronous=FULL');db.execute('BEGIN IMMEDIATE')
            rows=db.execute('SELECT scope,sha256 FROM plan_configuration').fetchall()
            if not rows or any(sha not in (old_cfg,new_cfg) for _,sha in rows):
                raise ValueError('Existing plan ledger configuration does not match migration source')
            db.execute('UPDATE plan_configuration SET sha256=?',(new_cfg,))

    def _notice(self,db,cycle,kind,route,reason):
        ident=hashlib.sha256(f'{self.scope}:v4:{cycle}:{kind}:{route}:{self.clock_epoch}'.encode()).hexdigest()
        label=self.routes.get(route,'ChatGPT')
        messages={
            'low':'Included ChatGPT allowance is low. Long or in-flight requests may use credits. Automatic switching is planned before the lower margin.',
            'switch':'Upcoming requests are switching to '+label+'. Visible history and an outgoing-model handoff will continue in this chat. The outgoing handoff may use credits on its current provider. Native OpenAI compaction or safety reviews may also use credits after their own warning.',
            'recovery':'Fresh account evidence permits returning upcoming requests to ChatGPT. This is best-effort admission, not an atomic billing reservation.',
        }
        text=messages[kind]+' Reason: '+reason
        db.execute('INSERT OR IGNORE INTO warnings VALUES(?,?,?,?,NULL)',(self.scope,ident,kind,text))
        row=db.execute('SELECT kind,text,receipt FROM warnings WHERE scope=? AND id=?',(self.scope,ident)).fetchone()
        return None if row[2] is not None else {'id':ident,'kind':row[0],'text':row[1]}

    def admit(self,request_id,snapshot,*,now,ready_routes,requested_route_id=None,recover_openai=False):
        if not isinstance(request_id,str) or not re.fullmatch('[A-Za-z0-9:_-]{1,200}',request_id):return Decision('pause','invalid_request_id')
        if not isinstance(ready_routes,(set,frozenset)) or not ready_routes<=self.known_routes:return Decision('pause','invalid_ready_routes')
        if type(recover_openai) is not bool or requested_route_id is not None and requested_route_id not in self.routes:
            return Decision('pause','invalid_route_selection')
        if recover_openai and requested_route_id is not None:return Decision('pause','conflicting_route_selection')
        try:
            with self._transaction() as db:
                if db.execute('SELECT 1 FROM attempts WHERE scope=? AND id=?',(self.scope,request_id)).fetchone():return Decision('pause','request_already_recorded')
                pending=db.execute("SELECT state FROM attempts WHERE scope=? AND state IN ('reserved','uncertain') LIMIT 1",(self.scope,)).fetchone()
                if pending:return Decision('pause','active_request' if pending[0]=='reserved' else 'uncertain_request')
                row=db.execute('SELECT mode,cycle,trigger_at,trigger_epoch FROM policy WHERE scope=?',(self.scope,)).fetchone()
                if row is None or row[0] not in self.known_routes:return Decision('pause','invalid_durable_state')
                mode,cycle,trigger_at,trigger_epoch=row
                reason=self._snapshot_reason(snapshot,now)
                if reason=='invalid_clock':return Decision('pause',reason)
                desired=mode
                if requested_route_id is not None:
                    desired=requested_route_id;reason='manual_model_selection'
                elif mode=='openai' and reason:
                    desired=self.auto_fallback_id
                elif (recover_openai and mode!='openai' and reason is None and snapshot.remaining_percent>self.recover_remaining
                        and trigger_at is not None and (trigger_epoch!=self.clock_epoch or snapshot.observed_monotonic>trigger_at)):
                    desired='openai';reason='new_full_account_evidence'
                if desired not in ready_routes:return Decision('pause','route_unavailable_or_incompatible')
                if desired!=mode:
                    kind='recovery' if desired=='openai' else 'switch'
                    warning=self._notice(db,cycle,kind,desired,reason or 'explicit_selection')
                    if warning:return Decision('pause','warning_pending',warning)
                    mode=desired;cycle+=1
                    trigger_at=None if mode=='openai' else now
                    trigger_epoch=None if mode=='openai' else self.clock_epoch
                    db.execute('UPDATE policy SET mode=?,cycle=?,trigger_at=?,trigger_epoch=? WHERE scope=?',
                        (mode,cycle,trigger_at,trigger_epoch,self.scope))
                if mode=='openai' and snapshot.remaining_percent<=self.warn_remaining:
                    warning=self._notice(db,cycle,'low',mode,'warning_margin_reached')
                    if warning:return Decision('pause','warning_pending',warning)
                db.execute('INSERT INTO attempts VALUES(?,?,?,?)',(self.scope,request_id,'reserved',mode))
                return Decision(mode,reason or ('sticky_plan' if mode!='openai' else 'included_allowance_available'))
        except (sqlite3.Error,OSError):return Decision('pause','durable_storage_unavailable')
