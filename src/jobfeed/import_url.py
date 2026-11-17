"""JSON command for URL intake without loading unrelated scan dependencies."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

from jobfeed.adapters.store.sqlite import SQLiteStore
from jobfeed.adapters.url_import import JobUrlImportService
from jobfeed.domain.posting_url import PostingURLKey

MAX_KEY_BATCH = 20


async def import_url(db_path: Path, url: str) -> dict[str, object]:
    """Import one posting through an explicitly initialized database.

    Args:
        db_path: Existing Jobfeed database path.
        url: Public posting URL to import.

    Returns:
        The adapter outcome as JSON-compatible status, source ID, canonical ID,
        and reason fields. URL refusals and fetch failures are returned outcomes.

    Raises:
        FileNotFoundError: The database has not been initialized.
    """
    if not db_path.is_file():
        raise FileNotFoundError(
            "Database is missing. Initialize it explicitly with --initialize."
        )
    store = SQLiteStore(db_path)
    await store.connect()
    await store.close()
    return asdict(await JobUrlImportService(db_path).import_url(url))


async def initialize(db_path: Path) -> None:
    """Create the database directory and initialize the Jobfeed schema.

    Args:
        db_path: Database path explicitly selected for initialization.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(db_path)
    await store.connect()
    await store.close()


async def import_observed_posting(db_path: Path, payload: object) -> dict[str, object]:
    """Import one browser observation into an existing database.

    Args:
        db_path: Configured Jobfeed database.
        payload: Observed posting sent by the browser.

    Returns:
        The import result as a dictionary.

    Raises:
        FileNotFoundError: If the database does not exist.
    """
    if not db_path.is_file():
        raise FileNotFoundError("Configured Jobfeed database is missing")
    return asdict(await JobUrlImportService(db_path).import_observed_posting(payload))


async def star_imported(db_path: Path, source_id: int) -> dict[str, object]:
    """Set the formal manual-intake default without overriding user choices.

    Args:
        db_path: Configured Jobfeed database.
        source_id: Source job ID of the imported posting.

    Returns:
        The source job ID and whether it is starred.

    Raises:
        FileNotFoundError: If the database does not exist.
        ValueError: If the source job ID is not positive.
    """
    if not db_path.is_file():
        raise FileNotFoundError("Configured Jobfeed database is missing")
    if source_id <= 0:
        raise ValueError("Source job ID must be positive")
    store = SQLiteStore(db_path)
    await store.connect()
    try:
        result = await store.set_job_star(str(source_id), True, user_set=False)
        return {
            "status": "starred",
            "source_job_id": source_id,
            "starred": result.starred,
        }
    finally:
        await store.close()


def main() -> None:
    """Run URL import, explicit initialization, or URL-key JSON preflight.

    Preflight accepts 1 to 20 URL strings on stdin and returns one key per input.
    Invalid posting URLs retain their position as null keys.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument("--url-keys", action="store_true")
    parser.add_argument("--star-imported", type=int)
    parser.add_argument("--observed-posting", action="store_true")
    parser.add_argument("url", nargs="?")
    args = parser.parse_args()
    if args.observed_posting:
        print(
            json.dumps(
                asyncio.run(import_observed_posting(args.db, json.load(sys.stdin)))
            )
        )
    elif args.url_keys:
        urls = json.load(sys.stdin)
        if (
            not isinstance(urls, list)
            or not 1 <= len(urls) <= MAX_KEY_BATCH
            or any(not isinstance(url, str) for url in urls)
        ):
            parser.error("Provide 1 to 20 URL strings on stdin")
        keys: list[str | None] = []
        for url in urls:
            try:
                keys.append(PostingURLKey.parse(url).concurrency_key)
            except ValueError:
                keys.append(None)
        print(json.dumps({"status": "url_keys", "keys": keys}))
    elif args.star_imported is not None:
        print(json.dumps(asyncio.run(star_imported(args.db, args.star_imported))))
    elif args.initialize:
        asyncio.run(initialize(args.db))
        print(json.dumps({"status": "initialized", "db": str(args.db.resolve())}))
    elif args.url:
        print(json.dumps(asyncio.run(import_url(args.db, args.url))))
    else:
        parser.error("Provide a posting URL or --initialize")


if __name__ == "__main__":
    main()
