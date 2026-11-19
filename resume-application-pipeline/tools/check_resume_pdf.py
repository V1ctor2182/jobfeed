#!/usr/bin/env python3
"""check_resume_pdf.py: mechanical checks on a built resume before it is delivered.

Usage:
    python3 tools/check_resume_pdf.py <resume.pdf> --content <content.py> [--keywords "a,b,c"] [--keywords-file f]
                                      [--name "Example Candidate"]

Checks (all must pass; exit code 1 on any failure, 0 otherwise):
  pages      pdfinfo reports exactly 1 page
  name       first non-empty text line contains the candidate name
  header     the contact line stays on one line (its last token, GitHub link or email, is on the same text line as US Citizen)
  keywords   every keyword is found (case-insensitive, whitespace-normalized) in the PDF text
  emdash     no em dash (U+2014) inside content.py entries of kind bullet / text / skill / tech / headline / course
             (title kinds degree / job / company / role / proj may keep the structural dash)
  spacing    content.py spacing-before values follow the template rule (first entry after a section 22, later 51; roles 0 then 51)
  numbers    (info only, never fails) lists bold figures that are new on this page, for a quick sanity glance
  repolinks  no github.com/example-candidate/<repo> link in the PDF text (profile link github.com/example-candidate alone is fine)
  fonts      pdffonts shows every font embedded (when pdffonts is available)
  fontsize   the sibling .docx has no text below 10pt (Candidate 2026-09-24)
Prints one line per check so the output can be pasted into tailored.md section 7.
"""
import argparse, re, subprocess, sys, pathlib

def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('pdf')
    ap.add_argument('--content', required=True)
    ap.add_argument('--keywords', default='')
    ap.add_argument('--keywords-file')
    ap.add_argument('--name', default='Example Candidate')
    a = ap.parse_args()
    ok = True
    def report(check, passed, detail=''):
        nonlocal ok
        ok = ok and passed
        print(f"[{'PASS' if passed else 'FAIL'}] {check}{': ' + detail if detail else ''}")

    info = run(['pdfinfo', a.pdf])
    m = re.search(r'Pages:\s+(\d+)', info)
    pages = int(m.group(1)) if m else -1
    report('pages', pages == 1, f'{pages} page(s)')

    layout = run(['pdftotext', '-layout', a.pdf, '-'])
    first = next((l.strip() for l in layout.splitlines() if l.strip()), '')
    report('name', a.name in first, first[:80])

    # header on one line: the contact line (the first line after the name) must carry the last visible token of
    # content.py's contact entry (github link for contact set 1, the email for sets 2 and 3)
    lines = [l.strip() for l in layout.splitlines() if l.strip()]
    contact = lines[1] if len(lines) > 1 else ''
    ns = {}
    exec(pathlib.Path(a.content).read_text(), ns)
    centry = next((e[1] for e in ns.get('CONTENT', ()) if e[0] == 'contact'), '')
    cvis = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', centry)
    last = cvis.split('|')[-1].strip()
    onl = bool(last) and last in contact
    report('header', onl, 'contact line is one line' if onl else f'contact line wrapped; line 2 = "{contact[:70]}"')

    raw = run(['pdftotext', a.pdf, '-'])
    text = re.sub(r'\s+', ' ', raw).lower()
    kws = [k.strip() for k in a.keywords.split(',') if k.strip()]
    if a.keywords_file:
        kws += [l.strip() for l in pathlib.Path(a.keywords_file).read_text().splitlines() if l.strip() and not l.startswith('#')]
    missing = [k for k in kws if re.sub(r'\s+', ' ', k).lower() not in text]
    report('keywords', not missing, f'{len(kws) - len(missing)}/{len(kws)} found' + (f'; MISSING: {missing}' if missing else ''))

    ns = {}
    exec(pathlib.Path(a.content).read_text(), ns)
    bad = []
    for item in ns.get('CONTENT', []):
        kind = item[0]
        if kind in ('bullet', 'text', 'headline', 'course', 'tech'):
            if '—' in item[1]: bad.append((kind, item[1][:50]))
        elif kind == 'skill':
            if '—' in item[2] or '—' in item[1]: bad.append((kind, item[1]))
    report('emdash', not bad, 'none in body text' if not bad else f'found in {bad}')

    # spacing-before rule: first job/company/proj after a section heading uses 22, later ones 51; first role 0, later roles 51
    sp_bad = []; first_in_section = True; first_role = True
    for item in ns.get('CONTENT', []):
        k = item[0]
        if k == 'section': first_in_section = True; continue
        if k in ('job', 'company', 'proj'):
            want = 22 if first_in_section else 51
            if item[-1] != want: sp_bad.append((k, item[1][:30], item[-1], want))
            first_in_section = False; first_role = True
        elif k == 'role':
            want = 0 if first_role else 51
            if item[-1] != want: sp_bad.append((k, item[1][:30], item[-1], want))
            first_role = False
    report('spacing', not sp_bad, 'job/proj/role spacing follows the 22/51 rule' if not sp_bad else f'wrong spacing (kind, title, got, want): {sp_bad}')


    # numbers: every bold span containing a digit must appear in master.md (a fact on file) or in this folder's tailored.md (a flagged estimate)
    master = pathlib.Path('master.md').read_text() if pathlib.Path('master.md').exists() else ''
    tmd = pathlib.Path(a.content).with_name('tailored.md')
    tail = tmd.read_text() if tmd.exists() else ''
    spans = []
    for item in ns.get('CONTENT', []):
        if item[0] in ('bullet', 'tech', 'text'):
            spans += [m for m in re.findall(r'\*\*([^*]+)\*\*', item[1]) if re.search(r'\d', m)]
    unreg = [sp for sp in spans if sp not in master and sp not in tail]
    print(f"[INFO] numbers: {len(spans)} bold figures; new on this page (not in master.md or tailored.md): {unreg or 'none'}")

    links = re.findall(r'github\.com/example-candidate/\S+', raw)
    report('repolinks', not links, 'none' if not links else str(links))

    fonts = run(['pdffonts', a.pdf])
    if fonts:
        rows = [l for l in fonts.splitlines()[2:] if l.strip()]
        notemb = [l for l in rows if re.search(r'\s(no)\s', l)]
        report('fonts', not notemb, f'{len(rows)} fonts, all embedded' if not notemb else f'not embedded: {notemb}')

    # font size: nothing smaller than 10pt (Candidate 2026-09-24); read the .docx Word converted, sizes are half-points
    docx = pathlib.Path(a.pdf).with_suffix('.docx')
    if docx.exists():
        import zipfile
        xml = zipfile.ZipFile(docx).read('word/document.xml').decode('utf-8')
        sizes = sorted({int(v) for v in re.findall(r'<w:sz w:val="(\d+)"/>', xml)})
        small = [v / 2 for v in sizes if v < 20]
        report('fontsize', bool(sizes) and not small, f'smallest font {sizes[0] / 2:g}pt' if not small else f'text below 10pt: {small}pt; rebuild with tools/build_resume_docx.py')
    sys.exit(0 if ok else 1)

if __name__ == '__main__':
    main()
