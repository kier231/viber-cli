"""FastAPI loopback controller, retaining the established browser gateway."""
from contextlib import asynccontextmanager
from email.message import Message
import asyncio
import io
import json
import threading
from types import SimpleNamespace
from fastapi import FastAPI, Request
from starlette.responses import Response, JSONResponse
from app.web_server import Handler, CSP


class Gateway(Handler):
    def __init__(self,request,body,server):
        self.server = server
        self.command = request.method
        self.path = request.url.path + ('?'+request.url.query if request.url.query else '')
        self.headers = Message()
        for key,value in request.headers.raw:
            self.headers[key.decode()] = value.decode()
        self.rfile = io.BytesIO(body)
        self.close_connection = False
        self.response = None
        self.manager = server.service if getattr(server.service,'multi_account',False) else None
        if self.manager:
            from app.instances import AccountServer
            self.selection_error = None
            try:
                selected = self.manager.selected(request.headers.get('x-viber-account'))
            except PermissionError as exc:
                self.selection_error = exc
                selected = None
            self.server = AccountServer(server,selected or self.manager.pending)

    def _viber_api(self,path,query):
        if self.manager:
            if self.selection_error:
                raise self.selection_error
            if path == '/viber/api/instances' and self.command == 'GET':
                self._authenticate()
                return self._reply(200,self.manager.status())
            if path == '/viber/api/shutdown' and self.command == 'POST' and self.server.service is self.manager.pending:
                # Maintenance must remain available when no account can bind.
                # Reuse the normal authenticated, confirmed shutdown handler.
                self.server.service = SimpleNamespace(lock=self.manager.lock,managed=True,
                    request_shutdown=self.manager.request_shutdown)
                return super()._viber_api(path,query)
            if path != '/viber/api/session' and self.server.service is self.manager.pending:
                self._authenticate()
                return self._reply(409,{'message':'Link a new number in this Viber VM before using its inbox or sending messages.'})
        return super()._viber_api(path,query)

    def _reply(self,status,body,content_type='application/json; charset=utf-8',extra=()):
        if isinstance(body,(dict,list)):
            body = json.dumps(body,ensure_ascii=False).encode()
        self.response = Response(body,status_code=status,headers={
            'Content-Type':content_type,'Cache-Control':'no-store',
            'X-Content-Type-Options':'nosniff','Content-Security-Policy':CSP,
            'X-Frame-Options':'DENY','Referrer-Policy':'no-referrer',
            'Cross-Origin-Resource-Policy':'same-origin',**dict(extra)})
        return self.response


def create_app(service,port=4001,email_port=4000):
    server = SimpleNamespace(service=service,email_port=email_port,sessions={},session_lock=threading.RLock(),server_address=('127.0.0.1',port))
    @asynccontextmanager
    async def lifespan(app):
        if getattr(service,'multi_account',False):
            service.start()
        else:
            service.start_scheduler()
            service.watcher.start()
            await asyncio.to_thread(service.replies.start)
        try:
            yield
        finally:
            await asyncio.to_thread(service.close)
    app = FastAPI(docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
    app.state.service = service

    @app.api_route('/worker/{action}',methods=['POST'])
    async def worker(action:str,request:Request):
        try:
            if request.headers.get('host') not in {f'127.0.0.1:{port}',f'localhost:{port}'} or request.headers.get('origin'):
                raise PermissionError('Worker interfaces require the local authenticated transport.')
            body = await request.body()
            if len(body)>1048576:
                raise ValueError('Worker requests must be smaller than 1 MB.')
            payload = json.loads(body)
            selected = service.selected(payload['account_id']) if getattr(service,'multi_account',False) else service
            if selected is None:
                raise PermissionError('This Viber account has not been linked and verified.')
            a = selected.accounts
            identity = (request.headers.get('authorization','').removeprefix('Bearer '),payload['account_id'],payload['worker_id'],payload['epoch'])
            a.authenticate(*identity)
            if action=='register':
                # Registration refreshes the existing fenced identity only.
                result = a.register(identity[1],identity[2],identity[0],identity[3])
            elif action=='heartbeat':
                result = a.heartbeat(*identity)
            elif action=='claim':
                result = selected.pool.queue.claim(*identity[1:])
            elif action=='result':
                with selected.store._connect() as db:
                    job = db.execute('SELECT account_id FROM controller_jobs WHERE id=?',(payload['job_id'],)).fetchone()
                if not job or job[0]!=identity[1]:
                    raise PermissionError('Job belongs to another account.')
                selected.pool.queue.finish(payload['job_id'],identity[2],identity[3],payload['state'],payload.get('result'),payload.get('error'))
                result = {'accepted':True}
            elif action=='incoming':
                if identity[1]!=a.account_id:
                    raise PermissionError('Only the activated account may ingest on this controller.')
                a.source(payload['snapshot'])
                result = selected.watcher.inbox.ingest(payload['snapshot'])
                selected.replies.wake.set()
            else:
                return JSONResponse({'message':'Worker endpoint not found.'},404)
            return JSONResponse(result)
        except PermissionError as exc:
            return JSONResponse({'message':str(exc)},403)
        except (ValueError,KeyError,TypeError) as exc:
            return JSONResponse({'message':str(exc)},400)

    @app.api_route('/{path:path}',methods=['GET','HEAD','POST','PATCH','PUT','DELETE','OPTIONS'])
    async def gateway(request:Request,path:str):
        # Reuse the tested host/origin, CSRF, dashboard assets and email proxy.
        facade = Gateway(request,await request.body(),server)
        await asyncio.to_thread(facade._handle)
        return facade.response or JSONResponse({'message':'Local request failed.'},500)
    return app
