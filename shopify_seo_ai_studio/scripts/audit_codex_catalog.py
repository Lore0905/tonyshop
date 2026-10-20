"""Offline audit of the complete proposed catalog against captured supplier sources."""
import sys,json,collections
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
from content_safety import factual_issues,clean_html
from import_codex_proposals import RUN,source_index
OUT=Path(sys.argv[1]) if len(sys.argv)>1 else RUN
sources=source_index();seen=set();failures=[];blocked=[];titles=collections.defaultdict(list);meta=collections.defaultdict(list);lengths=collections.Counter()
for file in sorted((OUT/'proposals').glob('batch_*.json')):
    expected={r['id'] for r in app.jload((RUN/'sources'/file.name).read_text(),[])}
    records=app.jload(file.read_text(),[])
    if {r['id'] for r in records}!=expected:failures.append({'file':file.name,'error':'ID mancanti o estranei al lotto'})
    for record in records:
        pid=record['id'];out=record['output'];source=sources[pid]
        if pid in seen:failures.append({'id':pid,'error':'ID duplicato'})
        seen.add(pid)
        try:app.Proposal.model_validate(out)
        except Exception as exc:failures.append({'id':pid,'file':file.name,'error':str(exc)});continue
        if source['supplier'].get('title'):
            issues=factual_issues(out,app.technical_evidence({'data':source['supplier']}))
            if issues:failures.append({'id':pid,'file':file.name,'error':issues})
        if record.get('blocking_issues'):blocked.append({'id':pid,'issues':record['blocking_issues']})
        if out['metaTitle'].strip():titles[out['metaTitle'].casefold()].append(pid)
        else:lengths['meta_title_missing']+=1
        if out['metaDescription'].strip():meta[out['metaDescription'].casefold()].append(pid)
        else:lengths['meta_description_missing']+=1
        if len(out['metaTitle'])>60:lengths['meta_title_over_60']+=1
        if len(out['metaDescription'])>160:lengths['meta_description_over_160']+=1
        if clean_html(out['descriptionHtml'])!=out['descriptionHtml']:lengths['html_to_sanitize']+=1
report={'total_sources':len(sources),'proposals':len(seen),'missing':sorted(set(sources)-seen),'failures':failures,'blocked':blocked,'length_warnings':dict(lengths),'duplicate_meta_title_groups':sum(len(ids)>1 for ids in titles.values()),'duplicate_meta_description_groups':sum(len(ids)>1 for ids in meta.values())}
(OUT/'audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps({k:v if k not in ('failures','blocked','missing') else len(v) for k,v in report.items()},ensure_ascii=False))
for row in failures[:30]:print(json.dumps(row,ensure_ascii=False))
