#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DOWNLOAD_MOVIES = Path("/srv/downloads/complete/movies")
DOWNLOAD_TV = Path("/srv/downloads/complete/tv")
MEDIA_MOVIES = Path("/srv/media/movies")
MEDIA_TV = Path("/srv/media/tv")
LOG_FILE = Path("/home/stone/homelab/logs/jellyfin-importer.log")

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".m4v"}
IGNORE_WORDS = {"sample", "trailer", "rarbg", "tgx", "torrentgalaxy"}
MIN_AGE_MINUTES = 30
PREFER_HARDLINKS = True

SHOW_ALIASES = {
    "iasip": ("It's Always Sunny in Philadelphia", "2005"),
    "its always sunny in philadelphia": ("It's Always Sunny in Philadelphia", "2005"),
}

MOVIE_OVERRIDES = {
    "millers crossing 1990": ("Miller's Crossing", "1990"),
    "miller s crossing 1990": ("Miller's Crossing", "1990"),
    "bluevelvet": ("Blue Velvet", "1986"),
    "blue velvet": ("Blue Velvet", "1986"),
    "pulp fiction": ("Pulp Fiction", "1994"),
    "pulp_fiction": ("Pulp Fiction", "1994"),
    "inglourious basterds": ("Inglourious Basterds", "2009"),
    "once upon a time in hollywood": ("Once Upon a Time... in Hollywood", "2019"),
    "the departed": ("The Departed", "2006"),
    "the color of money": ("The Color of Money", "1986"),
}

YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
TV_RE = re.compile(r"(?P<prefix>.*?)[. _-]*S(?P<season>\d{1,2})E(?P<episode>\d{1,2})(?P<rest>.*)", re.IGNORECASE)


@dataclass
class ImportResult:
    source: Path
    destination: Path
    action: str


def log(message: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def normalize_text(value: str) -> str:
    value = value.replace("_", " ").replace(".", " ")
    value = re.sub(r"[\[\]\(\){}]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def clean_title(value: str) -> str:
    value = normalize_text(value)
    junk_patterns = [
        r"\b(1080p|720p|480p|360p|2160p|4k|bluray|brrip|bdrip|webrip|web-dl|webdl|hdtv|dvdrip|hdrip)\b",
        r"\b(x264|x265|h264|h265|hevc|10bit|aac|ddp5 1|dts|ac3|ion10|galaxyrg265|tgx|rarbg|rartv|demand|xvid|ettv|evo)\b",
        r"\b(dc|proper|repack|extended|remastered)\b",
    ]
    for pattern in junk_patterns:
        value = re.sub(pattern, " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip(" -.")
    return value


def title_case_safely(value: str) -> str:
    small = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to", "vs"}
    words = value.split()
    out = []
    for i, word in enumerate(words):
        lw = word.lower()
        if i != 0 and lw in small:
            out.append(lw)
        else:
            out.append(word[:1].upper() + word[1:].lower())
    return " ".join(out)


def is_old_enough(path: Path) -> bool:
    return time.time() - path.stat().st_mtime >= MIN_AGE_MINUTES * 60


def is_video(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in VIDEO_EXTS


def is_probably_junk_video(path: Path) -> bool:
    name = path.name.lower()
    if any(word in name for word in IGNORE_WORDS):
        return True
    try:
        return path.stat().st_size < 100 * 1024 * 1024
    except FileNotFoundError:
        return True


def marker_path(source_dir: Path) -> Path:
    return source_dir / ".jellyfin-imported"


def already_imported(source_dir: Path) -> bool:
    return marker_path(source_dir).exists()


def write_marker(source_dir: Path, results: list[ImportResult], dry_run: bool) -> None:
    if dry_run:
        return
    data = {
        "imported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "results": [{"source": str(r.source), "destination": str(r.destination), "action": r.action} for r in results],
    }
    marker_path(source_dir).write_text(json.dumps(data, indent=2), encoding="utf-8")


def link_or_copy(src: Path, dest: Path, dry_run: bool) -> ImportResult:
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists():
        return ImportResult(src, dest, "exists")

    if dry_run:
        return ImportResult(src, dest, "would-hardlink")

    if PREFER_HARDLINKS:
        try:
            os.link(src, dest)
            return ImportResult(src, dest, "hardlinked")
        except OSError as exc:
            log(f"Hardlink failed, copying instead: {src} -> {dest} ({exc})")

    shutil.copy2(src, dest)
    return ImportResult(src, dest, "copied")


def movie_override_for(text: str) -> Optional[tuple[str, str]]:
    lower = normalize_text(text).lower()
    compact = lower.replace(" ", "")
    for fragment, result in MOVIE_OVERRIDES.items():
        normalized_fragment = normalize_text(fragment).lower()
        if normalized_fragment in lower or normalized_fragment.replace(" ", "") in compact:
            return result
    return None


def parse_movie_title_year(source_dir: Path, main_video: Path) -> Optional[tuple[str, str]]:
    text = f"{source_dir.name} {main_video.name}"
    override = movie_override_for(text)
    if override:
        return override

    year_match = YEAR_RE.search(text)
    if not year_match:
        return None

    year = year_match.group(1)
    title = title_case_safely(clean_title(text[: year_match.start()]))
    return (title, year) if title else None


def find_main_movie_file(source_dir: Path) -> Optional[Path]:
    videos = [p for p in source_dir.rglob("*") if is_video(p) and not is_probably_junk_video(p)]
    videos = [p for p in videos if is_old_enough(p)]
    return max(videos, key=lambda p: p.stat().st_size) if videos else None


def import_movie_dir(source_dir: Path, dry_run: bool) -> list[ImportResult]:
    if already_imported(source_dir):
        log(f"Skipping already imported movie folder: {source_dir}")
        return []

    main_video = find_main_movie_file(source_dir)
    if not main_video:
        log(f"No movie video found or files too new: {source_dir}")
        return []

    parsed = parse_movie_title_year(source_dir, main_video)
    if not parsed:
        log(f"Needs review, could not parse movie title/year: {source_dir}")
        return []

    title, year = parsed
    dest = MEDIA_MOVIES / f"{title} ({year})" / f"{title} ({year}){main_video.suffix.lower()}"
    result = link_or_copy(main_video, dest, dry_run)
    write_marker(source_dir, [result], dry_run)
    log(f"Movie {result.action}: {result.source} -> {result.destination}")
    return [result]


def parse_show_from_prefix(prefix: str) -> tuple[str, str]:
    cleaned = clean_title(prefix).lower()
    cleaned = re.sub(r"\bseason\b", "", cleaned).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)

    for alias, show in SHOW_ALIASES.items():
        if alias in cleaned:
            return show

    return title_case_safely(cleaned), ""


def parse_episode_title(rest: str) -> str:
    rest = rest.replace(".", " ").replace("_", " ").strip(" -")
    rest = re.sub(
        r"\b(WEBRip|WEB-DL|WEBDL|HDTV|BDRip|BluRay|DVDRip|x264|x265|XviD|ION10|DEMAND|ASAP|FQM|P0W4|AAC|10bit|720p|1080p|360p).*",
        "",
        rest,
        flags=re.IGNORECASE,
    )
    rest = re.sub(r"\s+", " ", rest).strip(" -")
    return title_case_safely(rest) if rest else ""


def import_tv_file(path: Path, dry_run: bool) -> Optional[ImportResult]:
    if not is_old_enough(path):
        log(f"Skipping too-new TV file: {path}")
        return None

    match = TV_RE.match(path.stem)
    if not match:
        log(f"Skipping TV file without SxxEyy pattern: {path}")
        return None

    season = int(match.group("season"))
    episode = int(match.group("episode"))
    show_title, show_year = parse_show_from_prefix(match.group("prefix"))
    episode_title = parse_episode_title(match.group("rest"))

    show_folder = f"{show_title} ({show_year})" if show_year else show_title
    filename = f"{show_folder} - S{season:02d}E{episode:02d}"
    if episode_title:
        filename += f" - {episode_title}"
    filename += path.suffix.lower()

    dest = MEDIA_TV / show_folder / f"Season {season:02d}" / filename
    result = link_or_copy(path, dest, dry_run)
    log(f"TV {result.action}: {result.source} -> {result.destination}")
    return result


def folder_contains_tv_episodes(source_dir: Path) -> bool:
    for p in source_dir.rglob("*"):
        if is_video(p) and re.search(r"S\d{1,2}E\d{1,2}", p.name, re.IGNORECASE):
            return True
    return False


def import_tv_dir(source_dir: Path, dry_run: bool) -> list[ImportResult]:
    if already_imported(source_dir):
        log(f"Skipping already imported TV folder: {source_dir}")
        return []
    if folder_contains_tv_episodes(source_dir):
        log(f"Skipping movie pass because folder contains TV episodes: {source_dir}")
        return []

    videos = [p for p in source_dir.rglob("*") if is_video(p) and not is_probably_junk_video(p)]
    results = [r for v in sorted(videos) if (r := import_tv_file(v, dry_run))]

    if results:
        write_marker(source_dir, results, dry_run)
    else:
        log(f"No TV episodes imported from: {source_dir}")

    return results


def iter_source_dirs(root: Path):
    if not root.exists():
        log(f"Source root does not exist: {root}")
        return
    for item in sorted(root.iterdir()):
        if item.is_dir():
            yield item
        elif is_video(item):
            log(f"Single loose file skipped for now; put it in a folder: {item}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Import completed downloads into Jellyfin library folders.")
    parser.add_argument("--apply", action="store_true", help="Actually create hardlinks/copies and marker files.")
    args = parser.parse_args()

    dry_run = not args.apply
    log(f"Starting Jellyfin import ({'dry-run' if dry_run else 'apply'})")

    total: list[ImportResult] = []
    for source_dir in iter_source_dirs(DOWNLOAD_MOVIES) or []:
        total.extend(import_movie_dir(source_dir, dry_run))
    for source_dir in iter_source_dirs(DOWNLOAD_TV) or []:
        total.extend(import_tv_dir(source_dir, dry_run))

    log(f"Finished Jellyfin import. Results: {len(total)}")
    if dry_run:
        log("Dry run only. Re-run with --apply to import.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
