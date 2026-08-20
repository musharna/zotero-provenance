#!/usr/bin/env python3
"""Interactive first-run setup: pick a library, ensure a collection, write secrets.

Ends with a real round-trip against the Zotero API (create -> read back -> delete)
so a green run means the credentials actually work, not merely that they parsed.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

from zotero_capture.config import DEFAULT_SECRETS_FILE  # noqa: E402
from zotero_capture.zotero_client import ZoteroClient  # noqa: E402

API_BASE = "https://api.zotero.org"
DEFAULT_COLLECTION_NAME = "web-sources"


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Zotero-API-Version": "3",
        "User-Agent": "zotero-provenance/0.1",
    }


def key_info(api_key: str) -> dict:
    resp = httpx.get(f"{API_BASE}/keys/current", headers=_headers(api_key), timeout=15)
    if resp.status_code == 403:
        raise SystemExit("That API key was rejected by Zotero (403). Check the key.")
    resp.raise_for_status()
    return resp.json()


def list_groups(api_key: str, user_id: str) -> list[dict]:
    resp = httpx.get(
        f"{API_BASE}/users/{user_id}/groups",
        headers=_headers(api_key),
        params={"limit": 100},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def find_or_create_collection(
    api_key: str, library_type: str, library_id: str, name: str
) -> str:
    base = f"{API_BASE}/{library_type}s/{library_id}"
    resp = httpx.get(
        f"{base}/collections",
        headers=_headers(api_key),
        params={"limit": 100},
        timeout=15,
    )
    resp.raise_for_status()
    for coll in resp.json():
        if coll["data"]["name"] == name:
            return coll["key"]
    resp = httpx.post(
        f"{base}/collections", headers=_headers(api_key), json=[{"name": name}], timeout=15
    )
    if resp.status_code >= 400:
        raise SystemExit(f"Could not create collection {name!r}: {resp.status_code} {resp.text}")
    body = resp.json()
    if body.get("failed"):
        raise SystemExit(f"Could not create collection {name!r}: {body['failed']}")
    return next(iter(body["successful"].values()))["key"]


def verify_round_trip(
    api_key: str, library_type: str, library_id: str, collection_key: str
) -> None:
    """Create a throwaway item, confirm it came back, then delete it."""
    probe_url = "https://example.invalid/zotero-provenance-setup-probe"
    with ZoteroClient(
        api_key=api_key,
        library_id=library_id,
        library_type=library_type,
        web_sources_collection_key=collection_key,
    ) as client:
        item_key = client.post_webpage_item(
            url_canonical=probe_url,
            title="zotero-provenance setup probe (safe to delete)",
            access_date=date.today().isoformat(),
            tags=["zotero-provenance:setup-probe"],
        )
        found = client.query_by_tag("zotero-provenance:setup-probe")
        try:
            if not any(i["key"] == item_key for i in found):
                raise SystemExit(
                    "Setup probe was created but did not come back from a tag query — "
                    "the library is reachable for writes but not for reads. Stopping."
                )
        finally:
            client.delete_item(item_key)


def write_secrets(
    path: Path, api_key: str, library_type: str, library_id: str, collection_key: str
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Written by zotero-provenance setup. Keep this file private.\n"
        f"ZOTERO_API_KEY={api_key}\n"
        f"ZOTERO_LIBRARY_TYPE={library_type}\n"
        f"ZOTERO_LIBRARY_ID={library_id}\n"
        f"ZOTERO_WEBSOURCES_COLLECTION_KEY={collection_key}\n"
    )
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600


def choose_library(api_key: str, info: dict, requested: str | None) -> tuple[str, str]:
    user_id = str(info["userID"])
    groups = list_groups(api_key, user_id)
    options: list[tuple[str, str, str]] = [
        ("user", user_id, f"your personal library ({info.get('username', user_id)})")
    ]
    options.extend(("group", str(g["id"]), f"group: {g['data']['name']}") for g in groups)

    if requested:
        for lib_type, lib_id, _label in options:
            if requested in (lib_id, _label) or requested == f"{lib_type}:{lib_id}":
                return lib_type, lib_id
        raise SystemExit(f"--library {requested!r} did not match any accessible library.")

    if not sys.stdin.isatty():
        raise SystemExit(
            "Multiple libraries are available; re-run with --library <id> "
            "(non-interactive mode cannot prompt).\nAvailable: "
            + "; ".join(f"{i} = {label} [{lib_id}]" for i, (_t, lib_id, label) in enumerate(options, 1))
        )

    print("\nWhich library should captured sources go into?")
    for i, (_t, lib_id, label) in enumerate(options, 1):
        print(f"  {i}. {label}  [{lib_id}]")
    while True:
        raw = input(f"Choice [1-{len(options)}]: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            lib_type, lib_id, _ = options[int(raw) - 1]
            return lib_type, lib_id
        print("Please enter one of the listed numbers.")


def main() -> int:
    p = argparse.ArgumentParser(description="Set up zotero-provenance.")
    p.add_argument("--api-key", default=os.environ.get("ZOTERO_API_KEY"))
    p.add_argument("--library", default=None, help="library id to use, skipping the prompt")
    p.add_argument("--collection", default=DEFAULT_COLLECTION_NAME)
    p.add_argument("--secrets-file", default=DEFAULT_SECRETS_FILE)
    p.add_argument("--no-verify", action="store_true", help="skip the live round-trip check")
    args = p.parse_args()

    api_key = args.api_key
    if not api_key:
        if not sys.stdin.isatty():
            raise SystemExit("No API key. Pass --api-key or set ZOTERO_API_KEY.")
        print("Create a key at https://www.zotero.org/settings/keys (needs library write access).")
        api_key = input("Zotero API key: ").strip()
    if not api_key:
        raise SystemExit("No API key given.")

    info = key_info(api_key)
    library_type, library_id = choose_library(api_key, info, args.library)
    collection_key = find_or_create_collection(
        api_key, library_type, library_id, args.collection
    )
    print(f"Using {library_type} library {library_id}, collection {args.collection} [{collection_key}]")

    if not args.no_verify:
        print("Verifying with a live create/read/delete round-trip...")
        verify_round_trip(api_key, library_type, library_id, collection_key)
        print("Round-trip OK.")

    secrets_path = Path(args.secrets_file).expanduser()
    write_secrets(secrets_path, api_key, library_type, library_id, collection_key)
    print(f"Wrote {secrets_path} (mode 0600).")
    print("\nSetup complete. Sources cited in this project will be captured from the next turn.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
