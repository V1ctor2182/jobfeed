"""estimate_resume_height.py — 用 Word 自带的 Calibri 字体度量，估算 build_resume_docx.py 的 content.py 渲染后的行数与总高度，
不用驱动 Word 就能判断能不能装进一页。

用法：
    <venv-with-fonttools>/bin/python tools/estimate_resume_height.py applications/<版本>/content.py

需要 fontTools（系统 python 是 PEP 668 保护的，建议 `python3 -m venv .venv && .venv/bin/pip install fonttools`）。
字体从 Word 的 DFonts 目录读取，与 docx2pdf.sh 的渲染引擎一致。

标定（2026-09-07 Google 1yr 版实测）：
- 估算器给 General-SWE-Review 定稿算出 683.9pt，该 PDF 实际最后一行 yMax=728.7pt（可用到 758pt）。
- 经验阈值：总高度 ≤ 700pt 稳一页；710pt 以上必溢出。每输出行标 "<-- 2 lines / 3 lines" 的段落是超行元凶。
- 单行 skills 行安全宽度 ≤ 520pt（满宽 535pt）；单行 bullet 宽度比 ≤ 0.95。"""
import re, sys
from fontTools.ttLib import TTFont

FD = '/Applications/Microsoft Word.app/Contents/Resources/DFonts/'
FONTS = {}
for key, fn in [('r', 'Calibri.ttf'), ('b', 'Calibrib.ttf'), ('i', 'Calibrii.ttf'), ('bi', 'Calibriz.ttf')]:
    f = TTFont(FD + fn)
    FONTS[key] = (f.getGlyphSet(), f['cmap'].getBestCmap(), f['head'].unitsPerEm, f['hhea'])

def width(text, pt, style='r'):
    gs, cmap, upem, _ = FONTS[style]
    w = 0
    for ch in text:
        g = cmap.get(ord(ch))
        w += gs[g].width if g else gs['.notdef'].width
    return w * pt / upem

PAGE_W = (12240 - 770 * 2) / 20.0   # 535 pt
BULLET_IND = 245 / 20.0            # 12.25 pt left indent

def lines_for(segments, avail):
    """segments: list of (text, pt, style). Greedy word wrap."""
    # build word list with widths (spaces attach to preceding word)
    words = []
    for text, pt, style in segments:
        parts = re.split(r'(\s+)', text)
        buf = ''
        for p in parts:
            if p.isspace():
                buf += p
                words.append((buf, pt, style)); buf = ''
            elif p:
                if buf:
                    words.append((buf, pt, style)); buf = ''
                buf = p
        if buf:
            words.append((buf, pt, style))
    # merge: a word token followed by a space token is one unit (space trailing)
    units = []
    i = 0
    while i < len(words):
        w, pt, st = words[i]
        if i + 1 < len(words) and words[i + 1][0].isspace() and not w.isspace():
            units.append([(w, pt, st), (words[i + 1][0], words[i + 1][1], words[i + 1][2])]); i += 2
        else:
            units.append([(w, pt, st)]); i += 1
    n = 1; cur = 0.0
    for u in units:
        core = sum(width(t, pt, st) for t, pt, st in u if not t.isspace())
        full = sum(width(t, pt, st) for t, pt, st in u)
        if cur + core > avail + 0.01 and cur > 0:
            n += 1; cur = full
        else:
            cur += full
    return n

def md_segments(text, pt, base='r'):
    segs = []
    for p in re.split(r'(\*\*.*?\*\*)', text):
        if not p: continue
        if p.startswith('**') and p.endswith('**'):
            segs.append((p[2:-2], pt, 'b' if base == 'r' else 'bi'))
        else:
            segs.append((p, pt, base))
    return segs

def line_height(pt, mult=1.0):
    _, _, upem, hhea = FONTS['r']
    return (hhea.ascent - hhea.descent + hhea.lineGap) * pt / upem * mult

def estimate(content, verbose=True):
    total = 0.0
    report = []
    for kind, *args in content:
        if kind == 'name':
            n = 1; h = n * line_height(20) + 14 / 20
        elif kind == 'contact':
            n = lines_for([(re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', args[0]).replace('  |  ', ' | '), 10, 'r')], PAGE_W); h = n * line_height(10) + 8 / 20
        elif kind == 'headline':
            n = lines_for([(args[0], 10, 'i')], PAGE_W); h = n * line_height(10) + 12 / 20
        elif kind == 'section':
            n = 1; h = line_height(10.5) + (99 + 45) / 20 + 4 / 20 * 0 + 6 / 8  # border space approx
        elif kind == 'degree':
            n = 1; h = line_height(10) + 7 / 20
        elif kind == 'course':
            n = lines_for([('Relevant Coursework: ', 10, 'i'), (args[0], 10, 'r')], PAGE_W); h = n * line_height(10) + 3 / 20
        elif kind == 'skill':
            n = lines_for([(args[0] + ': ', 10, 'b')] + md_segments(args[1], 10), PAGE_W); h = n * line_height(10) + 4 / 20
        elif kind == 'text':
            n = lines_for(md_segments(args[0], 10), PAGE_W); h = n * line_height(10) + 4 / 20
        elif kind == 'job':
            n = 1; h = line_height(10) + (19 + args[3]) / 20
        elif kind in ('company', 'role'):
            n = 1; h = line_height(10) + (19 + args[2]) / 20
        elif kind == 'proj':
            n = 1; h = line_height(10) + (19 + args[2]) / 20
        elif kind == 'tech':
            n = lines_for(md_segments(args[0], 10, 'i'), PAGE_W); h = n * line_height(10) + 20 / 20
        elif kind == 'bullet':
            n = lines_for(md_segments(args[0], 10), PAGE_W - BULLET_IND); h = n * line_height(10, 248 / 240) + 19 / 20
        else:
            raise ValueError(kind)
        total += h
        label = (args[0] if kind != 'skill' else args[0] + ': ' + args[1])[:70]
        report.append((kind, n, round(h, 1), label))
    if verbose:
        for r in report:
            flag = '  <-- 3 lines' if r[0] == 'bullet' and r[1] >= 3 else ('  <-- 2 lines' if r[0] in ('skill', 'tech', 'headline', 'course', 'text') and r[1] >= 2 else '')
            print(f'{r[0]:8} {r[1]:2d}  {r[2]:6.1f}  {r[3]}{flag}')
    print(f'TOTAL {total:.1f} pt   (usable page height = {(15840 - 720 - 680) / 20:.0f} pt)')
    return total

if __name__ == '__main__':
    ns = {}
    exec(open(sys.argv[1], encoding='utf-8').read(), ns)
    estimate(ns['CONTENT'])
