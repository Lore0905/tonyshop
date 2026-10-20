"""Conservative HTML allowlist and factual checks, independent from model score."""
import re
from html import escape, unescape
from html.parser import HTMLParser
from seo import plain


class Cleaner(HTMLParser):
    allowed = {'p', 'br', 'h2', 'h3', 'ul', 'ol', 'li', 'strong', 'em', 'b', 'i', 'table', 'thead', 'tbody', 'tr', 'td', 'th'}
    void = {'br'}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []; self.skip = 0; self.stack = []
    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'iframe', 'object', 'svg', 'math', 'template'}:
            self.skip += 1
        elif not self.skip and tag in self.allowed:
            self.parts.append(f'<{tag}>')
            if tag not in self.void: self.stack.append(tag)
    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'iframe', 'object', 'svg', 'math', 'template'}:
            self.skip = max(0, self.skip - 1)
        elif not self.skip and tag in self.stack:
            while self.stack:
                current = self.stack.pop(); self.parts.append(f'</{current}>')
                if current == tag: break
    def handle_data(self, data):
        if not self.skip: self.parts.append(escape(data))


def clean_html(value):
    parser = Cleaner(); parser.feed(value or ''); parser.close()
    return ''.join(parser.parts) + ''.join(f'</{t}>' for t in reversed(parser.stack))


def factual_text(value):
    """Normalize presentation equivalents, never values, units or model codes."""
    # Inline markup must not split a documented code (MW<strong>-</strong>8502).
    value=re.sub(r"</?(?:div|section|article|header|footer|h1|h4|h5|h6|dl|dt|dd|p|br|h2|h3|ul|ol|li|table|thead|tbody|tr|td|th)\b[^>]*>","\n",value,flags=re.I)
    value=clean_html(value)
    value=re.sub(r"</?(?:p|br|h2|h3|ul|ol|li|table|thead|tbody|tr|td|th)\b[^>]*>"," ",value,flags=re.I)
    value=unescape(re.sub(r"<[^>]+>","",value))
    value=re.sub(r"(?<![\w-])0*(\d+)[ \t]*(?:pz|pezzi|pezzo)\b(?![ \t]+per\b)",lambda m:str(int(m[1]))+"pz",value,flags=re.I)
    value=re.sub(r"(?<![\w-])0*(\d+)[ \t]*(?:cp)\b",lambda m:str(int(m[1]))+"cp",value,flags=re.I)
    value=re.sub(r"\b([24])\s*(?:tempi|t)\b",lambda m:m[1]+"t",value,flags=re.I)
    value=re.sub(r"\b(\d+(?:[.,]\d+)?)[ \t]*(cc|mm|cm|km|kw|w|v|ah|a|lm|k|hz|bar|kg|rpm|cv|hp)\b",lambda m:m[0] if m[2].lower()=="a" and re.search(r"\s",m[0]) and (m[2]=="a" or re.match(r"[ \t]*\d",m.string[m.end():])) else m[1]+m[2].lower(),value,flags=re.I)
    value=re.sub(r"(?<=\d)\s*[x×]\s*(?=\d)","x",value,flags=re.I)
    value=re.sub(r"\b(\d+(?:[.,]\d+)?(?:x\d+(?:[.,]\d+)?)+)\s+(mm|cm)\b",lambda m:m[1]+m[2].lower(),value,flags=re.I)
    return re.sub(r"\s+"," ",value).strip()


def factual_issues(proposal, evidence):
    source = factual_text(evidence).casefold()
    text = factual_text('\n|\n'.join(str(proposal.get(k, '')) for k in ['title', 'descriptionHtml', 'metaTitle', 'metaDescription']))
    issues = []
    # Numbers, dimensions, model codes and acronyms must be documented verbatim.
    tokens = re.findall(r'\b[\w.,/+\-]*\d[\w.,/+\-]*\b|\b[A-Z][A-Z0-9-]{2,}\b', text)
    for token in sorted(set(tokens)):
        if not re.search(r'(?<!\w)' + re.escape(token.casefold()) + r'(?!\w)', source):
            dimension=re.fullmatch(r"(\d+(?:[.,]\d+)?)(mm|cm|cc)",token.casefold())
            if dimension and re.search(r"(?<!\w)"+re.escape(dimension[1])+r"\s+"+dimension[2]+r"(?!\w)",source):
                continue
            quantity=re.fullmatch(r"(\d+)pz",token.casefold())
            if quantity and re.search(r"pezzi[ \t]+per[ \t]+confezione\s*:?\s*"+re.escape(quantity[1])+r"(?!\d)",source):
                continue
            issues.append(f'Dato tecnico non documentato: {token}')
    for claim in ['universale', 'originale', 'omologato', 'certificato', 'garantito', 'spedizione gratuita', 'consegna in', 'disponibile', 'in stock']:
        if claim in text.casefold() and claim not in source:
            issues.append(f'Affermazione non documentata: {claim}')
    if not plain(proposal.get('title', '')) or not plain(proposal.get('descriptionHtml', '')):
        issues.append('Titolo o descrizione vuoti.')
    return issues
