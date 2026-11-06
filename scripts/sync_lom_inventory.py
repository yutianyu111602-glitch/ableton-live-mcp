"""Inventory official LOM identifiers; source matches are candidates, never coverage proof.

Only public API names, access modes and version labels are retained, not the
documentation prose. Refresh is explicit and never changes a running Live Set.
"""

import argparse
import ast
import hashlib
import html
import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://docs.cycling74.com/apiref/lom/"
OUT = ROOT / "api" / "lom_inventory.json"
OVERRIDES = ROOT / "api" / "coverage_overrides.json"
HEADINGS = re.compile(r"<h([123])\b([^>]*)>(.*?)</h\1>", re.S)


def text(fragment):
    return html.unescape(re.sub(r"<[^>]+>", " ", fragment)).strip()


def fetch(url):
    if not url.startswith(BASE) or "?" in url or "#" in url:
        raise ValueError("Only official LOM pages may be fetched")
    request = urllib.request.Request(url, headers={"User-Agent": "AbletonMCP-API-Inventory/1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        if not response.geturl().startswith(BASE):
            raise ValueError("Unexpected documentation redirect")
        raw = response.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("Documentation page exceeds 4 MiB")
    return raw.decode("utf-8")


def parse_page(url, source):
    headings = list(HEADINGS.finditer(source))
    name = next((text(m[3]) for m in headings if m[1] == "1"), None)
    if not name:
        raise ValueError(f"No API object heading: {url}")
    section = None
    members = []
    for index, match in enumerate(headings):
        if match[1] == "2":
            section = text(match[3]).lower()
        if match[1] != "3" or section not in {"children", "properties", "functions"}:
            continue
        anchor = re.search(r'\bid="([^"]+)"', match[2])
        if anchor is None:
            continue  # Unanchored Resources/Support headings belong to the footer.
        identifier = html.unescape(anchor[1])
        end = headings[index + 1].start() if index + 1 < len(headings) else len(source)
        body = source[match.end() : end]
        access_match = re.search(r"Access:(.*?)</p>", body, re.S)
        access = re.findall(r">(get|set|observe)<", access_match[1]) if access_match else []
        since = re.search(r"Available since Live\s+([\d.]+)", text(body))
        members.append(
            dict(
                identifier=f"{name}.{identifier}",
                name=identifier,
                kind=section,
                access=access,
                since_live=since[1].rstrip(".") if since else None,
                url=url + "#" + identifier,
            )
        )
    return dict(
        name=name,
        url=url,
        source_sha256=hashlib.sha256(source.encode()).hexdigest(),
        members=members,
    )


def source_index():
    mentions = {}
    for path in sorted((ROOT / "ableton_live_mcp").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                item = f"{path.relative_to(ROOT).as_posix()}:{node.lineno}"
                mentions.setdefault(node.attr, set()).add(item)
    return mentions


def refresh():
    index = fetch(BASE)
    links = sorted(set(re.findall(r'href="(/apiref/lom/[a-z0-9_]+/)"', index)))
    if len(links) < 30:
        raise ValueError("Official object index unexpectedly incomplete; retain previous snapshot")
    urls = [urljoin(BASE, link) for link in links]
    with ThreadPoolExecutor(max_workers=4) as pool:
        pages = list(pool.map(lambda url: parse_page(url, fetch(url)), urls))
    mentions = source_index()
    overrides = json.loads(OVERRIDES.read_text(encoding="utf-8")) if OVERRIDES.is_file() else {}
    known = set()
    for page in pages:
        for member in page["members"]:
            known.add(member["identifier"])
            member["source_candidates"] = sorted(mentions.get(member["name"], []))[:20]
            member["coverage"] = overrides.get(
                member["identifier"],
                {
                    "status": "review_required",
                    "runtime_acceptance": "not_run",
                },
            )
    unknown = set(overrides) - known
    if unknown:
        raise ValueError(
            f"Coverage overrides refer to missing official identifiers: {sorted(unknown)}"
        )
    version = re.search(r"refers to Ableton Live version\s+([\d.]+)", text(index))
    result = dict(
        schema_version=1,
        source=BASE,
        documentation_live_version=version[1].rstrip(".") if version else None,
        fetched_at_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        object_count=len(pages),
        member_count=len(known),
        interpretation=(
            "Source-name matches require object/transport review. "
            "Offline implementation is not Live acceptance."
        ),
        objects=sorted(pages, key=lambda p: p["name"]),
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(OUT)
    return result


def check():
    data = json.loads(OUT.read_text(encoding="utf-8"))
    objects = data["objects"]
    names = [m["identifier"] for obj in objects for m in obj["members"]]
    if len(names) != len(set(names)) or len(names) != data["member_count"]:
        raise ValueError("API inventory contains duplicate or missing members")
    if len(objects) != data["object_count"]:
        raise ValueError("API object count mismatch")
    for obj in objects:
        if not obj["url"].startswith(BASE):
            raise ValueError("Unexpected inventory source")
    return data


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Fetch current official LOM pages; default checks the committed snapshot offline",
    )
    args = parser.parse_args()
    result = refresh() if args.refresh else check()
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "object_count",
                    "member_count",
                    "documentation_live_version",
                    "fetched_at_utc",
                )
            }
        )
    )
