#!/usr/bin/env python3
"""build_resume_docx.py — 从内容清单整页重建简历 docx，段落 / run 的 XML 与定稿模板逐字一致。

适用：需要整体重排（换段落顺序、换项目组合、skills 整组换血）的版本；只改几处文字时仍按 CLAUDE.md
的"解包 → 改 document.xml → 重打包"做。

用法：
    python3 tools/build_resume_docx.py <模板.docx> <内容.py> <输出.docx>

<内容.py> 只需定义一个 CONTENT 列表，每项是 (kind, *args)，kind 对应下面的 p_* 函数：
    ('name', 'Example Candidate')
    ('contact', '202.555.0100  |  [candidate@example.com](mailto:candidate@example.com)  |  [github.com/example-candidate](https://github.com/example-candidate)')   # [text](url) = 超链接
    ('headline', 'Software Engineer — ...')
    ('section', 'EDUCATION')
    ('degree', 'Example University — B.S. Computer Engineering', 'Aug 2023 – Dec 2026')
    ('course', 'Computer Architecture, ...')
    ('skill', 'Languages', 'Python, **Java**, ...')            # **加粗**
    ('text', 'Seeking a ... role ...')                          # 普通正文段（OBJECTIVE 段正文）
    ('job', 'Example Software LLC', 'Software Engineer', 'Example City, ST  |  Mar 2026 – Present', 22)   # 末位 = spacing before（首段 22，其余 51）
    ('company', 'Example Software LLC', 'Example City, ST', 22)          # 多职位雇主：公司行 + 若干 role 行
    ('role', 'Software Engineer', 'Jun 2026 – Present', 0)      # 第 2 个及以后的 role 用 51
    ('proj', 'Jobfeed — ...', 'May 2026 – Present', 22)
    ('tech', 'Python, FastAPI, ...')
    ('bullet', 'Built ... **1,000+ users** ...')
模板 docx 只借 document.xml 的头部与其余 zip 条目（styles / numbering / fonts），按原 zip 顺序重建。
"""
import re, zipfile, sys, html

# Minimum font size is 10pt, w:sz 20 in half-points (Candidate 2026-09-24); tests/test_build_resume_docx.py enforces it.

FONT = '<w:rFonts w:ascii="Calibri" w:cs="Calibri" w:eastAsia="Calibri" w:hAnsi="Calibri"/>'

HYPERLINKS = []   # (rId, url) collected while building paragraphs; written into document.xml.rels

def runs_links(text, color='4B5462', sz=20):
    """Contact-line runs; `[text](url)` becomes a clickable hyperlink (same color, underlined)."""
    out = []
    for part in re.split(r'(\[[^\]]+\]\([^)]+\))', text):
        if not part: continue
        m = re.fullmatch(r'\[([^\]]+)\]\(([^)]+)\)', part)
        if m:
            rid = f'rId{100 + len(HYPERLINKS)}'
            HYPERLINKS.append((rid, m.group(2)))
            rpr = FONT + f'<w:color w:val="{color}"/><w:sz w:val="{sz}"/><w:szCs w:val="{sz}"/><w:u w:val="single"/>'
            out.append(f'<w:hyperlink r:id="{rid}"><w:r><w:rPr>{rpr}</w:rPr><w:t xml:space="preserve">{esc(m.group(1))}</w:t></w:r></w:hyperlink>')
        else:
            out.append(run(part, color=color, sz=sz))
    return ''.join(out)

def esc(t):
    return html.escape(t, quote=False)

def run(text, bold=False, italic=False, color='111111', sz=20, tab=False, bold_first=False):
    rpr = FONT
    if bold_first and bold: rpr += '<w:b/><w:bCs/>'
    if italic: rpr += '<w:i/><w:iCs/>'
    rpr += f'<w:color w:val="{color}"/><w:sz w:val="{sz}"/><w:szCs w:val="{sz}"/>'
    if bold and not bold_first: rpr += '<w:b/><w:bCs/>'
    t = '<w:tab/>' if tab else ''
    t += f'<w:t xml:space="preserve">{esc(text)}</w:t>'
    return f'<w:r><w:rPr>{rpr}</w:rPr>{t}</w:r>'

def runs_md(text, color='111111', sz=20, italic=False):
    out = []
    for p in re.split(r'(\*\*.*?\*\*)', text):
        if not p: continue
        if p.startswith('**') and p.endswith('**'):
            out.append(run(p[2:-2], bold=True, color=color, sz=sz, italic=italic))
        else:
            out.append(run(p, color=color, sz=sz, italic=italic))
    return ''.join(out)

def p_name(t):
    return ('<w:p><w:pPr><w:spacing w:after="14" w:line="240" w:lineRule="auto"/><w:jc w:val="center"/></w:pPr>'
            + run(t, bold=True, color='111111', sz=40, bold_first=True) + '</w:p>')

def p_contact(t):
    t = t.replace('  |  ', ' | ')   # single-spaced separators so the base line fits one line at 10pt (557pt -> 534.4pt of 535)
    return ('<w:p><w:pPr><w:spacing w:after="8" w:line="240" w:lineRule="auto"/><w:jc w:val="center"/></w:pPr>'
            + runs_links(t, color='4B5462', sz=20) + '</w:p>')

def p_headline(t):
    return ('<w:p><w:pPr><w:spacing w:after="12" w:line="240" w:lineRule="auto"/><w:jc w:val="center"/></w:pPr>'
            + run(t, italic=True, color='4B5462', sz=20) + '</w:p>')

def p_section(t):
    return ('<w:p><w:pPr><w:pBdr><w:top w:val="single" w:color="BCC1C9" w:sz="6" w:space="4"/></w:pBdr>'
            '<w:spacing w:after="45" w:before="99" w:line="240" w:lineRule="auto"/></w:pPr>'
            + run(t, bold=True, color='232F3D', sz=21, bold_first=True) + '</w:p>')

def p_degree(left, right):
    return ('<w:p><w:pPr><w:tabs><w:tab w:val="right" w:pos="10700"/></w:tabs><w:spacing w:after="7" w:line="240" w:lineRule="auto"/></w:pPr>'
            + run(left, bold=True, color='111111', sz=20, bold_first=True)
            + f'<w:r><w:rPr>{FONT}<w:i/><w:iCs/><w:color w:val="6B7280"/><w:sz w:val="20"/><w:szCs w:val="20"/></w:rPr><w:tab/><w:t>{esc(right)}</w:t></w:r></w:p>')

def p_course(t):
    return ('<w:p><w:pPr><w:spacing w:after="3" w:line="240" w:lineRule="auto"/></w:pPr>'
            + run('Relevant Coursework: ', italic=True, color='4B5462', sz=20)
            + run(t, color='4B5462', sz=20) + '</w:p>')

def p_text(t):
    """Plain body paragraph (10pt, same spacing as a skills line). Used for the OBJECTIVE section body."""
    return ('<w:p><w:pPr><w:spacing w:after="4" w:line="240" w:lineRule="auto"/></w:pPr>'
            + runs_md(t, color='111111', sz=20) + '</w:p>')

def p_skill(label, t):
    return ('<w:p><w:pPr><w:spacing w:after="4" w:line="240" w:lineRule="auto"/></w:pPr>'
            + run(label + ': ', bold=True, color='111111', sz=20)
            + runs_md(t, color='111111', sz=20) + '</w:p>')

def p_job(company, title, right, before=22):
    return (f'<w:p><w:pPr><w:tabs><w:tab w:val="right" w:pos="10700"/></w:tabs><w:spacing w:after="19" w:before="{before}" w:line="240" w:lineRule="auto"/></w:pPr>'
            + run(company, bold=True, color='111111', sz=20, bold_first=True)
            + run(' — ' + title, color='4B5462', sz=20)
            + run('\t' + right, italic=True, color='6B7280', sz=20) + '</w:p>')

def p_company(name, right, before=22):
    """Company header for a multi-role employer: bold company name, right-aligned italic location. Follow with p_role lines."""
    return (f'<w:p><w:pPr><w:tabs><w:tab w:val="right" w:pos="10700"/></w:tabs><w:spacing w:after="19" w:before="{before}" w:line="240" w:lineRule="auto"/></w:pPr>'
            + run(name, bold=True, color='111111', sz=20, bold_first=True)
            + run('\t' + right, italic=True, color='6B7280', sz=20) + '</w:p>')

def p_role(title, right, before=0):
    """Role line under p_company: title in the same grey as a job title, right-aligned italic dates. before=51 for the 2nd+ role."""
    return (f'<w:p><w:pPr><w:tabs><w:tab w:val="right" w:pos="10700"/></w:tabs><w:spacing w:after="19" w:before="{before}" w:line="240" w:lineRule="auto"/></w:pPr>'
            + run(title, color='4B5462', sz=20)
            + run('\t' + right, italic=True, color='6B7280', sz=20) + '</w:p>')

def p_proj(title, right, before=22):
    return (f'<w:p><w:pPr><w:tabs><w:tab w:val="right" w:pos="10700"/></w:tabs><w:spacing w:after="19" w:before="{before}" w:line="240" w:lineRule="auto"/></w:pPr>'
            + run(title, bold=True, color='111111', sz=20)
            + f'<w:r><w:rPr>{FONT}<w:i/><w:iCs/><w:color w:val="6B7280"/><w:sz w:val="20"/><w:szCs w:val="20"/></w:rPr><w:tab/><w:t xml:space="preserve">{esc(right)}</w:t></w:r></w:p>')

def p_tech(t):
    return ('<w:p><w:pPr><w:spacing w:after="20" w:line="240" w:lineRule="auto"/></w:pPr>'
            + runs_md(t, color='4B5462', sz=20, italic=True) + '</w:p>')

def p_bullet(t):
    return ('<w:p><w:pPr><w:pStyle w:val="ListParagraph"/><w:numPr><w:ilvl w:val="0"/><w:numId w:val="2"/></w:numPr>'
            '<w:spacing w:after="19" w:line="248" w:lineRule="auto"/></w:pPr>'
            + runs_md(t, color='111111', sz=20) + '</w:p>')

SECT = ('<w:sectPr><w:pgSz w:w="12240" w:h="15840" w:orient="portrait"/>'
        '<w:pgMar w:top="720" w:right="770" w:bottom="680" w:left="770" w:header="708" w:footer="708" w:gutter="0"/>'
        '<w:pgNumType/><w:docGrid w:linePitch="360"/></w:sectPr>')

def main():
    import json
    from pathlib import Path
    from content_autofill import convert_tuples
    if len(sys.argv) != 4:
        print(__doc__); sys.exit(2)
    base, content_py, out = sys.argv[1:]
    ns = {}
    exec(open(content_py, encoding='utf-8').read(), ns)
    autofill_data = convert_tuples(ns['CONTENT'])
    paras = [globals()['p_' + kind](*args) for kind, *args in ns['CONTENT']]
    zin = zipfile.ZipFile(base)
    doc = zin.read('word/document.xml').decode('utf-8')
    newdoc = doc[:doc.find('<w:body>') + 8] + ''.join(paras) + SECT + '</w:body></w:document>'
    with zipfile.ZipFile(out, 'w') as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == 'word/document.xml':
                data = newdoc.encode('utf-8')
            elif info.filename == 'word/_rels/document.xml.rels' and HYPERLINKS:
                rels = ''.join(f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="{html.escape(url, quote=True)}" TargetMode="External"/>' for rid, url in HYPERLINKS)
                # A template that was itself built by this script already carries rId100+ hyperlink rels;
                # drop them first, or Word reports the file as corrupted (duplicate relationship Ids).
                text = re.sub(r'<Relationship Id="rId1\d\d" [^>]*relationships/hyperlink"[^>]*/>', '', data.decode('utf-8'))
                data = text.replace('</Relationships>', rels + '</Relationships>').encode('utf-8')
            zout.writestr(info, data, compress_type=info.compress_type)
    Path(out).with_suffix('.autofill.json').write_text(json.dumps(autofill_data,indent=2,ensure_ascii=False)+'\n')
    print('wrote', out, len(paras), 'paragraphs')

if __name__ == '__main__':
    main()
