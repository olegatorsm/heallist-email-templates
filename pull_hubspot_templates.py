#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import posixpath
from datetime import datetime, timezone
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
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def api_url(environment: str, action: str, remote_path: str) -> str:
    # "/" is the Design Manager root. Other paths are root-relative.
    encoded = "%2F" if remote_path in ("", "/") else quote(remote_path.lstrip("/"), safe="/")
    return f"{API_BASE}/{environment}/{action}/{encoded}"


def join_remote(parent: str, child: str) -> str:
    if parent in ("", "/"):
        return child.lstrip("/")
    return posixpath.join(parent.rstrip("/"), child.lstrip("/"))


def relative_remote(remote_root: str, remote_path: str) -> str:
    if remote_root in ("", "/"):
        return remote_path.lstrip("/")
    root = remote_root.strip("/")
    path = remote_path.strip("/")
    if path == root:
        return Path(path).name
    prefix = root + "/"
    if not path.startswith(prefix):
        raise ValueError(f"{remote_path!r} is outside remote root {remote_root!r}")
    return path[len(prefix):]


def safe_local_path(local_root: Path, relative_path: str) -> Path:
    root = local_root.resolve()
    candidate = (root / Path(relative_path)).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"Unsafe path from HubSpot: {relative_path!r}")
    return candidate


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_template_file(remote_path: str) -> bool:
    """
    HubSpot coded templates/partials/sections are .html files.
    module.html inside *.module directories is a module, not a template.
    Also include legacy *.template.json if present.
    """
    normalized = remote_path.replace("\\", "/")
    parts = normalized.split("/")
    if any(part.endswith(".module") for part in parts):
        return False
    lower = normalized.lower()
    return lower.endswith(".html") or lower.endswith(".template.json")


def is_supported_asset(remote_path: str) -> bool:
    return Path(remote_path).suffix.lower() in SUPPORTED_EXTENSIONS


def get_metadata(session: requests.Session, environment: str, remote_path: str) -> dict:
    response = session.get(
        api_url(environment, "metadata", remote_path),
        headers={"Accept": "application/json"},
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def download_file(session: requests.Session, environment: str, remote_path: str) -> bytes:
    response = session.get(
        api_url(environment, "content", remote_path),
        headers={"Accept": "application/octet-stream"},
        timeout=120,
    )
    response.raise_for_status()
    return response.content


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pull HubSpot Design Manager templates via CMS Source Code API."
    )
    parser.add_argument(
        "local_dir",
        nargs="?",
        default="./hubspot_templates",
        help="Local destination directory (default: ./hubspot_templates)",
    )
    parser.add_argument(
        "--remote-root",
        default="/",
        help='Remote Design Manager folder to traverse (default: "/")',
    )
    parser.add_argument(
        "--environment",
        choices=("draft", "published"),
        default="draft",
        help="HubSpot source environment (default: draft)",
    )
    parser.add_argument(
        "--all-assets",
        action="store_true",
        help="Pull all supported Design Manager assets, not only templates.",
    )
    parser.add_argument(
        "--include-hubspot-defaults",
        action="store_true",
        help="Include the top-level @hubspot system/default assets when pulling from root.",
    )
    args = parser.parse_args()

    token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise SystemExit("Set HUBSPOT_ACCESS_TOKEN to your HubSpot private-app access token.")

    local_root = Path(args.local_dir)
    local_root.mkdir(parents=True, exist_ok=True)
    session = make_session(token)

    manifest = {
        "api_base": API_BASE,
        "environment": args.environment,
        "remote_root": args.remote_root,
        "selection": "all_assets" if args.all_assets else "templates",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": {},
    }

    downloaded = 0
    visited = 0

    def walk(remote_path: str) -> None:
        nonlocal downloaded, visited

        meta = get_metadata(session, args.environment, remote_path)
        visited += 1

        if meta.get("folder"):
            for child in meta.get("children", []):
                if (
                    remote_path in ("", "/")
                    and child == "@hubspot"
                    and not args.include_hubspot_defaults
                ):
                    print("[skip] @hubspot (use --include-hubspot-defaults to include it)")
                    continue
                walk(join_remote(remote_path, child))
            return

        selected = is_supported_asset(remote_path) if args.all_assets else is_template_file(remote_path)
        if not selected:
            return

        data = download_file(session, args.environment, remote_path)
        relative = relative_remote(args.remote_root, remote_path)
        destination = safe_local_path(local_root, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)

        manifest["files"][remote_path] = {
            "local_path": relative.replace("\\", "/"),
            "sha256": sha256_bytes(data),
            "hubspot_hash": meta.get("hash"),
            "updated_at": meta.get("updatedAt"),
        }
        downloaded += 1
        print(f"[pull] {remote_path} -> {destination}")

    try:
        walk(args.remote_root)
    except requests.HTTPError as exc:
        body = ""
        if exc.response is not None:
            body = exc.response.text[:2000]
        raise SystemExit(f"HubSpot API error: {exc}\n{body}") from exc

    manifest_path = local_root / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print()
    print(f"Done. Downloaded: {downloaded}; metadata nodes visited: {visited}")
    print(f"Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
