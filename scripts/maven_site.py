#!/usr/bin/env python3
"""Maven repository tooling for the public SDK repository.

The repository keeps one folder per published version under maven/, e.g.

    maven/ai/amani/android/<artifactId>/<version>/      e.g. amanivideosdk/1.10.0
        <artifactId>-<version>.aar  (+ .sha1 .sha256)
        <artifactId>-<version>.pom  (+ checksums)
        <artifactId>-<version>-sources.jar  (+ checksums)
        <artifactId>-<version>.module  (+ checksums; only modules that publish Gradle Module Metadata)
        SHA256SUMS          sha256sum output for every artifact above
        RELEASE_NOTES.md    GitHub Release body
        release.json        {"groupId", "artifactId", "version", "tag", "prerelease", ...}

Files are served straight from the repository (raw.githubusercontent.com), so the per-artifact
maven-metadata.xml (+ .sha1 .sha256) is committed too: a release pull request adds the version folder
and regenerates that file with `metadata`; the PR check compares it with `check-metadata`. It has no
timestamp, so the same folders always give the same bytes.

    python3 -I scripts/maven_site.py verify maven [VERSION_DIR ...]
    python3 -I scripts/maven_site.py metadata maven           # (re)write maven-metadata.xml files
    python3 -I scripts/maven_site.py check-metadata maven     # committed metadata == folders
    python3 -I scripts/maven_site.py pending maven            # one JSON line per version folder
    python3 -I scripts/maven_site.py notes VERSION_DIR [--pr-body-file F] [--maven-url URL]

Standard library only.
"""
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

# Per-file checksums served next to every artifact. .sha1 is what Maven and Gradle clients look for
# when downloading; .sha256 is the one the publish flow cross-checks. .md5 and .sha512 are not
# published: no supported client needs them, and a folder that has them is rejected (allowlist).
CHECKSUM_ALGOS = {"sha1": hashlib.sha1, "sha256": hashlib.sha256}
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9._-]*$")


def digest(path, algo="sha256"):
    h = CHECKSUM_ALGOS[algo]()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_checksums(path):
    for algo in CHECKSUM_ALGOS:
        Path(f"{path}.{algo}").write_text(digest(path, algo))


def version_dirs(maven_dir):
    """Every folder that holds a release.json, sorted for stable output."""
    return sorted(p.parent for p in Path(maven_dir).rglob("release.json"))


# Tag of a version in its public repository: artifactId → prefix + version.
# The Video SDK keeps its historical tag format X.Y.Z (no prefix).
TAG_PREFIX = {"amanivideosdk": ""}


def load_release(vdir):
    return json.loads((vdir / "release.json").read_text())


def read_sums(vdir):
    sums = {}
    for line in (vdir / "SHA256SUMS").read_text().splitlines():
        if line.strip():
            value, name = line.split(maxsplit=1)
            sums[name.strip().lstrip("*")] = value.lower()
    return sums


def version_key(version):
    """Approximate Maven ordering: 3.22.0-alpha.2 < 3.22.0-rc1 < 3.22.0 < 3.22.1."""
    m = re.match(r"^([0-9]+(?:\.[0-9]+)*)(.*)$", version)
    nums = tuple(int(n) for n in m.group(1).split(".")) if m else ()
    qualifier = (m.group(2) if m else version).lstrip(".-_").lower()
    rank = {"alpha": 0, "a": 0, "beta": 1, "b": 1, "milestone": 2, "m": 2, "rc": 3, "cr": 3,
            "snapshot": 4}
    if not qualifier:
        q = (5,)
    else:
        word = re.match(r"[a-z]*", qualifier).group(0)
        rest = tuple(int(n) for n in re.findall(r"[0-9]+", qualifier))
        q = (rank.get(word, 4), word, rest)
    return nums + (0,) * (6 - len(nums)), q


# ── verify ───────────────────────────────────────────────────────────────────────────────────────
def verify_dir(vdir):
    """Returns a list of problems for one version folder (empty when it is consistent)."""
    problems = []
    try:
        rel = load_release(vdir)
    except (OSError, ValueError) as exc:
        return [f"{vdir}: unreadable release.json ({exc})"]
    for key in ("groupId", "artifactId", "version", "tag"):
        if not isinstance(rel.get(key), str) or not rel[key]:
            problems.append(f"{vdir}: release.json is missing '{key}'")
    for key in ("prerelease", "backfilled"):
        if key in rel and not isinstance(rel[key], bool):
            problems.append(f"{vdir}: release.json '{key}' must be true or false")
    if not problems:
        expected_tag = TAG_PREFIX.get(rel["artifactId"], "v") + rel["version"]
        if rel["tag"] != expected_tag:
            problems.append(f"{vdir}: release.json tag '{rel['tag']}' must be '{expected_tag}'")
    if problems:
        return problems
    group, artifact, version = rel["groupId"], rel["artifactId"], rel["version"]
    if not VERSION_RE.match(version):
        problems.append(f"{vdir}: invalid version '{version}'")
    expected = Path(*group.split("."), artifact, version)
    if not str(vdir).replace(os.sep, "/").endswith(str(expected).replace(os.sep, "/")):
        problems.append(f"{vdir}: folder does not match {group}:{artifact}:{version}")
    if not (vdir / "SHA256SUMS").is_file():
        return problems + [f"{vdir}: SHA256SUMS is missing"]

    sums = read_sums(vdir)
    base = f"{artifact}-{version}"
    for required in (f"{base}.aar", f"{base}.pom"):
        if required not in sums:
            problems.append(f"{vdir}: SHA256SUMS does not list {required}")
    for name, expected_sha in sums.items():
        f = vdir / name
        if not f.is_file():
            problems.append(f"{vdir}: {name} is listed in SHA256SUMS but missing")
            continue
        actual = digest(f)
        if actual != expected_sha:
            problems.append(f"{vdir}: {name} SHA-256 is {actual}, SHA256SUMS says {expected_sha}")
        for algo in CHECKSUM_ALGOS:
            cf = vdir / f"{name}.{algo}"
            if not cf.is_file():
                problems.append(f"{vdir}: {cf.name} is missing")
            elif cf.read_text().strip().lower() != digest(f, algo):
                problems.append(f"{vdir}: {cf.name} does not match {name}")
    # Allowlist: anything not listed above would be served from the Maven URL unchecked.
    allowed = {"SHA256SUMS", "RELEASE_NOTES.md", "release.json"}
    allowed |= set(sums) | {f"{n}.{algo}" for n in sums for algo in CHECKSUM_ALGOS}
    for entry in sorted(vdir.iterdir()):
        if entry.is_symlink():
            problems.append(f"{vdir}: {entry.name} is a symbolic link")
        elif entry.is_dir():
            problems.append(f"{vdir}: unexpected folder {entry.name}/")
        elif entry.name not in allowed:
            problems.append(f"{vdir}: unexpected file {entry.name} (not listed in SHA256SUMS)")
    return problems


def cmd_verify(args):
    dirs = [Path(d) for d in args.dirs] if args.dirs else version_dirs(args.maven_dir)
    if not dirs:
        print("No version folders found.")
        return 0
    problems = [p for d in dirs for p in verify_dir(d)]
    for d in dirs:
        print(f"checked {d}")
    for p in problems:
        print(f"::error::{p}")
    return 1 if problems else 0


# ── maven-metadata.xml ───────────────────────────────────────────────────────────────────────────
def maven_metadata(group, artifact, versions, releases):
    """Deterministic maven-metadata.xml (no lastUpdated): same folders → same bytes."""
    ordered = sorted(versions, key=version_key)
    stable = [v for v in ordered if not releases[v].get("prerelease")]
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<metadata>",
             f"  <groupId>{group}</groupId>", f"  <artifactId>{artifact}</artifactId>",
             "  <versioning>", f"    <latest>{ordered[-1]}</latest>"]
    if stable:
        lines.append(f"    <release>{stable[-1]}</release>")
    lines.append("    <versions>")
    lines += [f"      <version>{v}</version>" for v in ordered]
    lines += ["    </versions>", "  </versioning>", "</metadata>", ""]
    return "\n".join(lines)


def expected_metadata(maven_dir):
    """{artifact folder: (xml text)} for every artifact that has version folders."""
    artifacts = {}
    for vdir in version_dirs(maven_dir):
        rel = load_release(vdir)
        artifacts.setdefault((rel["groupId"], rel["artifactId"], vdir.parent), {})[rel["version"]] = rel
    return {adir: maven_metadata(group, artifact, rels.keys(), rels)
            for (group, artifact, adir), rels in artifacts.items()}


def cmd_metadata(args):
    for adir, xml in sorted(expected_metadata(args.maven_dir).items()):
        meta = adir / "maven-metadata.xml"
        meta.write_text(xml)
        write_checksums(meta)
        print(f"wrote {meta}")
    return 0


def cmd_check_metadata(args):
    problems = []
    expected = expected_metadata(args.maven_dir)
    for adir, xml in sorted(expected.items()):
        meta = adir / "maven-metadata.xml"
        if not meta.is_file():
            problems.append(f"{meta} is missing (run: maven_site.py metadata maven)")
            continue
        if meta.read_text() != xml:
            problems.append(f"{meta} does not match the version folders (run: maven_site.py metadata maven)")
        for algo in CHECKSUM_ALGOS:
            cf = Path(f"{meta}.{algo}")
            if not cf.is_file() or cf.read_text().strip().lower() != digest(meta, algo):
                problems.append(f"{cf} is missing or does not match maven-metadata.xml")
    for meta in Path(args.maven_dir).rglob("maven-metadata.xml"):
        if meta.parent not in expected:
            problems.append(f"{meta} has no version folders next to it")
    for p in problems:
        print(f"::error::{p}")
    print("maven-metadata.xml " + ("problems found" if problems else "matches the version folders"))
    return 1 if problems else 0


# ── pending ──────────────────────────────────────────────────────────────────────────────────────
def cmd_pending(args):
    """Prints one JSON line per version folder; the deploy workflow decides which need a release."""
    for vdir in version_dirs(args.maven_dir):
        rel = load_release(vdir)
        print(json.dumps({"dir": str(vdir), "tag": rel["tag"], "version": rel["version"],
                          "artifactId": rel["artifactId"], "prerelease": bool(rel.get("prerelease")),
                          "backfilled": rel.get("backfilled") is True}))
    return 0


# ── notes ────────────────────────────────────────────────────────────────────────────────────────
# Same badges as the SDK release notes on the documentation site (androidv3/ReleaseNote.md).
BADGES = {
    "feature":     "![Feature](https://img.shields.io/badge/Feature-✨-27ae60)",
    "improvement": "![Improvement](https://img.shields.io/badge/Improvement-🛠️-8e44ad)",
    "change":      "![Change](https://img.shields.io/badge/Change-♻️-136634)",
    "fix":         "![Fix](https://img.shields.io/badge/Fix-🐛-e74c3c)",
}
ALIASES = {"features": "feature", "improvements": "improvement", "changes": "change",
           "update": "change", "updates": "change", "fixes": "fix", "bugfix": "fix", "bug": "fix"}
LEADING_IMAGE = re.compile(r"^!\[[^\]]*\]\([^)]*\)\s*")
KEYWORD = re.compile(r"^([A-Za-z]+)\s*(?::|-|—|–)\s*|^([A-Za-z]+)\s+(?=\S)")


def section_items(text, heading):
    """List items under a '### <heading>' line (case-insensitive) up to the next heading or rule."""
    items, capture = [], False
    for line in (text or "").splitlines():
        s = line.strip()
        if s.lower().startswith("### " + heading):
            capture = True
            continue
        if capture and (s.startswith("#") or s == "---"):
            break
        if capture:
            s = s.lstrip("-*• ").strip()
            if s and not s.startswith("<!--") and not s.startswith("-->"):
                items.append(s)
    return items


def pr_release_items(body):
    """Items under a '### Release notes' heading of a pull request body (until the next heading)."""
    return section_items(body, "release notes")


def badge(item):
    """'fix: **NFC** — text' / 'Feature - text' / an item already carrying a badge image →
    '<docs badge> **NFC** — text'. Items without a known keyword are kept as they are."""
    text = LEADING_IMAGE.sub("", item.strip())
    m = KEYWORD.match(text)
    if m:
        word = (m.group(1) or m.group(2)).lower()
        word = ALIASES.get(word, word)
        if word in BADGES:
            rest = text[m.end():].strip()
            return f"{BADGES[word]} {rest}" if rest else BADGES[word]
    return text


def cmd_notes(args):
    """GitHub Release body for one version folder: "What's Changed" first, checksums at the bottom.

    Items come from the '### Release notes' section of the pull request that added the folder; when
    it has none, from the "What's Changed" section of the folder's RELEASE_NOTES.md (written by the
    SDK repository's release workflows). Every item gets the documentation-site badge of its leading
    keyword (feature / improvement / change / fix). The checksum section always comes from
    RELEASE_NOTES.md."""
    vdir = Path(args.version_dir)
    rel = load_release(vdir)
    text = (vdir / "RELEASE_NOTES.md").read_text()
    body = Path(args.pr_body_file).read_text() if args.pr_body_file else ""
    items = pr_release_items(body) or section_items(text, "what's changed")
    idx = text.find("### Checksums (SHA-256)")
    checksums = text[idx:].rstrip("\n") if idx >= 0 else ""
    lines = ["## What's Changed", ""]
    lines += [f"* {badge(i)}" for i in items] if items else ["* Maintenance release."]
    lines += ["", "---", ""]
    if checksums:
        lines.append(checksums)
    maven_url = (args.maven_url or "").rstrip("/")
    if maven_url:
        lines += ["", f"Maven repository: `{maven_url}/` · coordinates "
                      f"`{rel['groupId']}:{rel['artifactId']}:{rel['version']}`"]
    sys.stdout.write("\n".join(lines).rstrip("\n") + "\n")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify")
    v.add_argument("maven_dir")
    v.add_argument("dirs", nargs="*", help="version folders to check (default: all)")
    m = sub.add_parser("metadata")
    m.add_argument("maven_dir")
    c = sub.add_parser("check-metadata")
    c.add_argument("maven_dir")
    p = sub.add_parser("pending")
    p.add_argument("maven_dir")
    n = sub.add_parser("notes")
    n.add_argument("version_dir")
    n.add_argument("--pr-body-file", default="")
    n.add_argument("--maven-url", default="")
    args = parser.parse_args()
    return {"verify": cmd_verify, "metadata": cmd_metadata, "check-metadata": cmd_check_metadata,
            "pending": cmd_pending, "notes": cmd_notes}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
