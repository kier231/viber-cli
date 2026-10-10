"""Route local CLI actions through the account controller and durable ledger."""
import json
import time
import uuid
import httpx


def dispatch(args):
    origin='http://127.0.0.1:4001'
    with httpx.Client(base_url=origin,timeout=100,headers={'Origin':origin,'X-Viber-Browser':'1'}) as client:
        response=client.post('/viber/api/session',json={})
        response.raise_for_status()
        client.headers['X-Viber-CSRF']=response.json()['csrf']
        def api(path,payload=None):
            r=client.get('/viber/api/'+path) if payload is None else client.post('/viber/api/'+path,json=payload)
            value=r.json()
            if r.is_error:raise ValueError(value.get('message','Local controller request failed.'))
            if 'operation_id' in value:
                deadline=time.monotonic()+180
                while time.monotonic()<deadline:
                    op=api('operations/'+value['operation_id'])
                    if op['state']=='SUCCEEDED':return op['result']
                    if op['state'] in ('FAILED','INTERRUPTED'):raise ValueError(op['error'])
                    time.sleep(.2)
                raise ValueError('Operation is still queued. Check the dashboard before retrying.')
            return value
        def send(text):
            preview=api('prepare',{'lead_id':args.lead_id,'text':text})
            print(json.dumps({k:preview[k] for k in ('lead','viber_name','text')},ensure_ascii=False,indent=2))
            if input('Send? [y/N]: ')!='y':return {'state':'CANCELLED'}
            return api('send',{'confirmed':True,'preview_token':preview['token'],'request_key':str(uuid.uuid4())})
        command=args.command
        if command=='contacts':result=api('contacts')
        elif command=='add-contact':result=api('contacts',{'phone':args.phone,'company_name':args.company})
        elif command=='set-viber-name':result=api('name',{'lead_id':args.lead_id,'name':args.name})
        elif command in ('open','read'):result=api(command,{'lead_id':args.lead_id})
        elif command=='send':result=send(args.message)
        elif command=='read-current':result=api('read-current',{})
        elif command=='inspect':result=api('inspect',{})
        elif command=='chat':
            while True:
                line=input('Message, /read, or /exit: ')
                if line=='/exit':return
                print(json.dumps(api('read',{'lead_id':args.lead_id}) if line=='/read' else send(line),ensure_ascii=False,indent=2))
            return
        else:
            print('Viber dashboard: '+origin+'/viber')
            return
        print(json.dumps(result,ensure_ascii=False,indent=2))
