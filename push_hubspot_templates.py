#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import posixpath
from pathlib import Path
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_BASE = "https://api.hubapi.com/cms/source-code/2026-03"
MANIFEST_NAME = ".hubspot-manifest.json"

SUPPORTED_EXTENSIONS = {
    ".css", ".js", ".json", ".html", ".txt", ".md",
    ".jpg", ".jpeg", ".png", ".gif", ".map", ".svg",
    ".ttf", ".woff", ".woff2", ".zip",
}


def make_session(token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST", "PUT"}),
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def api_url(environment: str, action: str, remote_path: str) -> str:
    encoded = "%2F" if remote_path in ("", "/") else quote(remote_path.lstrip("/"), safe="/")
    return f"{API_BASE}/{environment}/{action}/{encoded}"


def join_remote(parent: str, child: str) -> str:
    if parent in ("", "/"):
        return child.lstrip("/")
    return posixpath.join(parent.rstrip("/"), child.lstrip("/"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_template_file(remote_path: str) -> bool:
    normalized = remote_path.replace("\\", "/")
    parts = normalized.split("/")
    if any(part.endswith(".module") for part in parts):
        return False
    lower = normalized.lower()
    return lower.endswith(".html") or lower.endswith(".template.json")


def is_supported_asset(remote_path: str) -> bool:
    return Path(remote_path).suffix.lower() in SUPPORTED_EXTENSIONS


def get_metadata_or_none(
    session: requests.Session, environment: str, remote_path: str
) -> dict | None:
    response = session.get(
        api_url(environment, "metadata", remote_path),
        headers={"Accept": "application/json"},
        timeout=60,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def validate_file(
    session: requests.Session,
    environment: str,
    remote_path: str,
    local_path: Path,
) -> tuple[bool, str]:
    with local_path.open("rb") as fh:
        response = session.post(
            api_url(environment, "validate", remote_path),
            files={"file": (local_path.name, fh, "application/octet-stream")},
            timeout=120,
        )

    if response.ok:
        text = response.text.strip()
        return True, text

    return False, response.text[:4000]


def upload_file(
    session: requests.Session,
    environment: str,
    remote_path: str,
    local_path: Path,
) -> dict:
    with local_path.open("rb") as fh:
        response = session.put(
            api_url(environment, "content", remote_path),
            files={"file": (local_path.name, fh, "application/octet-stream")},
            timeout=180,
        )
    response.raise_for_status()
    return response.json() if response.content else {}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Push only changed HubSpot Design Manager templates."
    )
    parser.add_argument(
        "local_dir",
        nargs="?",
        default="./hubspot_templates",
        help="Local directory created by pull script (default: ./hubspot_templates)",
    )
    parser.add_argument(
        "--environment",
        choices=("draft", "published"),
        help="Override target environment. Default: environment stored in manifest.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show files that would be uploaded without changing HubSpot.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite even if the remote file changed since the last pull.",
    )
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip HubSpot validation before upload.",
    )
    args = parser.parse_args()

    token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise SystemExit("Set HUBSPOT_ACCESS_TOKEN to your HubSpot private-app access token.")

    local_root = Path(args.local_dir).resolve()
    manifest_path = local_root / MANIFEST_NAME
    if not manifest_path.exists():
        raise SystemExit(
            f"{manifest_path} not found. Run the pull script first so changes can be detected."
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    remote_root = manifest.get("remote_root", "/")
    manifest_environment = manifest.get("environment", "draft")
    environment = args.environment or manifest_environment
    selection = manifest.get("selection", "templates")

    if environment != manifest_environment and not args.force:
        raise SystemExit(
            f"Manifest was created for {manifest_environment!r}, but target is {environment!r}. "
            "Pull that environment first, or use --force if you intentionally want to switch."
        )
    tracked = manifest.setdefault("files", {})

    if environment == "published":
        print("WARNING: target environment is PUBLISHED. Uploads become live immediately.")

    session = make_session(token)

    candidates: list[tuple[Path, str, str]] = []

    for local_path in sorted(p for p in local_root.rglob("*") if p.is_file()):
        if local_path == manifest_path:
            continue

        rel = local_path.relative_to(local_root).as_posix()

        # Ignore common local-only files/directories.
        if any(part in {".git", "__pycache__", ".idea", ".vscode"} for part in local_path.parts):
            continue
        if local_path.name.startswith("."):
            continue

        remote_path = join_remote(remote_root, rel)
        selected = is_supported_asset(remote_path) if selection == "all_assets" else is_template_file(remote_path)
        if not selected:
            continue

        current_sha = sha256_file(local_path)
        previous_sha = tracked.get(remote_path, {}).get("sha256")
        if current_sha != previous_sha:
            candidates.append((local_path, remote_path, current_sha))

    if not candidates:
        print("No local changes to push.")
        return

    print(f"Changed/new files: {len(candidates)}")
    uploaded = 0
    skipped_conflicts = 0
    failed_validation = 0

    for local_path, remote_path, current_sha in candidates:
        previous = tracked.get(remote_path)

        # Protect against overwriting edits made remotely since our last pull.
        if previous and not args.force:
            remote_meta = get_metadata_or_none(session, environment, remote_path)
            old_remote_hash = previous.get("hubspot_hash")
            new_remote_hash = remote_meta.get("hash") if remote_meta else None
            if old_remote_hash and new_remote_hash and old_remote_hash != new_remote_hash:
                print(f"[conflict] {remote_path}")
                print("           Remote file changed since pull; use --force to overwrite.")
                skipped_conflicts += 1
                continue

        if args.dry_run:
            print(f"[dry-run] {remote_path}")
            continue

        # HubSpot validation is especially useful for HubL templates and JSON metadata.
        if not args.no_validate and local_path.suffix.lower() in {".html", ".json"}:
            ok, details = validate_file(session, environment, remote_path, local_path)
            if not ok:
                print(f"[invalid] {remote_path}")
                print(details)
                failed_validation += 1
                continue
            if details and details not in ("{}", "[]"):
                try:
                    parsed = json.loads(details)
                    warnings = parsed.get("warnings") if isinstance(parsed, dict) else None
                    if warnings:
                        print(f"[warning] {remote_path}: {json.dumps(warnings, ensure_ascii=False)}")
                except json.JSONDecodeError:
                    pass

        try:
            result = upload_file(session, environment, remote_path, local_path)
        except requests.HTTPError as exc:
            body = exc.response.text[:4000] if exc.response is not None else ""
            print(f"[error] {remote_path}: {exc}")
            if body:
                print(body)
            continue

        tracked[remote_path] = {
            "local_path": local_path.relative_to(local_root).as_posix(),
            "sha256": current_sha,
            "hubspot_hash": result.get("hash"),
            "updated_at": result.get("updatedAt"),
        }
        manifest["environment"] = environment
        uploaded += 1
        print(f"[push] {remote_path}")

        # Save after every successful upload so an interrupted run can resume safely.
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    print()
    print(
        f"Done. Uploaded: {uploaded}; conflicts: {skipped_conflicts}; "
        f"validation failures: {failed_validation}"
    )

    if args.dry_run:
        print("Dry run: HubSpot was not modified.")


if __name__ == "__main__":
    main()
