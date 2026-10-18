"""Approve the reviewed batch and publish via the app's existing serialized queue."""
import asyncio,json,time,sys
from pathlib import Path
import httpx
BASE=Path(__file__).resolve().parents[1]
REPORT=BASE/'data/codex-all-20261016-810b4820a46b.json'
STATE=BASE/'data/codex_publish_all_2026-10-16.json'
URL='http://127.0.0.1:8770'

def emit(message):print(json.dumps(message,ensure_ascii=False),flush=True)
async def run(preview_only=False):
 report=json.loads(REPORT.read_text())
 state=json.loads(STATE.read_text()) if STATE.exists() else {'approved':[],'batches':[],'skipped_without_paths':[]}
 def save():STATE.write_text(json.dumps(state,ensure_ascii=False,indent=2))
 async with httpx.AsyncClient(base_url=URL,timeout=120) as client:
  async def req(method,path,body=None):
   response=await client.request(method,path,json=body)
   response.raise_for_status();return response.json()
  async def operation(action,payload):
   op=await req('POST','/api/operations',{'action':action,'payload':payload})
   emit({'stage':action,'operation_id':op['operation_id']})
   last=time.monotonic()
   while True:
    status=await req('GET','/api/operations/'+op['operation_id'])
    if status['status']=='completed':return status['result']
    if status['status'] in ('error','cancelled'):raise RuntimeError(str(status['error'] or status['status']))
    if time.monotonic()-last>30:
     emit({'stage':action,'status':status['status'],'operation_id':op['operation_id']});last=time.monotonic()
    await asyncio.sleep(5)
  # API approval keeps approval history and preserves the previous publication.
  for entry in report['results']:
   if not entry['paths']:
    if entry['id'] not in state['skipped_without_paths']:state['skipped_without_paths'].append(entry['id'])
    continue
   if entry['generation_id'] in state['approved']:continue
   detail=await req('GET','/api/products/'+__import__('urllib.parse',fromlist=['quote']).quote(entry['id'],safe=''))
   latest=detail['product']['latest_generation']
   if latest['id']!=entry['generation_id']:raise RuntimeError('Proposta cambiata: '+entry['title'])
   if latest['status'] != 'approved':
    await req('POST','/api/generations/'+entry['generation_id']+'/approve',{'indexes':list(range(len(entry['paths'])))})
   state['approved'].append(entry['generation_id'])
   if len(state['approved'])%250==0:save();emit({'approved':len(state['approved'])})
  save();emit({'approved':len(state['approved']),'without_paths':len(state['skipped_without_paths'])})
  eligible=sorted((r for r in report['results'] if r['paths']),key=lambda r:(r['paths'],r['id']))
  batches=[eligible[i:i+500] for i in range(0,len(eligible),500)]
  for index,entries in enumerate(batches):
   batch=next((b for b in state['batches'] if b['index']==index),None)
   if batch and batch.get('status')=='published':continue
   ids=[r['id'] for r in entries]
   preview=await operation('preview',{'product_ids':ids})
   summary={k:len(preview.get(k,[])) for k in ['create_collections','add_products','remove_products','blocked','hierarchy_updates']}
   emit({'batch':index+1,'products':len(ids),'preview':summary})
   batch={'index':index,'product_ids':ids,'preview_id':preview['preview_id'],'summary':summary,'status':'previewed'}
   state['batches']=[b for b in state['batches'] if b['index']!=index]+[batch];save()
   if preview.get('blocked'):raise RuntimeError('Operazioni bloccate: '+json.dumps(preview['blocked'],ensure_ascii=False))
   if preview_only:return
   result=await operation('publish',{'preview_id':preview['preview_id'],'confirm':True})
   batch.update(status='published',result=result);save();emit({'batch_completed':index+1,**result})
  state['complete']=True;save();emit({'complete':True,'published_products':sum(b.get('result',{}).get('published_products',0) for b in state['batches'])})
if __name__=='__main__':asyncio.run(run('--preview-only' in sys.argv))
