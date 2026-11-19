#!/usr/bin/env python3
"""build_library.py — rebuild the class template PDFs under bases/.

For every bases/<class>/content.py: build a Dec 2026 and a May 2027 docx with tools/build_resume_docx.py,
convert each to PDF with tools/docx2pdf.sh (one conversion at a time; Word must not be driven by another
session), and run tools/check_resume_pdf.py on the result. A content.py whose degree line already says
"Expected May 2027" (the intern class) gets only the May 2027 variant.

Outputs: bases/<class>/Example_Candidate_Resume_Dec2026.{docx,pdf} and Example_Candidate_Resume_May2027.{docx,pdf}.

Usage:
    python3 tools/build_library.py                 # every class
    python3 tools/build_library.py ai-agent fde    # only these classes
    python3 tools/build_library.py --docx-only     # build docx, skip Word and the checker
"""
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / 'templates/resume.docx'
# degree dates read "'Aug 2023 – Expected Dec 2026'" since 2026-09-24; match on the tail so both forms work
DEC, MAY = "Expected Dec 2026'", "Expected May 2027'"


def variants(text):
    """Yield (tag, content text) for each graduation-date variant of one content.py."""
    if DEC in text:
        yield 'Dec2026', text
        yield 'May2027', text.replace(DEC, MAY)
    elif MAY in text:
        yield 'May2027', text
    else:
        raise SystemExit('content.py has neither Expected Dec 2026 nor Expected May 2027')


def run(cmd):
    """Run a command from the repo root; return (exit code, combined output)."""
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def build_one(folder, tag, text, tmp, docx_only):
    """Build, convert, and check one variant; return an error string or None."""
    src = pathlib.Path(tmp) / f'{folder.name}_{tag}.py'
    src.write_text(text)
    docx = folder / f'Example_Candidate_Resume_{tag}.docx'
    code, out = run([sys.executable, 'tools/build_resume_docx.py', str(TEMPLATE), str(src), str(docx)])
    if code:
        return f'build failed: {out[-300:]}'
    if docx_only:
        return None
    pdf = docx.with_suffix('.pdf')
    code, out = run(['tools/docx2pdf.sh', str(docx), str(pdf)])
    if code or not pdf.exists():
        return f'docx2pdf failed: {out[-300:]}'
    code, out = run([sys.executable, 'tools/check_resume_pdf.py', str(pdf), '--content', str(src)])
    return None if code == 0 else f'check failed: {out[-500:]}'


def main(argv):
    docx_only = '--docx-only' in argv
    names = [a for a in argv if not a.startswith('--')]
    if names:
        folders = [ROOT / 'bases' / n for n in names]
    else:
        folders = sorted(p.parent for p in (ROOT / 'bases').glob('*/content.py'))
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        for folder in folders:
            for tag, text in variants((folder / 'content.py').read_text()):
                err = build_one(folder, tag, text, tmp, docx_only)
                status = 'ok' if err is None else err
                print(f'{folder.name:18s} {tag}: {status}', flush=True)
                if err:
                    failures.append(f'{folder.name} {tag}')
    print(f'done: {len(failures)} failure(s)' + (': ' + ', '.join(failures) if failures else ''))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
