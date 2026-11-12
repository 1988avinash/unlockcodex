"""Outgoing-model checkpoint lifecycle. No default network/inference executor.

The host must qualify a provider-specific executor and give it the outgoing
model's identity. It may make ONE bounded request, without tools or retries.
This module does not claim Desktop activation or a qualified live executor.
"""
from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from handoff import CheckpointStore, MAX_SOURCE, build_prompt, validate_handoff, _no_links


class GenerationBlocked(ValueError):
    pass


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


class OutgoingHandoffService:
    def __init__(self,directory):
        self.store=CheckpointStore(directory)
        self.path=self.store.root/'generation.sqlite3'
        with self.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS generations(id TEXT PRIMARY KEY,chat TEXT NOT NULL,input_hash TEXT NOT NULL,state TEXT NOT NULL,result BLOB,result_hash TEXT)')

    @contextmanager
    def transaction(self):
        for suffix in ('','-journal','-wal','-shm'):
            _no_links(Path(str(self.path)+suffix))
        with closing(sqlite3.connect(self.path,timeout=2)) as db,db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db

    def generate(self,logical_id,*,snapshot,visible_source,executor,max_source=MAX_SOURCE):
        if not isinstance(logical_id,str) or not re.fullmatch('[A-Za-z0-9:_-]{1,200}',logical_id) or not callable(executor):
            raise GenerationBlocked('Stable logical identity and a qualified outgoing-model executor required')
        prompt=build_prompt(snapshot,visible_source,max_source=max_source)
        digest=hashlib.sha256(canonical(snapshot)).hexdigest()
        with self.transaction() as db:
            row=db.execute('SELECT input_hash,state,result,result_hash FROM generations WHERE id=?',(logical_id,)).fetchone()
            if row:
                if row[0]!=digest:raise GenerationBlocked('Logical ID already bound to different source')
                if row[1]!='completed':raise GenerationBlocked('Prior generation is unresolved; do not retry')
                if hashlib.sha256(row[2]).hexdigest()!=row[3]:raise GenerationBlocked('Saved generation integrity failed')
                return json.loads(row[2])
            if db.execute("SELECT 1 FROM generations WHERE chat=? AND state IN ('reserved','uncertain')",(snapshot['chat_id'],)).fetchone():
                raise GenerationBlocked('Another checkpoint is unresolved; reconcile before new generation')
            db.execute('INSERT INTO generations VALUES(?,?,?,?,NULL,NULL)',(logical_id,snapshot['chat_id'],digest,'reserved'))
        try:
            prior=self.store.load_latest(snapshot['chat_id'],current_turn_id=snapshot['last_turn_id'],now=snapshot['captured_at'])
            # Executor must translate this bounded request to its qualified wire
            # protocol. It cannot execute tools or silently select another model.
            request={'provider':snapshot['source_provider'],'model':snapshot['source_model'],
                'tools':[],'tool_choice':'none','max_output_tokens':16384,
                'prompt':prompt,'purpose':'task_handoff','logical_id':logical_id}
            response=executor(request)
            if not isinstance(response,dict) or response.get('model')!=snapshot['source_model'] or response.get('status')!='completed':
                raise GenerationBlocked('Outgoing model did not complete the requested checkpoint')
            output=response.get('output')
            if not isinstance(output,list) or not output:raise GenerationBlocked('No completed checkpoint message')
            texts=[]
            for item in output:
                if not isinstance(item,dict):raise GenerationBlocked('Invalid checkpoint output')
                if item.get('type')=='reasoning':continue
                if item.get('type')!='message' or item.get('role')!='assistant':raise GenerationBlocked('Checkpoint attempted a tool or unsupported output')
                content=item.get('content')
                if not isinstance(content,list):raise GenerationBlocked('Checkpoint content unavailable')
                for part in content:
                    if not isinstance(part,dict) or part.get('type')!='output_text' or not isinstance(part.get('text'),str):
                        raise GenerationBlocked('Unsupported checkpoint content')
                    texts.append(part['text'])
            raw=''.join(texts).encode()
            value=validate_handoff(raw,snapshot)
            checkpoint_id=self.store.save(raw,snapshot=snapshot,expected_latest=prior['checkpoint_id'] if prior else None)
            result={'checkpoint_id':checkpoint_id,'snapshot':snapshot,'handoff':value,
                'requires_reconciliation':True,'model_claims_verified':False,
                'unknown_encrypted_compaction_recovered':False}
            encoded=canonical(result)
            with self.transaction() as db:
                db.execute('UPDATE generations SET state=?,result=?,result_hash=? WHERE id=?',
                    ('completed',encoded,hashlib.sha256(encoded).hexdigest(),logical_id))
            return result
        except Exception:
            with self.transaction() as db:
                db.execute("UPDATE generations SET state='uncertain' WHERE id=?",(logical_id,))
            raise

    def reconcile(self,logical_id,*,evidence_receipt):
        """Trusted host reconciliation allows distinct future work, never replay."""
        if not isinstance(evidence_receipt,str) or not evidence_receipt.strip() or len(evidence_receipt)>1000:
            raise GenerationBlocked('Independent reconciliation receipt required')
        with self.transaction() as db:
            row=db.execute('SELECT state FROM generations WHERE id=?',(logical_id,)).fetchone()
            if row is None or row[0] not in ('reserved','uncertain'):
                raise GenerationBlocked('Generation is not pending reconciliation')
            db.execute('UPDATE generations SET state=? WHERE id=?',('reconciled:'+evidence_receipt,logical_id))


def bind_saved_checkpoint(saved,*,request_sha256,current_turn_id,now,max_age_seconds=300):
    """Bind delivery to a new request without relabeling old source as fresh."""
    if not isinstance(request_sha256,str) or not re.fullmatch('[0-9a-f]{64}',request_sha256):
        raise GenerationBlocked('Expected current request digest')
    if type(now) is not int or type(max_age_seconds) is not int or max_age_seconds<=0:
        raise GenerationBlocked('Trusted current time and positive age limit required')
    snapshot=saved['snapshot']
    value=validate_handoff(canonical(saved['handoff']),snapshot)
    expected=hashlib.sha256(canonical({'snapshot':snapshot,'handoff':value})).hexdigest()
    if saved.get('checkpoint_id')!=expected or saved.get('requires_reconciliation') is not True or saved.get('model_claims_verified') is not False:
        raise GenerationBlocked('Checkpoint identity or trust labels invalid')
    age=now-snapshot['captured_at']
    return {'request_sha256':request_sha256,'checkpoint_id':expected,
        'source_provider':snapshot['source_provider'],'source_model':snapshot['source_model'],
        'checkpoint_source_sha256':snapshot['source_sha256'],'checkpoint_turn_id':snapshot['last_turn_id'],
        'text':canonical({'handoff':value,'model_claims_verified':False,
            'unknown_encrypted_compaction_recovered':False,'requires_reconciliation':True}).decode(),
        'stale':snapshot['last_turn_id']!=current_turn_id or not 0<=age<=max_age_seconds,
        'requires_reconciliation':True}
