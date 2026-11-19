#!/usr/bin/env python3
"""measure_resume_lines.py: estimate how many rendered lines a build_resume_docx.py content list will take.

Usage:  uv run --with fonttools python3 tools/measure_resume_lines.py <content.py> <template.docx>
(fontTools is not installed system-wide; uv pulls it per run.) Uses the Calibri faces Word ships and a greedy word wrap
over the 535pt text width, bullet indent read from the template numbering.xml. A one-page layout with 3 sections,
2 jobs, and 2 projects fits about 50 to 51 line units (General-SWE-Review 2026-09-04 base = 51, Confido 2026-09-07 = 50).
"""
import re, sys, math, zipfile
from fontTools.ttLib import TTFont
D='/Applications/Microsoft Word.app/Contents/Resources/DFonts/'
F={'r':TTFont(D+'Calibri.ttf'),'b':TTFont(D+'Calibrib.ttf'),'i':TTFont(D+'Calibrii.ttf'),'bi':TTFont(D+'Calibriz.ttf')}
CM={k:(f.getBestCmap(), f['hmtx'].metrics, f['head'].unitsPerEm) for k,f in F.items()}
def w(text, style, pt):
    cmap,hm,upm=CM[style]; tot=0
    for ch in text:
        g=cmap.get(ord(ch)) or cmap.get(ord('?')); tot+=hm[g][0]
    return tot*pt/upm
TEXTW=535.0  # 10700 twips
def wrap(segs, avail, pt):
    # segs: list of (text, style); greedy word wrap
    tokens=[]
    for text,style in segs:
        for m in re.finditer(r'\S+\s*', text):
            tok=m.group(0); tokens.append((w(tok.rstrip(),style,pt), w(tok,style,pt)))
    lines=1; cur=0
    for wd,wds in tokens:
        if cur+wd>avail and cur>0: lines+=1; cur=wds
        else: cur+=wds
    return lines, cur
def md(text, base):
    out=[]
    for p in re.split(r'(\*\*.*?\*\*)', text):
        if not p: continue
        if p.startswith('**'): out.append((p[2:-2], 'b' if base=='r' else 'bi'))
        else: out.append((p, base))
    return out
ns={}; exec(open(sys.argv[1],encoding='utf-8').read(), ns)
tpl=sys.argv[2]
num=zipfile.ZipFile(tpl).read('word/numbering.xml').decode()
m=re.search(r'<w:ind w:left="(\d+)" w:hanging="(\d+)"', num); left=int(m.group(1))/20 if m else 18
print('bullet left indent pt', left)
total=0
for kind,*a in ns['CONTENT']:
    if kind=='bullet':
        n,last=wrap(md(a[0],'r'), TEXTW-left, 10); total+=n
        flag=' <-- 3 lines' if n>=3 else ''
        print(f'bullet {n}L last={last:5.0f}pt {a[0][:60]}{flag}' + (' <-- last line > 505pt, budget +1' if last > 505 else ''))
    elif kind=='skill':
        n,last=wrap([(a[0]+': ','b')]+md(a[1],'r'), TEXTW, 10); total+=n
        print(f'skill  {n}L width={w(a[0]+": ","b",10)+sum(w(t,s,10) for t,s in md(a[1],"r")):5.0f}pt {a[0]}' + (' <-- WRAPS' if n>1 else ''))
    elif kind in ('headline','tech','course'):
        pt=10; txt=a[0] if kind!='course' else 'Relevant Coursework: '+a[0]   # 10pt minimum since 2026-09-24
        n,last=wrap([(txt,'i' if kind!='course' else 'r')], TEXTW, pt); total+=n
        print(f'{kind:6} {n}L width={w(txt,"i",pt):5.0f}pt' + (' <-- WRAPS' if n>1 else ''))
    else:
        total+=1
print('TOTAL line units', total)
