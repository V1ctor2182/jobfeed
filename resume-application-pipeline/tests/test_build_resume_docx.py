import html
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.create_example_template import write_template

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / 'tools' / 'build_resume_docx.py'

# One entry of every kind that has a right-aligned date or location column.
RIGHT_COLUMN = {
    'degree': 'Aug 2023 – Expected Dec 2026',
    'company': 'Example City, ST',
    'role': 'Jun 2026 – Present',
    'job': 'Example City, Country  |  May 2025 – Aug 2025',
    'proj': 'May 2026 – Present',
}
CONTENT = f'''CONTENT = [
    ('section', 'EDUCATION'),
    ('degree', 'Example University — B.S. Computer Engineering', {RIGHT_COLUMN['degree']!r}),
    ('section', 'WORK EXPERIENCE'),
    ('company', 'Example Software LLC', {RIGHT_COLUMN['company']!r}, 22),
    ('role', 'Software Engineer', {RIGHT_COLUMN['role']!r}, 0),
    ('job', 'Example Systems Ltd.', 'Software Engineer Intern', {RIGHT_COLUMN['job']!r}, 51),
    ('section', 'PROJECTS'),
    ('proj', 'Jobfeed — Open-Source Job Discovery and Evaluation Pipeline', {RIGHT_COLUMN['proj']!r}, 22),
]
'''


def build(tmp, content, out):
    """Build from the shipped clean template, not the developer's ignored local templates/resume.docx."""
    template = Path(tmp) / 'template.docx'
    write_template(template)
    subprocess.run([sys.executable, str(BUILDER), str(template), str(content), str(out)],
                   check=True, capture_output=True)


def right_column_rpr(document_xml, text):
    """Return the run properties of the run that holds a right-aligned date or location."""
    pattern = (r'<w:r><w:rPr>((?:(?!</w:rPr>).)*)</w:rPr>(?:<w:tab/>)?<w:t[^>]*>\t?'
               + re.escape(html.escape(text, quote=False)) + r'</w:t></w:r>')
    match = re.search(pattern, document_xml)
    if match is None:
        raise AssertionError(f'no run found for {text!r}')
    return match.group(1)


class RightColumnStyleTest(unittest.TestCase):
    def test_every_date_column_uses_one_style(self):
        with tempfile.TemporaryDirectory() as tmp:
            content = Path(tmp) / 'content.py'
            content.write_text(CONTENT, encoding='utf-8')
            out = Path(tmp) / 'out.docx'
            build(tmp, content, out)
            document_xml = zipfile.ZipFile(out).read('word/document.xml').decode('utf-8')

        styles = {kind: right_column_rpr(document_xml, text) for kind, text in RIGHT_COLUMN.items()}
        reference = styles['degree']
        # Grey 10pt italic, with <w:i/> before <w:color/> as the OOXML schema requires;
        # Word drops italic that comes after <w:sz/>.
        self.assertIn('<w:i/><w:iCs/><w:color w:val="6B7280"/><w:sz w:val="20"/>', reference)
        for kind, rpr in styles.items():
            self.assertEqual(rpr, reference, f'{kind} date column is styled differently from the degree date')


class MinimumFontSizeTest(unittest.TestCase):
    def test_no_text_below_10pt(self):
        # Candidate 2026-09-24: the smallest font on the page is 10pt (w:sz is in half-points, so 20).
        full = ROOT / 'bases' / 'general' / 'content.py'
        extra = '''CONTENT += [
    ('headline', 'Software Engineer building production AI systems'),
    ('course', 'Data Structures & Algorithms, Computer Architecture'),
]
'''
        with tempfile.TemporaryDirectory() as tmp:
            content = Path(tmp) / 'content.py'
            content.write_text(full.read_text(encoding='utf-8') + extra, encoding='utf-8')
            out = Path(tmp) / 'out.docx'
            build(tmp, content, out)
            document_xml = zipfile.ZipFile(out).read('word/document.xml').decode('utf-8')
        sizes = [int(v) for v in re.findall(r'<w:sz w:val="(\d+)"/>', document_xml)]
        self.assertTrue(sizes)
        self.assertGreaterEqual(min(sizes), 20, f'font sizes in half-points: {sorted(set(sizes))}')


class ContactLineTest(unittest.TestCase):
    def test_separators_are_single_spaced_so_the_header_fits_at_10pt(self):
        # At 10pt the base contact line with "  |  " separators is 557pt wide and wraps (text width 535pt);
        # with " | " it is 534.4pt. Content stays verbatim, only the separator spacing changes.
        contact = ('US Citizen  |  Open to relocation  |  202.555.0100  |  [candidate@example.com](mailto:candidate@example.com)'
                   '  |  [linkedin.com/in/example-candidate](https://www.linkedin.com/in/example-candidate/)'
                   '  |  [github.com/example-candidate](https://github.com/example-candidate)')
        with tempfile.TemporaryDirectory() as tmp:
            content = Path(tmp) / 'content.py'
            content.write_text(f'CONTENT = [(\'contact\', {contact!r})]\n', encoding='utf-8')
            out = Path(tmp) / 'out.docx'
            build(tmp, content, out)
            document_xml = zipfile.ZipFile(out).read('word/document.xml').decode('utf-8')
        text = ''.join(re.findall(r'<w:t[^>]*>([^<]*)</w:t>', document_xml))
        self.assertEqual(text, 'US Citizen | Open to relocation | 202.555.0100 | candidate@example.com'
                               ' | linkedin.com/in/example-candidate | github.com/example-candidate')


if __name__ == '__main__':
    unittest.main()
