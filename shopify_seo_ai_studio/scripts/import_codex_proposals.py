"""Import individually reviewed Codex proposals, without calling a local AI or Shopify.

Input files are local work artifacts, not a public API. Every product remains unapproved.
"""
import argparse
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
from content_safety import clean_html,factual_issues

RUN=app.DATA/'codex_catalog_run'


def source_index():
    return {r['id']:r for file in (RUN/'sources').glob('batch_*.json') for r in app.jload(file.read_text(),[])}


def import_file(path,sources=None,run_label="codex_catalog_run"):
    records=app.jload(path.read_text(),None)
    if not isinstance(records,list):raise ValueError(f'Invalid proposal file: {path.name}')
    sources=sources or source_index()
    result={'generated':0,'blocked':0,'unchanged':0}
    prompt_version=app.ensure_prompt_version();settings=app.get_settings()
    for record in records:
        pid=record['id'];source=sources[pid]
        with app.connect() as c:
            row=c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
            previous=c.execute("SELECT 1 FROM generations WHERE product_id=? AND model='Codex' AND output_json=?",(pid,app.jdump(record.get('output',{})))).fetchone()
            if previous:result['unchanged']+=1;continue
        if not row:raise ValueError(f'Missing Shopify product {pid}')
        if app.content_of(row)!=source['current']:raise ValueError(f'Shopify snapshot changed for {pid}')
        imported=app.supplier_for(pid)
        if imported.get('source_hash')!=source.get('source_hash'):raise ValueError(f'Source changed for {pid}')
        proposal=app.Proposal.model_validate(record['output']).model_dump()
        proposal['descriptionHtml']=clean_html(proposal['descriptionHtml'])
        issues=list(record.get('blocking_issues',[]))
        has_source=bool(imported.get('data',{}).get('title','').strip())
        if not has_source:issues.append('Fonte Autofantasy assente: verifica tecnica necessaria.')
        else:issues+=factual_issues(proposal,app.technical_evidence(imported))
        if record.get('reviewed') is not True:issues.append('Revisione individuale Codex assente.')
        issues=list(dict.fromkeys(issues))
        score=app.score_content(proposal,settings.weights,{'accuracy':0 if issues else .75})['score']
        proposal['voto']=score
        proposal['criticita']=list(dict.fromkeys(proposal['criticita']+issues))
        status='blocked' if issues else 'generated';gid=str(app.uuid.uuid4())
        with app.connect() as c:
            previous=c.execute("SELECT output_json FROM generations WHERE product_id=? AND model='Codex' ORDER BY created_at DESC,rowid DESC LIMIT 1",(pid,)).fetchone()
            if previous and app.jload(previous[0],{})==proposal:result['unchanged']+=1;continue
            if run_label!='codex_catalog_run':
                latest=c.execute("SELECT g.id,g.output_json,s.validation_json FROM generations g LEFT JOIN generation_sources s ON s.generation_id=g.id WHERE g.product_id=? ORDER BY g.created_at DESC,g.rowid DESC LIMIT 1",(pid,)).fetchone()
                if latest:
                    before=app.jload(latest['output_json'],{})
                    prior_issues=app.jload(latest['validation_json'],{}).get('issues',[])
                    if all(before.get(k)==proposal.get(k) for k in ('title','descriptionHtml','metaTitle','metaDescription','keywords')) and set(prior_issues)==set(issues):
                        result['unchanged']+=1;continue
                c.execute("DELETE FROM approvals WHERE generation_id IN (SELECT id FROM generations WHERE product_id=? AND status='approved')",(pid,))
                c.execute("UPDATE generations SET status='superseded' WHERE product_id=? AND status='approved'",(pid,))
            if not c.execute('SELECT 1 FROM shopify_snapshots WHERE product_id=? AND content_hash=?',(pid,row['content_hash'])).fetchone():
                c.execute('INSERT INTO shopify_snapshots VALUES(?,?,?,?,?,?)',(str(app.uuid.uuid4()),pid,app.jdump(app.content_of(row)),row['content_hash'],app.now(),'codex_generation'))
            c.execute('INSERT INTO generations(id,product_id,prompt_version,model,output_json,score,status,source_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)',(gid,pid,prompt_version,'Codex',app.jdump(proposal),score,status,row['content_hash'],app.now()))
            validation={'issues':issues,'human_review_required':True,'engine':'codex','reviewed_in_batch':record.get('reviewed') is True,'method':'Codex: regole editoriali per famiglie, controllo delle anomalie e verifiche documentali','allow_title_only':imported['status']=='incomplete' and has_source,'source_limitations':record.get('source_limitations',[]),'run':run_label,'editorial_review':record.get('editorial_review',{}),'keyword_strategy':record.get('keyword_strategy',{})}
            c.execute('INSERT INTO generation_sources VALUES(?,?,?,?)',(gid,imported.get('source_hash',''),app.jdump(imported),app.jdump(validation)))
            c.execute("UPDATE products SET processing_status=?,last_error=NULL WHERE id=?",('needs_review' if issues else 'to_approve',pid))
        result[status]+=1
    return result

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--run-label',default='codex_catalog_run');parser.add_argument('files',nargs='+',type=Path);args=parser.parse_args()
    sources=source_index()
    for path in args.files:print(path.name,import_file(path,sources,args.run_label),flush=True)
