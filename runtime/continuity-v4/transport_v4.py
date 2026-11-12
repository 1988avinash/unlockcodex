"""V3 transport fork; accepts native OpenAI SSE with absent Content-Type.

Transport constraints derive from the reviewed v1 gateway (SHA-256
f1c7f2b4e60570e1f70236256e1593a7282676ca2af2c4f87ac80d5a689b3c82).
No callback defaults pretend to notify a user or create an old-model checkpoint.
OAuth is forwarded only to the fixed OpenAI endpoint. Other chats are rejected.
"""
from contextlib import closing,contextmanager
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import importlib.util
import json
import math
from pathlib import Path
from types import MappingProxyType
import re
import select
import socket
import sqlite3
import threading
import time
from urllib.parse import urlsplit

from history_v4 import capture_response,capture_compaction,translate_request,HistoryBlocked
from protocol_codec import decode_request,DecodeRejected
from quota_policy import QuotaPolicy

ENDPOINTS={'openai':'https://chatgpt.com/backend-api/codex/responses',
           'minimax':'https://api.minimax.io/v1/responses'}
MAX_WIRE=4*1024*1024  # live zstd bodies reached 1.86 MiB against the former 2 MiB cap
MAX_BODY=8*1024*1024
MAX_FRAME=8*1024*1024
MAX_RESPONSE=16*1024*1024
FALLBACK_ALIAS='tp-minimax-m31'
FALLBACK_MODEL='MiniMax-M3.1-Flash'


class ClientStreamAborted(ConnectionError):
    """Raised when the local native client closes an active SSE stream before terminal completion."""


class UpstreamInputRejected(ValueError):
    """Upstream refused the request input (for example context_length_exceeded) before emitting any output.

    Nothing ran, so the caller forwards the real error to the native client and
    archives the attempt instead of pausing the chat for reconciliation.
    """
    def __init__(self,status,payload):
        super().__init__('Upstream rejected request input');self.status=status;self.payload=payload


class UpstreamStreamDied(ValueError):
    """Upstream cleanly ended (EOF) before any output reached the client.

    TCP delivered everything the provider sent, so no output was ever produced;
    the caller archives the attempt instead of pausing the chat.
    """


def _client_stream_closed(sock):
    if not isinstance(sock,socket.socket) or sock.fileno()<0:return False
    try:
        readable,_,_=select.select([sock],[],[],0)
        if not readable:return False
        return sock.recv(1,socket.MSG_PEEK)==b''
    except (BrokenPipeError,ConnectionAbortedError,ConnectionResetError):return True
    except OSError:return False


def strict_json(raw):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result: raise ValueError('Duplicate JSON key')
            result[key]=value
        return result
    value=json.loads(raw,object_pairs_hook=pairs)
    json.dumps(value,allow_nan=False)
    return value


def visible_rebase(raw,*,chat_id,target_provider):
    """Explicitly lossy compatibility option, never a decryption/recovery claim.

    Removes only recognized opaque reasoning/compaction containers. The strict
    translator still validates remaining fields, closed tools and remote refs.
    Caller MUST bind a fresh outgoing-model handoff and deliver the loss notice.
    """
    body=strict_json(raw);items=body.get('input')
    if not isinstance(items,list):raise HistoryBlocked('Visible rebase needs materialized history')
    output=[];compactions=0;reasoning=0
    allowed={'type','id','encrypted_content','summary','content','status','internal_chat_message_metadata_passthrough'}
    for item in items:
        if not isinstance(item,dict):raise HistoryBlocked('Invalid history item')
        kind=item.get('type')
        if kind not in ('reasoning','compaction','context_compaction'):
            output.append(item);continue
        if set(item)-allowed:raise HistoryBlocked('Unqualified opaque item fields')
        if item.get('encrypted_content') is not None and not isinstance(item['encrypted_content'],str):
            raise HistoryBlocked('Invalid opaque item')
        converted={key:value for key,value in item.items() if key not in ('encrypted_content','internal_chat_message_metadata_passthrough')}
        converted['type']='reasoning'
        if converted.get('summary') is None:converted['summary']=[]
        if converted.get('content') is None:converted['content']=[]
        output.append(converted)
        if kind=='reasoning':reasoning+=1
        else:compactions+=1
    if not compactions and not reasoning:raise HistoryBlocked('No qualified opaque container to rebase')
    body['input']=output
    result=translate_request(json.dumps(body,allow_nan=False).encode(),chat_id=chat_id,
        source_provider='openai',target_provider=target_provider,disclosure_acknowledged=True,max_bytes=MAX_BODY)
    return result,{'mode':'explicit_incomplete_visible_history','dropped_compactions':compactions,
        'reasoning_containers_rebased':reasoning,'original_sha256':hashlib.sha256(raw).hexdigest()}


class EvidenceStore:
    def __init__(self,path,thread_id,account_id):
        self.path=str(path);self.thread_id=thread_id
        with self.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS scope(id INTEGER PRIMARY KEY,thread_id TEXT,account_id TEXT)')
            db.execute('INSERT OR IGNORE INTO scope VALUES(1,?,?)',(thread_id,account_id))
            if db.execute('SELECT thread_id,account_id FROM scope WHERE id=1').fetchone()!=(thread_id,account_id):
                raise ValueError('Evidence directory belongs to another account/chat')
            db.execute('CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,state TEXT NOT NULL,route TEXT NOT NULL,model TEXT NOT NULL,wire BLOB NOT NULL,decoded BLOB NOT NULL,forwarded BLOB,response BLOB,response_wire BLOB,handoff TEXT,status INTEGER)')
    @contextmanager
    def transaction(self):
        with closing(sqlite3.connect(self.path,timeout=2)) as db,db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
    def reserve(self,ident,route,model,wire,raw):
        with self.transaction() as db:
            if db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]>=10000:
                raise ValueError('Evidence capacity requires reconciliation')
            db.execute('INSERT INTO requests(id,state,route,model,wire,decoded) VALUES(?,?,?,?,?,?)',
                (ident,'reserved',route,model,wire,raw))
    def prepared(self,ident,raw,handoff):
        with self.transaction() as db:
            db.execute('UPDATE requests SET forwarded=?,handoff=? WHERE id=?',
                (raw,json.dumps(handoff,allow_nan=False) if handoff is not None else None,ident))
    def completed_upstream(self,ident,response,response_wire,status):
        with self.transaction() as db:
            db.execute('UPDATE requests SET state=?,response=?,response_wire=?,status=? WHERE id=?',
                ('upstream_complete',response,response_wire,status,ident))
    def state(self,ident,state,status=None,response_wire=None):
        with self.transaction() as db:
            db.execute('UPDATE requests SET state=?,status=COALESCE(?,status),response_wire=COALESCE(?,response_wire) WHERE id=?',
                (state,status,response_wire,ident))
    def receipt(self,ident):
        with self.transaction() as db:
            row=db.execute('SELECT state,route,model,wire,decoded,forwarded,response,status FROM requests WHERE id=?',(ident,)).fetchone()
        if row is None:return None
        result=dict(zip(('state','route','actual_model'),row[:3]))
        for name,value in zip(('wire_sha256','decoded_sha256','forwarded_sha256','response_sha256'),row[3:7]):
            result[name]=hashlib.sha256(value).hexdigest() if value is not None else None
        result['upstream_status']=row[7]
        return result
    def history(self):
        responses=[];compactions=[]
        with self.transaction() as db:
            rows=db.execute("SELECT route,forwarded,response FROM requests WHERE state='completed' AND response IS NOT NULL ORDER BY rowid").fetchall()
        for route,forwarded,raw in rows:
            responses.append(capture_response(raw,chat_id=self.thread_id,provider=route))
            body=strict_json(forwarded)
            if isinstance(body.get('input'),list) and body['input'] and body['input'][-1].get('type')=='compaction_trigger':
                compactions.append(capture_compaction(forwarded,raw,chat_id=self.thread_id,source_provider=route))
        return responses,compactions


class Handler(BaseHTTPRequestHandler):
    response_started=False
    request_id=None
    def log_message(self,*args): pass
    def send_response(self,code,message=None):
        self.response_started=True
        super().send_response(code,message)
    def reply(self,status,message):
        value=message if isinstance(message,dict) else {'error':{'type':'local_continuity_pause','message':message}}
        payload=json.dumps(value).encode()
        self.send_response(status);self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(payload)));self.send_header('Cache-Control','no-store')
        if self.request_id:self.send_header('X-Continuity-Request-ID',self.request_id)
        self.end_headers();self.wfile.write(payload)
    def send_error(self,code,message=None,explain=None): self.reply(code,'Unsupported local request')
    def local_allowed(self):
        return (self.headers.get_all('Host',[])==['127.0.0.1:'+str(self.server.server_port)]
            and not any(name in self.headers for name in ('Origin','Upgrade','Sec-WebSocket-Key')))
    def do_GET(self):
        prefix='/'+self.server.capability
        if not self.local_allowed():self.reply(403,'Local native access only');return
        if self.path==prefix+'/health':
            self.reply(200,{'mode':'single_chat_automatic_backend','activated_in_desktop':False});return
        if re.fullmatch(re.escape(prefix)+r'/requests/[0-9a-f]{64}',self.path):
            receipt=self.server.store.receipt(self.path.rsplit('/',1)[1])
            self.reply(200,receipt) if receipt else self.reply(404,'Unknown request')
            return
        self.reply(404,'Unsupported local path')
    def _identity(self):
        for header,expected in [('thread-id',self.server.thread_id),('session-id',self.server.thread_id),
                                 ('ChatGPT-Account-ID',self.server.account_id)]:
            if self.headers.get_all(header,[])!=[expected]:raise ValueError('Native account/chat identity mismatch')
        for header in ('Authorization','Content-Encoding','x-codex-turn-metadata','x-client-request-id'):
            if len(self.headers.get_all(header,[]))>1:raise ValueError('Duplicate native identity/header')
        auth=self.headers.get('Authorization','')
        if not auth.startswith('Bearer ') or len(auth)<8 or len(auth)>16384:raise ValueError('Native OAuth authorization required')
        if self.headers.get('x-client-request-id',self.server.thread_id)!=self.server.thread_id:
            raise ValueError('Native request scope mismatch')
        metadata=self.headers.get('x-codex-turn-metadata')
        if metadata is not None:
            value=strict_json(metadata)
            if not isinstance(value,dict) or any(value.get(key)!=self.server.thread_id for key in ('thread_id','session_id')):
                raise ValueError('Native turn metadata scope mismatch')
    def do_POST(self):
        self.reply(403,'Use the qualified provider-aware gateway')
    def _forward(self,route,model,raw):
        spec=None if route=='openai' else self.server.route_registry.get(route)
        if spec is not None and spec.protocol_profile=='agy-structured':
            executor=self.server.route_executors.get(spec.protocol_profile)
            if executor is None:raise ValueError('Qualified structured executor missing')
            parent=self.server.scope.state_dir/'executors';parent.mkdir(exist_ok=True)
            context=MappingProxyType({'account_id':self.server.account_id,'thread_id':self.server.thread_id,
                'request_id':self.request_id,'evidence_dir':str(parent/self.request_id)})
            self.server.store.state(self.request_id,'dispatched')
            data=executor(spec,raw,context)
            if not isinstance(data,bytes) or len(data)>MAX_RESPONSE:raise ValueError('Invalid structured response bound')
            # The native client requested streaming Responses. The signed-in
            # agent finishes its bounded JSON result first; expose that result
            # with the same qualified native SSE sequence used by local tests.
            response=strict_json(data)
            events=[{'type':'response.created','response':dict(response,status='in_progress',output=[])}]
            for index,item in enumerate(response.get('output',[])):
                events.extend([{'type':'response.output_item.added','output_index':index,'item':item},
                    {'type':'response.output_item.done','output_index':index,'item':item}])
            events.append({'type':'response.completed','response':response})
            stream=b''.join(('event: '+event['type']+'\ndata: '+json.dumps(dict(event,sequence_number=index),separators=(',',':'),allow_nan=False)+'\n\n').encode()
                for index,event in enumerate(events))
            if len(stream)>MAX_RESPONSE:raise ValueError('Structured SSE response exceeds bound')
            self._completed(data,route,model,stream,200)
            self.server.finishing.set();self.send_response(200);self.send_header('Content-Type','text/event-stream')
            self.send_header('X-Continuity-Route',route);self.send_header('X-Continuity-Request-ID',self.request_id)
            self.send_header('Content-Length',str(len(stream)));self.end_headers();self.wfile.write(stream);self.wfile.flush();self.terminal_sent=True
            return
        target=urlsplit(self.server.endpoints[route])
        # Only the admitted, source-bound native compaction branch receives the
        # longer deadline. Never mutate the shared server or infer it from input.
        timeout_seconds=300 if route=='openai' and getattr(self,'remote_compaction_forwarded',None)==raw else self.server.timeout_seconds
        connection=(http.client.HTTPSConnection if target.scheme=='https' else http.client.HTTPConnection)(target.hostname,target.port,timeout=timeout_seconds)
        headers={'Content-Type':'application/json','Accept':'application/json, text/event-stream'}
        if route=='openai':
            for name in ('Authorization','ChatGPT-Account-ID','Version','Originator','User-Agent','Thread-Id','Session-Id',
                         'X-Client-Request-Id','X-Codex-Window-Id','X-Codex-Turn-Metadata','X-Codex-Routing-Hint','X-Codex-Beta-Features','OpenAI-Beta',
                         'X-Codex-Parent-Thread-Id','X-OpenAI-Subagent'):
                if name in self.headers:headers[name]=self.headers[name]
            if getattr(self,'qualified_lite_compaction',False):
                headers['X-OpenAI-Internal-Codex-Responses-Lite']='true'
        else:
            key=self.server.credential_reader(spec)
            if not isinstance(key,str) or not key or '\r' in key or '\n' in key:raise ValueError('Invalid Token Plan credential')
            headers['Authorization']='Bearer '+key
            if spec.protocol_profile=='responses-lite':headers['X-OpenAI-Internal-Codex-Responses-Lite']='true'
        wire=bytearray();watchdog=None;response=None
        deadline=time.monotonic()+timeout_seconds
        try:
            connection.connect()
            owned_socket=connection.sock
            def expire():
                # Close the exact owned socket, never a recycled descriptor.
                try:owned_socket.shutdown(socket.SHUT_RDWR)
                except OSError:pass
                try:owned_socket.close()
                except OSError:pass
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('Upstream connect deadline')
            watchdog=threading.Timer(remaining,expire);watchdog.daemon=True;watchdog.start()
            connection.request('POST',target.path,raw,headers)
            response=connection.getresponse()
            self.server.store.state(self.request_id,'dispatched',response.status)
            if response.getheader('Content-Encoding','identity').lower()!='identity':raise ValueError('Unexpected response encoding')
            if not 200<=response.status<300:
                wire.extend(response.read(MAX_RESPONSE+1))
                if response.status in (400,413,422) and len(wire)<=MAX_RESPONSE:
                    try:rejection=strict_json(bytes(wire))
                    except (ValueError,UnicodeError,RecursionError):rejection=None
                    if isinstance(rejection,dict) and isinstance(rejection.get('error'),dict):
                        raise UpstreamInputRejected(response.status,rejection)
                raise ValueError('Upstream rejected')
            media=response.getheader('Content-Type','').split(';')[0].strip().lower()
            # The installed native OpenAI backend was observed returning valid
            # SSE with no MIME header. The strict frame/model/terminal parser is
            # still mandatory; an empty header alone never establishes success.
            if media in ('','text/event-stream'):
                self._stream(response,wire,route,model)
            elif media=='application/json':
                wire.extend(response.read(MAX_RESPONSE+1))
                if len(wire)>MAX_RESPONSE:raise ValueError('Upstream oversized')
                data=bytes(wire)
                self._completed(data,route,model,wire,response.status)
                self.server.finishing.set()
                self.send_response(200);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(data)));self.send_header('X-Continuity-Route',route)
                self.send_header('X-Continuity-Request-ID',self.request_id);self.end_headers()
                self.wfile.write(data);self.wfile.flush();self.terminal_sent=True
            else:raise ValueError('Unqualified upstream content type')
        except Exception:
            self.server.store.state(self.request_id,'uncertain',response.status if response else None,bytes(wire))
            raise
        finally:
            if watchdog:watchdog.cancel()
            try:
                if response is not None:response.close()
            finally:
                connection.close()
    def _completed(self,data,route,model,wire,status):
        value=strict_json(data)
        if not isinstance(value,dict) or value.get('model')!=model or not isinstance(value.get('id'),str) or not value['id']:raise ValueError('Unexpected actual model or response identity')
        provider='openai' if route=='openai' else self.server.route_registry.get(route).provider_id
        capture_response(data,chat_id=self.server.thread_id,provider=provider)
        self.server.store.completed_upstream(self.request_id,data,bytes(wire),status)
    def _stream(self,response,wire,route,model):
        # Frames are validated before delivery. A completed response is persisted
        # before its terminal frame reaches native Codex; EOF is never success.
        frame=bytearray();seen_created=False;response_id=None;done_items={};emitted_output=False
        while True:
            if getattr(self,'stream_started',False) and _client_stream_closed(getattr(self,'connection',None)):
                raise ClientStreamAborted('Local client closed SSE stream')
            line=response.readline(MAX_FRAME+1)
            if not line:
                if getattr(self,'stream_started',False) and _client_stream_closed(getattr(self,'connection',None)):
                    raise ClientStreamAborted('Local client closed SSE stream')
                # Clean EOF: TCP delivered everything the provider sent, so zero output
                # frames means none was ever produced. Connection failures (OSError/RST)
                # below keep the uncertain latch: reset can discard unread output.
                if not emitted_output:raise UpstreamStreamDied('Upstream stream died before any output')
                raise ValueError('Upstream stream ended without completed response')
            wire.extend(line);frame.extend(line)
            if len(wire)>MAX_RESPONSE or len(frame)>MAX_FRAME:raise ValueError('Stream exceeds bounded evidence size')
            if line not in (b'\n',b'\r\n'):continue
            payload=[];event_name=None
            for part in bytes(frame).decode('utf-8').splitlines():
                if part.startswith('data:'):payload.append(part[5:].lstrip(' '))
                elif part.startswith('event:'):
                    if event_name is not None:raise ValueError('Duplicate SSE event')
                    event_name=part[6:].strip()
                elif part.startswith(('id:','retry:')):pass  # Metadata only; never reconnect or retry.
                elif part and not part.startswith(':'):raise ValueError('Unsupported SSE field')
            terminal=False
            if payload:
                value=strict_json('\n'.join(payload))
                if not isinstance(value,dict) or not isinstance(value.get('type'),str):raise ValueError('Invalid SSE data')
                kind=value['type']
                if event_name is not None and event_name!=kind:raise ValueError('Conflicting SSE event type')
                if (kind=='error' and not emitted_output and isinstance(value.get('error'),dict)
                        and value['error'].get('type')=='invalid_request_error'):
                    if self.response_started:
                        # Headers are already out: deliver the upstream error frame itself.
                        self.wfile.write(frame);self.wfile.flush();self.terminal_sent=True
                    raise UpstreamInputRejected(400,{'error':value['error']})
                if kind in ('error','response.failed','response.incomplete'):raise ValueError('Upstream stream failed')
                emitted_output=emitted_output or kind.startswith('response.output_') or 'delta' in kind
                if kind=='response.created':
                    created=value.get('response',{})
                    if seen_created or not isinstance(created,dict) or created.get('model')!=model or not isinstance(created.get('id'),str) or not created['id']:
                        raise ValueError('Unexpected initial response identity/model')
                    seen_created=True;response_id=created['id']
                elif not seen_created:raise ValueError('Stream output arrived before actual model identity')
                if kind=='response.output_item.done':
                    idx=value.get('output_index');item=value.get('item')
                    if type(idx) is int and idx>=0 and isinstance(item,dict):
                        done_items[idx]=item
                if kind=='response.completed':
                    completed=value.get('response')
                    if not isinstance(completed,dict) or completed.get('id')!=response_id:raise ValueError('Mismatched terminal response')
                    completed_for_store=(dict(completed,output=[done_items[i] for i in sorted(done_items)])
                        if completed.get('output')==[] and done_items else completed)
                    self._completed(json.dumps(completed_for_store,separators=(',',':'),allow_nan=False).encode(),route,model,wire,response.status)
                    terminal=True
            if not self.response_started:
                if not seen_created:
                    frame.clear();continue
                self.send_response(200);self.send_header('Content-Type','text/event-stream')
                self.send_header('Cache-Control','no-store');self.send_header('Connection','close')
                self.send_header('X-Continuity-Route',route);self.send_header('X-Continuity-Request-ID',self.request_id)
                self.end_headers();self.stream_started=True
            if terminal:self.server.finishing.set()
            if not terminal and _client_stream_closed(getattr(self,'connection',None)):
                raise ClientStreamAborted('Local client closed SSE stream')
            try:self.wfile.write(frame);self.wfile.flush()
            except (BrokenPipeError,ConnectionAbortedError,ConnectionResetError) as exc:
                if not terminal:raise ClientStreamAborted('Local client closed SSE stream') from exc
                raise
            frame.clear()
            if terminal:self.terminal_sent=True;return


class Server(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=False
    def server_bind(self):
        if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        super().server_bind()
    def handle_error(self,request,client_address):pass


