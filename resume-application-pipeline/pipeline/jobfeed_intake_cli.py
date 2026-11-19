"""Add exact job URLs as user priorities without starting applications."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from pipeline.jobfeed_intake import MAX_BATCH_URLS, JobfeedIntake

# Outcomes that leave the posting saved with User priority. Anything else is a failure to report.
OK_STATUSES = frozenset({"queued", "already_queued", "already_applied"})
URL_PROBLEM = "must be an exact public HTTP or HTTPS job URL"


def use_jobfeed(jobfeed_root: Path) -> None:
    """Make the sibling Jobfeed package importable."""
    if (source := str(jobfeed_root / "src")) not in sys.path:
        sys.path.insert(0, source)


def url_problem(url: str) -> str | None:
    """Why url cannot go to the intake, or None. Call use_jobfeed first."""
    from jobfeed.domain.posting_url import PostingURLKey

    try:
        if any(character.isspace() or ord(character) < 32 for character in url):
            raise ValueError("Invalid URL characters")
        PostingURLKey.parse(url)
    except ValueError:
        return URL_PROBLEM
    return None


def configured_db(jobfeed_root: Path, db_path: Path | None = None) -> Path:
    """The Jobfeed database from config.toml unless db_path overrides it. Raises ValueError when it is unusable."""
    if db_path is None:
        from jobfeed.config import load_settings

        try:
            db_path = load_settings(jobfeed_root / "config.toml").db.path
        except (OSError, ValueError):
            raise ValueError("Could not load Jobfeed configuration; repair config.toml or supply --db") from None
    db_path = db_path.expanduser().resolve()
    if not db_path.is_file():
        raise ValueError("Configured Jobfeed database is missing; initialize it separately first")
    return db_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("urls", nargs="*", help="One to twenty exact job posting URLs")
    parser.add_argument("--file", type=Path, help="JSON array of URLs; use - for stdin")
    parser.add_argument(
        "--observed-posting", type=Path,
        help="One browser-observed LinkedIn Easy Apply posting as JSON; use - for stdin",
    )
    parser.add_argument("--db", type=Path, help="Override the configured Jobfeed database")
    parser.add_argument(
        "--jobfeed-root", type=Path,
        default=Path(__file__).resolve().parents[2] / "jobfeed",
    )
    parser.add_argument("--jobfeed-python", type=Path)
    args = parser.parse_args(argv)

    if sum((bool(args.urls), args.file is not None, args.observed_posting is not None)) > 1:
        parser.error("Use URL arguments, --file, or --observed-posting, not more than one")
    observed = None
    try:
        if args.observed_posting is not None:
            observed = (json.load(sys.stdin) if args.observed_posting == Path("-")
                        else json.loads(args.observed_posting.read_text(encoding="utf-8")))
            urls = [observed.get("url")] if isinstance(observed, dict) else []
        elif args.file == Path("-"):
            urls = json.load(sys.stdin)
        elif args.file is not None:
            urls = json.loads(args.file.read_text(encoding="utf-8"))
        else:
            urls = args.urls
    except (OSError, ValueError):
        parser.error("Could not read a JSON array of job URLs")

    if (
        not isinstance(urls, list)
        or not 1 <= len(urls) <= MAX_BATCH_URLS
        or any(not isinstance(url, str) for url in urls)
    ):
        parser.error(f"Provide 1 to {MAX_BATCH_URLS} exact job URL strings")

    jobfeed_root = args.jobfeed_root.expanduser().resolve()
    use_jobfeed(jobfeed_root)

    if args.observed_posting is not None:
        from jobfeed.adapters.url_import import observed_linkedin_posting

        try:
            observed_linkedin_posting(observed)
        except (ValueError, TypeError) as exc:
            parser.error(str(exc))

    urls = [url.strip() for url in urls]
    for index, url in enumerate(urls):
        if problem := url_problem(url):
            parser.error(f"Item {index + 1} {problem}")

    try:
        db_path = configured_db(jobfeed_root, args.db)
    except ValueError as exc:
        parser.error(str(exc))

    intake = JobfeedIntake(db_path, jobfeed_root, args.jobfeed_python)
    if observed is not None:
        outcome = asyncio.run(intake.add_observed_posting(observed))
        result = {
            "status": "completed",
            "results": [{"index": 0, "url": urls[0], "result": outcome}],
            "counts_by_status": {outcome["status"]: 1},
            "newly_queued": int(outcome["status"] == "queued"),
        }
    else:
        result = asyncio.run(intake.add_job_urls(urls))
    print(json.dumps(result, ensure_ascii=False))
    return int(any(item["result"]["status"] not in OK_STATUSES for item in result["results"]))


if __name__ == "__main__":
    raise SystemExit(main())
