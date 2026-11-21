"""Copy the server setup prompt from docs/setup-prompt.txt to where people copy it.

The prompt is written once, in docs/setup-prompt.txt. This puts it in the README's
setup-prompt block and in the website's "Copy setup prompt" button. Edit the text
file, then run:

    python3 scripts/sync_setup_prompt.py           # update README.md and site/index.html
    python3 scripts/sync_setup_prompt.py --check   # exit 1 when a copy differs (CI)
"""

import argparse
import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "setup-prompt.txt"
README = ROOT / "README.md"
SITE = ROOT / "site" / "index.html"

_README_BLOCK = re.compile(
    r"(<!-- setup-prompt:start -->\n```text\n).*?(\n```\n<!-- setup-prompt:end -->)",
    re.DOTALL,
)
_SITE_BUTTON = re.compile(r'(<button class="copy"[^>]*?data-copy=")[^"]*(")', re.DOTALL)


def _replace_once(pattern, text, value, where):
    updated, count = pattern.subn(lambda m: m.group(1) + value + m.group(2), text)
    if count != 1:
        raise SystemExit(f"{where}: expected one setup prompt, found {count}")
    return updated


def synced():
    """README.md and site/index.html as they should read, by path."""
    prompt = SOURCE.read_text(encoding="utf-8").strip("\n")
    button = html.escape(prompt, quote=True).replace("\n", "&#10;")
    return {
        README: _replace_once(_README_BLOCK, README.read_text(encoding="utf-8"), prompt, "README.md"),
        SITE: _replace_once(_SITE_BUTTON, SITE.read_text(encoding="utf-8"), button, "site/index.html"),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="report copies that differ; change nothing")
    args = parser.parse_args(argv)
    stale = [path for path, text in synced().items() if path.read_text(encoding="utf-8") != text]
    if args.check:
        for path in stale:
            print(f"{path.relative_to(ROOT)} differs from docs/setup-prompt.txt; run python3 scripts/sync_setup_prompt.py")
        return 1 if stale else 0
    for path, text in synced().items():
        if path in stale:
            path.write_text(text, encoding="utf-8")
            print(f"updated {path.relative_to(ROOT)}")
    if not stale:
        print("README.md and site/index.html already match docs/setup-prompt.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
