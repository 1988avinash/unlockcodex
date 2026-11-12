"""Qualified native Guardian passthrough; never translates or switches provider.

Profile: installed native automatic guardian capture 1794318973622771600,
request 7. Approval semantics and lite payload are owned by native Codex.
"""
import hashlib
import http.client
import json
import re
import socket
import threading
import time
from urllib.parse import urlsplit

from paid_compaction_policy_v4 import PaidCompactionPolicy
from protocol_codec import decode_request
from transport_v4 import MAX_WIRE,MAX_BODY,MAX_RESPONSE,strict_json

MODEL='codex-auto-review'
ENDPOINT='https://chatgpt.com/backend-api/codex/responses'


class ReviewBlocked(ValueError):pass


class NativeReviewPolicy(PaidCompactionPolicy):
    warning_purpose='native_safety_review'
    warning_text=('Codex needs its native OpenAI safety reviewer for an upcoming tool action. This review may use '
        'ChatGPT credits while ordinary work stays on your Token Plan. The native reviewer keeps its normal '
        'allow/deny behavior. An uncertain result will pause; this gateway will not retry it automatically.')

    def admit_review(self,ident,evidence,*,now,notifier):
        return self.admit_compaction(ident,evidence,now=now,notifier=notifier)


def qualify_native_review(raw,headers):
    """Exact observed guardian identity, not a user-selected hidden model name."""
    if not isinstance(raw,bytes) or len(raw)>MAX_BODY:raise ReviewBlocked('Bounded native review required')
    body=strict_json(raw)
    if not isinstance(body,dict) or body.get('model')!=MODEL or body.get('stream') is not True:
        raise ReviewBlocked('Qualified native review model/stream required')
    expected={'x-codex-guardian':'reviewer','x-openai-subagent':'guardian',
        'x-openai-internal-codex-responses-lite':'true'}
    if any(headers.get(key)!=value for key,value in expected.items()):raise ReviewBlocked('Native guardian discriminator mismatch')
    metadata=strict_json(headers.get('x-codex-turn-metadata','{}'))
    client=body.get('client_metadata')
    if not isinstance(metadata,dict) or not isinstance(client,dict):raise ReviewBlocked('Guardian scope metadata missing')
    required={'subagent_kind':'guardian','thread_source':'guardian_review','turn_trigger':'guardian_review',
        'request_kind':'turn','model':MODEL}
    if any(metadata.get(key)!=value for key,value in required.items()):raise ReviewBlocked('Unqualified native review purpose')
    child=headers.get('thread-id');parent=headers.get('x-codex-parent-thread-id');session=headers.get('session-id')
    if not all(isinstance(value,str) and value for value in (child,parent,session)) or child==parent:
        raise ReviewBlocked('Distinct native reviewer child and parent required')
    checks={'thread_id':child,'session_id':session,'parent_thread_id':parent}
    if any(metadata.get(key)!=value for key,value in checks.items()):raise ReviewBlocked('Guardian header/metadata lineage mismatch')
    if headers.get('x-client-request-id')!=child:raise ReviewBlocked('Guardian client identity mismatch')
    client_checks={'thread_id':child,'session_id':session,'x-codex-parent-thread-id':parent,'x-openai-subagent':'guardian'}
    if any(client.get(key)!=value for key,value in client_checks.items()):raise ReviewBlocked('Guardian body lineage mismatch')
    nested=strict_json(client.get('x-codex-turn-metadata','{}'))
    if nested!=metadata:raise ReviewBlocked('Guardian nested metadata differs from native header')
    for key in ('turn_id','parent_turn_id','root_turn_id'):
        if not isinstance(metadata.get(key),str) or not metadata[key] or client.get(key)!=metadata[key]:
            raise ReviewBlocked('Guardian turn binding missing or inconsistent')
    response=client.get('parent_response_id')
    if not isinstance(response,str) or not 0<len(response)<=256:raise ReviewBlocked('Recorded parent response binding required')
    items=body.get('input')
    if not isinstance(items,list) or not items or any(not isinstance(item,dict) for item in items):
        raise ReviewBlocked('Materialized native lite review input required')
    if items[0].get('type')!='additional_tools' or items[0].get('role')!='developer':
        raise ReviewBlocked('Qualified native lite tool declaration required')
    return {'child':child,'parent':parent,'session':session,'parent_response_id':response,
        'parent_turn_id':metadata['parent_turn_id'],'root_turn_id':metadata['root_turn_id'],'body':body}


def verify_parent_receipt(listener,account,identity):
    # Registry lineage is checked by _select_scope. This additional binding
    # proves the native review refers to an actually delivered parent response.
    with listener.registry.transaction() as db:
        row=db.execute('SELECT account_id FROM chats WHERE thread_id=?',(identity['parent'],)).fetchone()
        if row!=(account,):raise ReviewBlocked('Reviewer parent is not registered to this account')
    parent=listener.registry.get(account,identity['parent'])
    with parent.store.transaction() as db:
        rows=db.execute("SELECT decoded FROM requests WHERE state IN ('completed','upstream_complete') "
            "AND response IS NOT NULL AND json_valid(response) AND json_extract(response,'$.id')=? LIMIT 2",
            (identity['parent_response_id'],)).fetchall()
    if len(rows)!=1:raise ReviewBlocked('Exact completed parent response is unavailable')
    body=strict_json(rows[0][0]);meta=body.get('client_metadata',{})
    if not isinstance(meta,dict) or meta.get('turn_id')!=identity['parent_turn_id'] or meta.get('root_turn_id')!=identity['root_turn_id']:
        raise ReviewBlocked('Guardian parent turn is not bound to the recorded response')


def install_native_review(server,*,enabled=False,test_endpoint=None,allow_test_endpoint=False,test_token_sha256=None):
    """Install before serving. Enabled is explicit trusted host configuration."""
    if type(enabled) is not bool:raise ValueError('Explicit native review authorization required')
    endpoint=ENDPOINT
    if test_endpoint is not None:
        url=urlsplit(test_endpoint)
        if not allow_test_endpoint or url.scheme!='http' or url.hostname!='127.0.0.1' or not url.port or url.username or url.password or url.query or url.fragment or url.path!='/responses':
            raise ValueError('Explicit loopback native review test endpoint required')
        endpoint=test_endpoint
    if test_token_sha256 is not None and (test_endpoint is None or not isinstance(test_token_sha256,str) or not re.fullmatch('[0-9a-f]{64}',test_token_sha256)):
        raise ValueError('Exact reviewed synthetic token fingerprint requires explicit loopback test endpoint')
    parent=server.RequestHandlerClass
    class NativeReviewHandler(parent):
        def do_POST(self):
            if self.headers.get('x-codex-guardian')=='reviewer' or self.headers.get('x-openai-subagent')=='guardian':
                return self._native_review()
            return super().do_POST()

        def _native_review(self):
            listener=self.server;reserved=False;acquired=False
            self.connection.settimeout(listener.timeout_seconds)
            try:
                prefix='/'+listener.capability
                if not self.local_allowed() or self.path not in (prefix+'/responses',prefix+'/v1/responses'):
                    raise ReviewBlocked('Unsupported native review endpoint')
                sizes=self.headers.get_all('Content-Length',[])
                if len(sizes)!=1 or not sizes[0].isdigit() or int(sizes[0])>MAX_WIRE or 'Transfer-Encoding' in self.headers:
                    raise ReviewBlocked('Single bounded native review body required')
                wire=self.rfile.read(int(sizes[0]))
                if len(wire)!=int(sizes[0]):raise ReviewBlocked('Truncated native review body')
                if not listener.native_review_enabled:
                    self.reply(409,'Native safety review routing has not been enabled; the tool action remains paused');return
                for name in ('thread-id','session-id','chatgpt-account-id','authorization','x-codex-parent-thread-id',
                    'x-codex-turn-metadata','x-client-request-id','x-codex-guardian','x-openai-subagent',
                    'x-openai-internal-codex-responses-lite','content-encoding'):
                    if len(self.headers.get_all(name,[]))>1:raise ReviewBlocked('Duplicate native review header')
                headers={key.lower():value for key,value in self.headers.items()}
                raw=decode_request(wire,headers.get('content-encoding'),max_wire=MAX_WIRE,max_decoded=MAX_BODY)
                identity=qualify_native_review(raw,headers)
                if listener.native_review_test_endpoint:
                    auth=headers.get('authorization','')
                    pinned=(auth.startswith('Bearer ') and listener.native_review_test_token_sha256 is not None
                        and hashlib.sha256(auth[7:].encode()).hexdigest()==listener.native_review_test_token_sha256)
                    if not auth.startswith('Bearer synthetic-') and not pinned:
                        raise ReviewBlocked('Review test endpoint requires synthetic credentials')
                self._select_scope(identity['body'])
                verify_parent_receipt(listener,self.server.account_id,identity)
                self.request_id=hashlib.sha256(json.dumps(identity['body'],sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
                acquired=self.server.request_lock.acquire(False)
                if not acquired and self.server.finishing.is_set():
                    acquired=self.server.request_lock.acquire(timeout=min(1,self.server.timeout_seconds))
                if not acquired:self.reply(409,'Another request in this reviewer chat is active');return
                self.server.finishing.clear()
                evidence=listener.callback_factory.compaction_snapshot(self.server.account_id,headers)
                policy=NativeReviewPolicy(self.server.policy,clock=listener.clock)
                decision=policy.admit_review(self.request_id,evidence,now=listener.clock(),notifier=self.server.notifier)
                if decision.route!='openai':self.reply(409,'Native safety review paused: '+decision.reason);return
                reserved=True
                self.server.store.reserve(self.request_id,'openai',MODEL,wire,raw)
                self.server.store.prepared(self.request_id,wire,None)
                evidence=listener.callback_factory.compaction_snapshot(self.server.account_id,headers)
                if policy.check_dispatch(self.request_id,evidence,now=listener.clock()).route!='openai':
                    raise ReviewBlocked('Native review spend/freshness check changed before dispatch')
                self._review_forward(wire,headers,listener.native_review_endpoint)
                self.server.store.state(self.request_id,'completed');self.server.policy.settle(self.request_id,'completed')
            except Exception:
                if reserved:
                    try:self.server.store.state(self.request_id,'uncertain')
                    except Exception:pass
                    try:self.server.policy.settle(self.request_id,'uncertain')
                    except Exception:pass
                if not self.response_started:self.reply(502 if reserved else 403,'Native safety review paused; preserve its receipt and do not retry uncertain work')
                elif not getattr(self,'terminal_sent',False):self.close_connection=True
            finally:
                if acquired:self.server.request_lock.release()

        def _completed(self,data,route,model,wire,status):
            if model!=MODEL:return super()._completed(data,route,model,wire,status)
            value=strict_json(data)
            if not isinstance(value,dict) or value.get('model')!=MODEL or value.get('status')!='completed' or not isinstance(value.get('id'),str) or not value['id'] or not isinstance(value.get('output'),list):
                raise ReviewBlocked('Native review actual model/status/identity mismatch')
            self.server.store.completed_upstream(self.request_id,data,bytes(wire),status)

        def _review_forward(self,raw,headers,endpoint):
            target=urlsplit(endpoint)
            connection=(http.client.HTTPSConnection if target.scheme=='https' else http.client.HTTPConnection)(target.hostname,target.port,timeout=self.server.timeout_seconds)
            omitted={'host','connection','proxy-authorization','proxy-connection','transfer-encoding','content-length','upgrade',
                'keep-alive','te','trailer','proxy-authenticate'}
            omitted.update(name.strip().lower() for name in headers.get('connection','').split(',') if name.strip())
            forwarded={key:value for key,value in headers.items() if key not in omitted}
            wire=bytearray();watchdog=None;response=None;deadline=time.monotonic()+self.server.timeout_seconds
            try:
                connection.connect();owned=connection.sock
                def expire():
                    try:owned.shutdown(socket.SHUT_RDWR)
                    except OSError:pass
                    try:owned.close()
                    except OSError:pass
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TimeoutError('Native review connection deadline')
                watchdog=threading.Timer(remaining,expire);watchdog.daemon=True;watchdog.start()
                connection.request('POST',target.path,raw,forwarded);response=connection.getresponse()
                self.server.store.state(self.request_id,'dispatched',response.status)
                if response.status!=200 or response.getheader('Content-Encoding','identity').lower()!='identity':
                    wire.extend(response.read(MAX_RESPONSE+1));raise ReviewBlocked('Native review upstream response rejected')
                media=response.getheader('Content-Type','').split(';')[0].strip().lower()
                if media in ('','text/event-stream'):self._stream(response,wire,'openai',MODEL)
                elif media=='application/json':
                    wire.extend(response.read(MAX_RESPONSE+1))
                    if len(wire)>MAX_RESPONSE:raise ReviewBlocked('Native review output exceeds bound')
                    self._completed(bytes(wire),'openai',MODEL,wire,response.status)
                    self.server.finishing.set()
                    self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(wire)));self.end_headers()
                    self.wfile.write(wire);self.wfile.flush();self.terminal_sent=True
                else:raise ReviewBlocked('Unqualified native review response protocol')
            except Exception:
                self.server.store.state(self.request_id,'uncertain',response.status if response else None,bytes(wire));raise
            finally:
                if watchdog:watchdog.cancel()
                if response is not None:response.close()
                connection.close()
    server.RequestHandlerClass=NativeReviewHandler
    server.native_review_enabled=enabled;server.native_review_endpoint=endpoint
    server.native_review_test_endpoint=test_endpoint is not None
    server.native_review_test_token_sha256=test_token_sha256
