"""Full editorial comparison for the revised local proposals; no network or writes to Shopify."""
import sys,json,re,collections
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
from content_safety import factual_issues
from import_codex_proposals import RUN,source_index
REV=RUN/'revision_v2'
sources=source_index();changed=collections.Counter();flags=collections.defaultdict(list);seen=set();blocked=[];metas=collections.defaultdict(list);descs=collections.defaultdict(list);keyword_counts=collections.Counter()
for file in sorted((REV/'proposals').glob('batch_*.json')):
    old={r['id']:r for r in json.loads((RUN/'proposals'/file.name).read_text())}
    for row in json.loads(file.read_text()):
        pid=row['id'];seen.add(pid);out=row['output'];source=sources[pid]['supplier'];prior=old[pid]
        for key in ('title','descriptionHtml','metaTitle','metaDescription','keywords'):
            if out.get(key)!=prior['output'].get(key):changed[key]+=1
        if prior.get('blocking_issues') and not row.get('blocking_issues'):flags['lost_previous_block'].append(pid)
        if row.get('blocking_issues'):blocked.append(pid)
        if source.get('title'):
            issues=factual_issues(out,app.technical_evidence({'data':source}))
            if issues:flags['factual_issues'].append({'id':pid,'issues':issues})
            for kw in out.get('keywords',[]):
                issues=factual_issues({'title':kw,'descriptionHtml':'Keyword di analisi.'},app.technical_evidence({'data':source}))
                if issues:flags['keyword_fact_issues'].append({'id':pid,'keyword':kw,'issues':issues})
        norm=lambda x:re.sub(r'[^\w]+',' ',app.plain(x).casefold()).strip()
        if norm(out['metaDescription'])==norm(out['title']):flags['snippet_is_title'].append(pid)
        if re.match(r'^0*1\s*(?:pz|cp)\b',out['title'],re.I):flags['quantity_prefix'].append(pid)
        if re.search(r'autofantasy|la fonte|la scheda (?:riporta|indica)',out['descriptionHtml'],re.I):flags['internal_copy'].append(pid)
        if len(out['metaTitle'])>70:flags['long_meta_title'].append(pid)
        if len(out['metaDescription'])>160:flags['long_meta_description'].append(pid)
        if out['metaTitle'].strip():metas[norm(out['metaTitle'])].append(pid)
        if out['metaDescription'].strip():descs[norm(out['metaDescription'])].append(pid)
        keyword_counts[len(out.get('keywords',[]))]+=1
report={'checked_at':app.now(),'total':len(seen),'missing':sorted(set(sources)-seen),'changed_fields':dict(changed),'blocked':len(blocked),'flags':dict(flags),'duplicate_meta_title':{k:v for k,v in metas.items() if len(v)>1},'duplicate_meta_description':{k:v for k,v in descs.items() if len(v)>1},'keyword_count_distribution':dict(keyword_counts),'volume_data':None,'gsc_status':'blocked_subscription_required','method':'Source-grounded editorial revision and public search language, without measured keyword volumes'}
(REV/'editorial_audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps({k:( {a:len(b) for a,b in v.items()} if k=='flags' else len(v) if k.startswith('duplicate_') else v) for k,v in report.items()},ensure_ascii=False))
