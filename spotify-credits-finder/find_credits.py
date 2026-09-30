#!/usr/bin/env python3
"""Find the Spotify tracks a session musician, producer or engineer worked on.

Spotify shows song credits inside its apps, but its public Web API has no way
to search by credit. So this script collects credits from open music databases
and then finds each of those songs on Spotify:

  MusicBrainz  per-recording credits (drums, producer, mixing...). No key needed.
  Discogs      credits from album liner notes. Token optional but recommended.
  Genius       producer / writer / musician credits. Token required.
  Spotify      the person's own artist profile(s), plus matching every song
               found above to a Spotify track (by ISRC code when known).

Only the Python 3 standard library is used. See README.md for setup.
"""
from __future__ import annotations

import argparse
import base64
import csv
import difflib
import hashlib
import html
import json
import os
import re
import string
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Presets for Stefan, used when --name isn't given.
DEFAULT_NAME = "Stefan Litrownik"
DEFAULT_ALIASES = ["Stefan Lit", "Stefan A. Litrownik", "Stefan Adam Litrownik"]
DEFAULT_DISCOGS_IDS = [2707450]
DEFAULT_CONTACT = "https://github.com/tuskensp/node-red-contrib-aws"

ALL_SOURCES = ("musicbrainz", "discogs", "genius", "spotify")


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Names and titles
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    """Lowercase, strip accents/punctuation and Discogs' "Name (2)" suffixes."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"\s+\(\d+\)$", "", text.strip())
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


_FEAT_PAREN = re.compile(r"\s*[(\[](?:feat|ft|featuring|with)\b\.?[^)\]]*[)\]]", re.I)
_FEAT_TAIL = re.compile(r"\s+(?:feat|ft|featuring)\b\.?\s.*$", re.I)
_REMASTER = re.compile(
    r"\s*(?:-\s*|[(\[])(?:\d{4}\s+)?(?:digital(?:ly)?\s+)?remaster(?:ed)?(?:\s+\d{4})?(?:\s+version)?[)\]]?\s*$",
    re.I,
)


def strip_title(title: str) -> str:
    """Drop "(feat. X)" and "- Remastered 2011" style decorations."""
    title = _FEAT_PAREN.sub("", title or "")
    title = _FEAT_TAIL.sub("", title)
    return _REMASTER.sub("", title).strip()


def core_title(title: str) -> str:
    return normalize(strip_title(title))


class NameMatcher:
    """Decides whether a credited name is the person we're looking for.

    A name matches if it equals one of the known names, or has the same first
    and last word as a known name with extra middle names/initials in between
    ("Stefan A. Litrownik" matches "Stefan Litrownik").
    """

    def __init__(self, names=()):
        self.names: set[str] = set()
        self.first_last: set[tuple[str, str]] = set()
        for name in names:
            self.add(name)

    def add(self, name: str) -> None:
        n = normalize(name)
        if not n:
            return
        self.names.add(n)
        words = n.split()
        if len(words) >= 2 and len(words[0]) > 1:
            self.first_last.add((words[0], words[-1]))

    def matches(self, name: str) -> bool:
        n = normalize(name)
        if not n:
            return False
        if n in self.names:
            return True
        words = n.split()
        return len(words) >= 3 and (words[0], words[-1]) in self.first_last


def match_score(title: str, artists: list, cand_title: str, cand_artists: list) -> float:
    """0..1 similarity between a credited song and a Spotify candidate."""
    a, b = core_title(title), core_title(cand_title)
    if not a or not b:
        return 0.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    ours = {normalize(x) for x in artists or [] if normalize(x)}
    if not ours:
        return ratio if ratio >= 0.95 else 0.0
    theirs = {normalize(x) for x in cand_artists or [] if normalize(x)}
    same_artist = any(o == t or f" {o} " in f" {t} " or f" {t} " in f" {o} " for o in ours for t in theirs)
    return ratio if same_artist else 0.0


def earliest_year(*years: str) -> str:
    found = [y for y in years if y and y.isdigit() and y != "0"]
    return min(found) if found else ""


def nice_role(role: str) -> str:
    role = " ".join((role or "").split())
    return role[:1].upper() + role[1:]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Credit:
    """One source saying: the person has <role> on <title> by <artists>."""

    title: str
    artists: list
    role: str
    source: str
    source_url: str = ""
    album: str = ""
    year: str = ""
    isrcs: list = field(default_factory=list)
    spotify_id: str = ""
    album_only: bool = False  # credited on an album, but which tracks is unknown


@dataclass
class Song:
    """All credits for one song merged together, plus its Spotify match."""

    title: str
    artists: list
    album: str = ""
    year: str = ""
    album_only: bool = False
    roles: list = field(default_factory=list)
    sources: dict = field(default_factory=dict)
    isrcs: set = field(default_factory=set)
    spotify_id: str = ""
    spotify_match: str = ""

    @classmethod
    def from_credit(cls, credit: Credit) -> "Song":
        song = cls(credit.title, list(credit.artists), credit.album, credit.year, credit.album_only)
        song.add(credit)
        return song

    def add_role(self, role: str) -> None:
        role = nice_role(role)
        if role and role.lower() not in {r.lower() for r in self.roles}:
            self.roles.append(role)

    def add(self, credit: Credit) -> None:
        self.add_role(credit.role)
        if credit.source:
            self.sources.setdefault(credit.source, credit.source_url)
        self.isrcs.update(i.upper() for i in credit.isrcs if i)
        self.album = self.album or credit.album
        self.year = earliest_year(self.year, credit.year)
        self.artists = self.artists or list(credit.artists)
        if credit.spotify_id and not self.spotify_id:
            self.spotify_id = credit.spotify_id
            self.spotify_match = f"linked from {credit.source}"

    def merge(self, other: "Song") -> None:
        for role in other.roles:
            self.add_role(role)
        for source, url in other.sources.items():
            self.sources.setdefault(source, url)
        self.isrcs |= other.isrcs
        self.album = self.album or other.album
        self.year = earliest_year(self.year, other.year)

    def use_spotify_track(self, track: dict, how: str) -> None:
        self.spotify_id = track["id"]
        self.spotify_match = how
        self.title = track.get("name") or self.title
        names = [a.get("name", "") for a in track.get("artists") or [] if a]
        self.artists = names or self.artists
        album = track.get("album") or {}
        self.album = self.album or album.get("name", "")
        self.year = self.year or (album.get("release_date") or "")[:4]

    @property
    def spotify_url(self) -> str:
        if not self.spotify_id:
            return ""
        return f"https://open.spotify.com/{'album' if self.album_only else 'track'}/{self.spotify_id}"


def merge_credits(credits: list) -> list:
    """Group credits that describe the same song (same ISRC, Spotify ID or title+artist)."""
    songs: list[Song] = []
    by_key: dict = {}
    by_isrc: dict = {}
    by_spotify: dict = {}
    for c in credits:
        key = (c.album_only, core_title(c.title), normalize(c.artists[0]) if c.artists else "")
        isrcs = [i.upper() for i in c.isrcs if i]
        song = by_spotify.get((c.album_only, c.spotify_id)) if c.spotify_id else None
        song = song or next((by_isrc[i] for i in isrcs if i in by_isrc), None) or by_key.get(key)
        if song is None:
            song = Song.from_credit(c)
            songs.append(song)
        else:
            song.add(c)
        by_key.setdefault(key, song)
        for i in isrcs:
            by_isrc.setdefault(i, song)
        if c.spotify_id:
            by_spotify.setdefault((c.album_only, c.spotify_id), song)
    return songs


def dedupe_by_spotify(songs: list) -> list:
    """Different database entries can turn out to be the same Spotify track."""
    out, seen = [], {}
    for song in songs:
        key = (song.album_only, song.spotify_id)
        if song.spotify_id and key in seen:
            seen[key].merge(song)
            continue
        if song.spotify_id:
            seen[key] = song
        out.append(song)
    return out


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Http:
    """JSON over HTTP with per-host rate limiting, retries and an on-disk cache."""

    def __init__(self, user_agent: str, cache_dir: Path | None = None, refresh: bool = False):
        self.user_agent = user_agent
        self.cache_dir = cache_dir
        self.refresh = refresh
        self._last_call: dict = {}

    def get_json(self, url: str, headers: dict | None = None, min_interval: float = 0.0):
        cache_file = None
        if self.cache_dir:
            cache_file = self.cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".json")
            if cache_file.exists() and not self.refresh:
                return json.loads(cache_file.read_text(encoding="utf-8"))
        data = self._request(url, headers, None, min_interval)
        if cache_file:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(data), encoding="utf-8")
        return data

    def post_form(self, url: str, form: dict, headers: dict | None = None):
        body = urllib.parse.urlencode(form).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
        return self._request(url, headers, body, 0.0)

    def _request(self, url, headers, body, min_interval):
        host = urllib.parse.urlsplit(url).netloc
        for attempt in range(6):
            wait = self._last_call.get(host, 0.0) + min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_call[host] = time.monotonic()
            request = urllib.request.Request(
                url,
                data=body,
                headers={"User-Agent": self.user_agent, "Accept": "application/json", **(headers or {})},
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.loads(response.read().decode("utf-8") or "null")
            except urllib.error.HTTPError as err:
                err.close()
                if err.code == 404:
                    return None
                if err.code in (429, 500, 502, 503, 504) and attempt < 5:
                    delay = _retry_after(err) or 2 ** attempt
                    log(f"    {host} answered {err.code}, waiting {delay:.0f}s and retrying")
                    time.sleep(min(delay, 120))
                    continue
                raise
            except OSError:
                if attempt < 5:
                    time.sleep(2 ** attempt)
                    continue
                raise
        raise RuntimeError(f"Gave up on {url}")


def _retry_after(err: urllib.error.HTTPError) -> float:
    try:
        return float(err.headers.get("Retry-After") or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# MusicBrainz
# ---------------------------------------------------------------------------

MB_MODIFIERS = {"additional", "assistant", "associate", "co", "executive", "guest", "solo"}
MB_ROLE_NAMES = {
    "recording": "recording engineer",
    "mix": "mixing engineer",
    "mastering": "mastering engineer",
    "audio": "audio engineer",
    "sound": "sound engineer",
    "misc": "miscellaneous",
}


def mb_role(rel: dict) -> str:
    """Turn a MusicBrainz relationship into a readable role like "drums" or "co-producer"."""
    kind = rel.get("type") or "credited"
    attrs = [a for a in rel.get("attributes") or [] if a != "task"]
    mods = [a for a in attrs if a in MB_MODIFIERS]
    details = [re.sub(r"\s*\(.*?\)", "", a).strip() for a in attrs if a not in MB_MODIFIERS]
    if kind in ("instrument", "vocal", "performer") and details:
        base = ", ".join(details)
    elif kind == "vocal":
        base = "vocals"
    else:
        base = MB_ROLE_NAMES.get(kind, kind)
    if "co" in mods:
        base = "co-" + base
    prefix = " ".join(m for m in mods if m != "co")
    return f"{prefix} {base}".strip()


class MusicBrainz:
    API = "https://musicbrainz.org/ws/2"

    def __init__(self, http: Http, matcher: NameMatcher):
        self.http = http
        self.matcher = matcher

    def _get(self, path: str, **params):
        params["fmt"] = "json"
        url = f"{self.API}/{path}?{urllib.parse.urlencode(params)}"
        return self.http.get_json(url, min_interval=1.1) or {}  # MusicBrainz allows 1 request/second

    def find_artists(self, names: list) -> list:
        query = " OR ".join(f'artist:"{n}" OR alias:"{n}"' for n in names)
        found = []
        for artist in self._get("artist", query=query, limit=25).get("artists") or []:
            candidates = [artist.get("name", "")] + [a.get("name", "") for a in artist.get("aliases") or []]
            if any(self.matcher.matches(c) for c in candidates):
                found.append(artist)
        return found

    def collect(self, artist_ids: list) -> list:
        credits: list[Credit] = []
        for mbid in artist_ids:
            artist = self._get(f"artist/{mbid}", inc="aliases+recording-rels+release-rels+work-rels")
            if not artist:
                log(f"  MusicBrainz artist {mbid} not found")
                continue
            log(f"  MusicBrainz: {artist.get('name')} (https://musicbrainz.org/artist/{mbid})")
            for alias in artist.get("aliases") or []:
                self.matcher.add(alias.get("name", ""))

            recording_roles: dict = {}
            release_roles: dict = {}
            work_roles: dict = {}
            for rel in artist.get("relations") or []:
                target = rel.get("target-type")
                role = mb_role(rel)
                if target == "recording" and rel.get("recording"):
                    recording_roles.setdefault(rel["recording"]["id"], []).append(role)
                elif target == "release" and rel.get("release"):
                    release_roles.setdefault(rel["release"]["id"], []).append(role)
                elif target == "work" and rel.get("work"):
                    work_roles.setdefault(rel["work"]["id"], []).append(role)

            # Songwriting credits live on "works"; each work links to its recordings.
            for work_id, roles in work_roles.items():
                work = self._get(f"work/{work_id}", inc="recording-rels")
                for rel in work.get("relations") or []:
                    if rel.get("target-type") == "recording" and rel.get("recording"):
                        recording_roles.setdefault(rel["recording"]["id"], []).extend(roles)

            total = len(recording_roles)
            for n, (rec_id, roles) in enumerate(recording_roles.items(), 1):
                if n % 20 == 0 or n == total:
                    log(f"    looked up {n}/{total} MusicBrainz recordings")
                rec = self._get(f"recording/{rec_id}", inc="artist-credits+isrcs+releases")
                if rec:
                    credits += [self._recording_credit(rec, role) for role in dict.fromkeys(roles)]

            for release_id, roles in release_roles.items():
                credits += self._release_credits(release_id, roles)

            credits += self._own_recordings(mbid)
        return credits

    def _recording_credit(self, rec: dict, role: str) -> Credit:
        releases = sorted(rec.get("releases") or [], key=lambda r: r.get("date") or "9999")
        return Credit(
            title=rec.get("title", ""),
            artists=[ac.get("name", "") for ac in rec.get("artist-credit") or []],
            role=role,
            source="MusicBrainz",
            source_url=f"https://musicbrainz.org/recording/{rec['id']}",
            album=releases[0].get("title", "") if releases else "",
            year=(rec.get("first-release-date") or "")[:4],
            isrcs=list(rec.get("isrcs") or []),
        )

    def _release_credits(self, release_id: str, roles: list) -> list:
        """A credit on a whole release applies to every track on it."""
        release = self._get(f"release/{release_id}", inc="recordings+artist-credits+isrcs")
        if not release:
            return []
        album_artists = [ac.get("name", "") for ac in release.get("artist-credit") or []]
        credits = []
        for medium in release.get("media") or []:
            for track in medium.get("tracks") or []:
                rec = track.get("recording") or {}
                artists = [ac.get("name", "") for ac in track.get("artist-credit") or rec.get("artist-credit") or []]
                for role in dict.fromkeys(roles):
                    credits.append(Credit(
                        title=track.get("title") or rec.get("title", ""),
                        artists=artists or album_artists,
                        role=f"{role} (album credit)",
                        source="MusicBrainz",
                        source_url=f"https://musicbrainz.org/release/{release_id}",
                        album=release.get("title", ""),
                        year=(release.get("date") or "")[:4],
                        isrcs=list(rec.get("isrcs") or []),
                    ))
        return credits

    def _own_recordings(self, mbid: str) -> list:
        """Recordings where the person is (one of) the main artists."""
        credits, offset = [], 0
        while True:
            page = self._get("recording", artist=mbid, inc="isrcs+artist-credits", limit=100, offset=offset)
            recordings = page.get("recordings") or []
            for rec in recordings:
                credits.append(Credit(
                    title=rec.get("title", ""),
                    artists=[ac.get("name", "") for ac in rec.get("artist-credit") or []],
                    role="artist",
                    source="MusicBrainz",
                    source_url=f"https://musicbrainz.org/recording/{rec['id']}",
                    year=(rec.get("first-release-date") or "")[:4],
                    isrcs=list(rec.get("isrcs") or []),
                ))
            offset += len(recordings)
            if not recordings or offset >= int(page.get("recording-count") or 0):
                return credits


# ---------------------------------------------------------------------------
# Discogs
# ---------------------------------------------------------------------------

def _position(pos: str) -> str:
    return re.sub(r"\s+", "", (pos or "").lower())


def flatten_tracklist(items: list) -> list:
    tracks = []
    for item in items or []:
        kind = item.get("type_") or "track"
        if kind == "track":
            tracks.append(item)
        elif kind == "index":
            tracks += [s for s in item.get("sub_tracks") or [] if (s.get("type_") or "track") == "track"]
    return tracks


def select_tracks(spec: str, tracks: list) -> list | None:
    """Resolve a Discogs track spec like "A1 to A3, B2". Empty spec means every track.

    Returns None if the spec can't be matched to this tracklist.
    """
    spec = (spec or "").strip()
    if not spec:
        return list(tracks)
    positions = [_position(t.get("position", "")) for t in tracks]
    chosen = []
    for part in re.split(r"\s*[,;&]\s*", spec):
        bounds = re.split(r"\s+to\s+", part.strip(), flags=re.I)
        keys = [_position(b) for b in bounds]
        if len(keys) == 2 and all(k in positions for k in keys):
            i, j = positions.index(keys[0]), positions.index(keys[1])
            chosen += tracks[min(i, j):max(i, j) + 1]
        elif len(keys) == 1 and keys[0] in positions:
            chosen.append(tracks[positions.index(keys[0])])
    unique = list({id(t): t for t in chosen}.values())
    return unique or None


def _discogs_name(artist: dict) -> str:
    return re.sub(r"\s+\(\d+\)$", "", artist.get("name", "")).strip()


GENERIC_DISCOGS_ROLES = {"main", "appearance", "trackappearance", "unofficialrelease"}


class Discogs:
    API = "https://api.discogs.com"

    def __init__(self, http: Http, matcher: NameMatcher, token: str = ""):
        self.http = http
        self.matcher = matcher
        self.token = token
        self.known_ids: set = set()
        self._seen_releases: set = set()

    def _get(self, path: str, **params):
        headers = {"Authorization": f"Discogs token={self.token}"} if self.token else {}
        url = f"{self.API}/{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        # Discogs allows 60 requests/minute with a token, 25 without.
        return self.http.get_json(url, headers=headers, min_interval=1.1 if self.token else 2.5) or {}

    def find_artists(self, names: list) -> list:
        if not self.token:
            log("  Discogs: no DISCOGS_TOKEN, so only searching the known artist IDs")
            return []
        ids = []
        for name in names:
            for result in self._get("database/search", q=name, type="artist", per_page=25).get("results") or []:
                if self.matcher.matches(result.get("title", "")) and result.get("id") not in ids:
                    ids.append(result["id"])
        return ids

    def collect(self, artist_ids: list, include_groups: bool = True) -> list:
        profiles, groups = [], []
        queue = list(dict.fromkeys(artist_ids))
        while queue:
            artist_id = queue.pop(0)
            if artist_id in self.known_ids:
                continue
            self.known_ids.add(artist_id)
            artist = self._get(f"artists/{artist_id}")
            if not artist:
                log(f"  Discogs artist {artist_id} not found")
                continue
            log(f"  Discogs: {artist.get('name')} (https://www.discogs.com/artist/{artist_id})")
            profiles.append(artist_id)
            self.matcher.add(_discogs_name(artist))
            for variation in artist.get("namevariations") or []:
                self.matcher.add(variation)
            for alias in artist.get("aliases") or []:
                self.matcher.add(_discogs_name(alias))
                queue.append(alias["id"])  # a pseudonym has its own profile and credits
            groups += artist.get("groups") or []

        credits = []
        for artist_id in profiles:
            credits += self._artist_credits(artist_id)
        if include_groups:
            for group in {g["id"]: g for g in groups}.values():
                name = _discogs_name(group)
                log(f"  Discogs: releases by {name} (band he was in)")
                credits += self._artist_credits(group["id"], group_name=name)
        return credits

    def _artist_releases(self, artist_id: int) -> list:
        items, page = [], 1
        while True:
            data = self._get(f"artists/{artist_id}/releases", per_page=100, page=page)
            items += data.get("releases") or []
            if page >= int((data.get("pagination") or {}).get("pages") or 1):
                return items
            page += 1

    def _artist_credits(self, artist_id: int, group_name: str = "") -> list:
        items = self._artist_releases(artist_id)
        if group_name:
            items = [i for i in items if (i.get("role") or "").lower() == "main"]
        credits = []
        for n, item in enumerate(items, 1):
            if n % 20 == 0 or n == len(items):
                log(f"    checked {n}/{len(items)} Discogs releases")
            fallback = f"band member ({group_name})" if group_name else ""
            if item.get("type") == "master":
                release_ids = [item.get("main_release")]
            else:
                release_ids = [item.get("id")]
            found = self._release_credits(release_ids[0], fallback)
            if found is None:
                continue  # already processed this release
            if not found and item.get("type") == "master":
                # The credit may only be printed on one particular pressing.
                versions = self._get(f"masters/{item['id']}/versions", per_page=10).get("versions") or []
                for version in versions[:5]:
                    found = self._release_credits(version.get("id"), fallback) or []
                    if found:
                        break
            if not found:
                found = self._listing_credit(item)
            credits += found
        return credits

    def _listing_credit(self, item: dict) -> list:
        """We couldn't find the credit inside the release data; use the artist-page listing."""
        role = item.get("role") or ""
        role = "credited" if role.lower() in GENERIC_DISCOGS_ROLES else role
        kind = "master" if item.get("type") == "master" else "release"
        url = f"https://www.discogs.com/{kind}/{item.get('id')}"
        common = dict(role=role, source="Discogs", source_url=url, album=item.get("title", ""),
                      year=str(item.get("year") or ""))
        artists = [a.strip() for a in re.split(r",| & ", item.get("artist") or "") if a.strip()]
        if item.get("trackinfo"):
            return [Credit(title=item["trackinfo"], artists=artists, **common)]
        return [Credit(title=item.get("title", ""), artists=artists, album_only=True, **common)]

    def _release_credits(self, release_id, fallback_role: str = "") -> list | None:
        if not release_id:
            return []
        if release_id in self._seen_releases:
            return None
        self._seen_releases.add(release_id)
        release = self._get(f"releases/{release_id}")
        if not release:
            return []
        tracks = flatten_tracklist(release.get("tracklist"))
        album = release.get("title", "")
        album_artists = [_discogs_name(a) for a in release.get("artists") or []]
        common = dict(source="Discogs", source_url=release.get("uri") or f"https://www.discogs.com/release/{release_id}",
                      album=album, year=str(release.get("year") or ""))

        def is_them(a: dict) -> bool:
            return (a.get("id") in self.known_ids or self.matcher.matches(a.get("anv") or "")
                    or self.matcher.matches(a.get("name", "")))

        def track_credit(track: dict, role: str) -> Credit:
            artists = [_discogs_name(a) for a in track.get("artists") or []] or album_artists
            return Credit(title=track.get("title", ""), artists=artists, role=role, **common)

        credits = []
        if any(is_them(a) for a in release.get("artists") or []):
            credits += [track_credit(t, "artist") for t in tracks]
        for extra in release.get("extraartists") or []:
            if not is_them(extra):
                continue
            role = extra.get("role") or "credited"
            spec = (extra.get("tracks") or "").strip()
            chosen = select_tracks(spec, tracks)
            if chosen is None:
                credits.append(Credit(title=album, artists=album_artists, album_only=True,
                                      role=f"{role} (tracks {spec})", **common))
                continue
            suffix = " (album credit)" if not spec and len(tracks) > 1 else ""
            credits += [track_credit(t, role + suffix) for t in chosen]
        for track in tracks:
            if any(is_them(a) for a in track.get("artists") or []):
                credits.append(track_credit(track, "artist"))
            for extra in track.get("extraartists") or []:
                if is_them(extra):
                    credits.append(track_credit(track, extra.get("role") or "credited"))
        if not credits and fallback_role:
            credits = [track_credit(t, fallback_role) for t in tracks]
        return credits


# ---------------------------------------------------------------------------
# Genius
# ---------------------------------------------------------------------------

SPOTIFY_TRACK_URL = re.compile(r"open\.spotify\.com/(?:intl-[\w-]+/)?track/([A-Za-z0-9]{22})")


class Genius:
    API = "https://api.genius.com"

    def __init__(self, http: Http, matcher: NameMatcher, token: str):
        self.http = http
        self.matcher = matcher
        self.token = token

    def _get(self, path: str, **params):
        url = f"{self.API}/{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        data = self.http.get_json(url, headers={"Authorization": f"Bearer {self.token}"}, min_interval=0.3)
        return (data or {}).get("response") or {}

    @staticmethod
    def _people(song: dict) -> list:
        people = [song.get("primary_artist") or {}]
        for key in ("primary_artists", "featured_artists", "producer_artists", "writer_artists"):
            people += song.get(key) or []
        for performance in song.get("custom_performances") or []:
            people += performance.get("artists") or []
        return [p for p in people if p]

    def find_artists(self, names: list) -> set:
        ids = set()
        for name in names:
            for hit in self._get("search", q=name).get("hits") or []:
                result = hit.get("result") or {}
                for person in [result.get("primary_artist") or {}] + (result.get("featured_artists") or []):
                    if person and self.matcher.matches(person.get("name", "")):
                        ids.add(person["id"])
        return ids

    def discover_from_credits(self, credits: list, tries: int = 6) -> set:
        """Find the person's Genius ID by checking songs we already know they're on."""
        seen = set()
        for credit in credits:
            if credit.album_only or not credit.artists:
                continue
            key = (core_title(credit.title), normalize(credit.artists[0]))
            if key in seen:
                continue
            seen.add(key)
            if len(seen) > tries:
                break
            for hit in (self._get("search", q=f"{strip_title(credit.title)} {credit.artists[0]}").get("hits") or [])[:3]:
                result = hit.get("result") or {}
                if core_title(result.get("title", "")) != key[0]:
                    continue
                song = self._get(f"songs/{result['id']}", text_format="plain").get("song") or {}
                ids = {p["id"] for p in self._people(song) if p.get("id") and self.matcher.matches(p.get("name", ""))}
                if ids:
                    return ids
        return set()

    def collect(self, artist_ids) -> list:
        song_ids = []
        for artist_id in artist_ids:
            page = 1
            while page:
                data = self._get(f"artists/{artist_id}/songs", per_page=50, page=page, sort="title")
                song_ids += [s["id"] for s in data.get("songs") or [] if s.get("id") not in song_ids]
                page = data.get("next_page")
            log(f"  Genius: artist {artist_id} is credited on {len(song_ids)} songs")
        credits = []
        for n, song_id in enumerate(song_ids, 1):
            if n % 20 == 0 or n == len(song_ids):
                log(f"    looked up {n}/{len(song_ids)} Genius songs")
            song = self._get(f"songs/{song_id}", text_format="plain").get("song")
            if song:
                credits += self.song_credits(song, set(artist_ids))
        return credits

    def song_credits(self, song: dict, artist_ids: set) -> list:
        def is_them(person: dict) -> bool:
            return person.get("id") in artist_ids or self.matcher.matches(person.get("name", ""))

        roles = []
        primaries = (song.get("primary_artists") or []) or [song.get("primary_artist") or {}]
        if any(is_them(p) for p in primaries if p):
            roles.append("artist")
        if any(is_them(p) for p in song.get("featured_artists") or []):
            roles.append("featured artist")
        if any(is_them(p) for p in song.get("producer_artists") or []):
            roles.append("producer")
        if any(is_them(p) for p in song.get("writer_artists") or []):
            roles.append("songwriter")
        for performance in song.get("custom_performances") or []:
            if any(is_them(p) for p in performance.get("artists") or []):
                roles.append(performance.get("label") or "credited")

        spotify_id = ""
        for media in song.get("media") or []:
            found = SPOTIFY_TRACK_URL.search(media.get("url") or "")
            if media.get("provider") == "spotify" and found:
                spotify_id = found.group(1)
        artists = [p.get("name", "") for p in primaries if p] + [p.get("name", "") for p in song.get("featured_artists") or []]
        common = dict(
            title=song.get("title", ""),
            artists=[a for a in artists if a],
            source="Genius",
            source_url=song.get("url", ""),
            album=(song.get("album") or {}).get("name", ""),
            year=(song.get("release_date") or "")[:4],
            spotify_id=spotify_id,
        )
        return [Credit(role=role, **common) for role in roles or ["credited"]]


# ---------------------------------------------------------------------------
# Spotify
# ---------------------------------------------------------------------------

class SpotifyAuthError(RuntimeError):
    pass


class Spotify:
    API = "https://api.spotify.com/v1"
    TOKEN_URL = "https://accounts.spotify.com/api/token"
    SEARCH_LIMIT = 10  # the most Spotify allows for development-mode apps since February 2026

    def __init__(self, http: Http, client_id: str, client_secret: str, market: str = "US"):
        self.http = http
        self.client_id = client_id
        self.client_secret = client_secret
        self.market = market
        self._token = ""
        self._expires = 0.0

    def _auth_header(self) -> dict:
        if not self._token or time.time() > self._expires - 60:
            basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            try:
                data = self.http.post_form(self.TOKEN_URL, {"grant_type": "client_credentials"},
                                           headers={"Authorization": f"Basic {basic}"})
            except urllib.error.HTTPError as err:
                raise SpotifyAuthError(
                    f"Spotify rejected SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET (HTTP {err.code})") from err
            self._token = data["access_token"]
            self._expires = time.time() + float(data.get("expires_in") or 3600)
        return {"Authorization": f"Bearer {self._token}"}

    def get(self, path_or_url: str, **params) -> dict:
        url = path_or_url if path_or_url.startswith("http") else f"{self.API}/{path_or_url}"
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        try:
            return self.http.get_json(url, headers=self._auth_header(), min_interval=0.1) or {}
        except urllib.error.HTTPError as err:
            if err.code == 403:
                raise SpotifyAuthError(
                    "Spotify refused the request (HTTP 403). Development-mode apps only work while the "
                    "app owner has Spotify Premium; check the app at https://developer.spotify.com/dashboard"
                ) from err
            if err.code != 401:
                raise
            self._token = ""  # expired; get a fresh one and retry once
            return self.http.get_json(url, headers=self._auth_header(), min_interval=0.1) or {}

    def search(self, query: str, kind: str) -> list:
        data = self.get("search", q=query, type=kind, limit=self.SEARCH_LIMIT, market=self.market)
        return [item for item in (data.get(f"{kind}s") or {}).get("items") or [] if item]

    def _pages(self, path: str, **params):
        url = path
        while url:
            page = self.get(url, **params)
            params = {}
            yield from (item for item in page.get("items") or [] if item)
            url = page.get("next")

    def track_by_isrc(self, isrc: str, title: str) -> dict | None:
        best, best_score = None, 0.5  # ISRCs are exact, but guard against bad data
        for track in self.search(f"isrc:{isrc}", "track"):
            score = difflib.SequenceMatcher(None, core_title(title), core_title(track.get("name", ""))).ratio()
            if (track.get("external_ids") or {}).get("isrc", "").upper() == isrc.upper():
                score += 0.5
            if score > best_score:
                best, best_score = track, score
        return best

    def best_track(self, title: str, artists: list) -> dict | None:
        query = " ".join(filter(None, [strip_title(title), artists[0] if artists else ""]))
        return self._best(self.search(query, "track"), title, artists)

    def best_album(self, title: str, artists: list) -> dict | None:
        query = " ".join(filter(None, [strip_title(title), artists[0] if artists else ""]))
        return self._best(self.search(query, "album"), title, artists)

    @staticmethod
    def _best(items: list, title: str, artists: list, threshold: float = 0.8) -> dict | None:
        best, best_score = None, threshold
        for item in items:
            names = [a.get("name", "") for a in item.get("artists") or [] if a]
            score = match_score(title, artists, item.get("name", ""), names)
            if score > best_score:
                best, best_score = item, score
        return best

    def album_tracks(self, album_id: str) -> list:
        return list(self._pages(f"albums/{album_id}/tracks", market=self.market))

    def find_artists(self, names: list, matcher: NameMatcher) -> list:
        found = {}
        for name in names:
            for artist in self.search(name, "artist"):
                if matcher.matches(artist.get("name", "")):
                    found.setdefault(artist["id"], artist)
        return list(found.values())

    def artist(self, artist_id: str) -> dict:
        return self.get(f"artists/{artist_id}")

    def profile_credits(self, artist: dict) -> list:
        """Every track on Spotify that lists this artist profile."""
        artist_id = artist["id"]
        albums = list(self._pages(f"artists/{artist_id}/albums",
                                  include_groups="album,single,appears_on,compilation", market=self.market))
        credits = []
        for album in albums:
            for track in self.album_tracks(album["id"]):
                ids = [a.get("id") for a in track.get("artists") or [] if a]
                if artist_id not in ids:
                    continue
                credits.append(Credit(
                    title=track.get("name", ""),
                    artists=[a.get("name", "") for a in track.get("artists") or [] if a],
                    role="artist" if ids[0] == artist_id else "featured artist",
                    source="Spotify",
                    source_url=f"https://open.spotify.com/artist/{artist_id}",
                    album=album.get("name", ""),
                    year=(album.get("release_date") or "")[:4],
                    spotify_id=track.get("id", ""),
                ))
        return credits


def match_on_spotify(songs: list, spotify: Spotify, expand_albums: bool = False) -> list:
    todo = [s for s in songs if not s.spotify_id]
    for n, song in enumerate(todo, 1):
        if n % 20 == 0 or n == len(todo):
            log(f"    searched Spotify for {n}/{len(todo)} songs")
        try:
            if song.album_only:
                album = spotify.best_album(song.album or song.title, song.artists)
                if album:
                    song.spotify_id, song.spotify_match = album["id"], "album search"
                continue
            for isrc in sorted(song.isrcs):
                track = spotify.track_by_isrc(isrc, song.title)
                if track:
                    song.use_spotify_track(track, "ISRC")
                    break
            else:
                track = spotify.best_track(song.title, song.artists)
                if track:
                    song.use_spotify_track(track, "title + artist search")
        except urllib.error.HTTPError as err:
            log(f"    Spotify lookup failed for {song.title!r}: HTTP {err.code}")

    if not expand_albums:
        return songs
    expanded = []
    for song in songs:
        if not (song.album_only and song.spotify_id):
            expanded.append(song)
            continue
        for track in spotify.album_tracks(song.spotify_id):
            item = Song(track.get("name", ""), [a.get("name", "") for a in track.get("artists") or [] if a],
                        song.album, song.year, roles=list(song.roles), sources=dict(song.sources))
            item.spotify_id, item.spotify_match = track["id"], "track on credited album"
            expanded.append(item)
    return expanded


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def sort_songs(songs: list) -> list:
    return sorted(songs, key=lambda s: (-(int(s.year) if s.year.isdigit() else 0),
                                        normalize(", ".join(s.artists)), normalize(s.title)))


CSV_FIELDS = ["title", "artists", "roles", "album", "year", "spotify_url", "spotify_match",
              "credited_on_album_only", "isrcs", "sources", "source_urls"]


def write_csv(songs: list, path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for s in songs:
            writer.writerow({
                "title": s.title,
                "artists": ", ".join(s.artists),
                "roles": "; ".join(s.roles),
                "album": s.album,
                "year": s.year,
                "spotify_url": s.spotify_url,
                "spotify_match": s.spotify_match,
                "credited_on_album_only": "yes" if s.album_only else "",
                "isrcs": " ".join(sorted(s.isrcs)),
                "sources": ", ".join(s.sources),
                "source_urls": " ".join(u for u in s.sources.values() if u),
            })


def write_links(songs: list, path: Path) -> int:
    urls = list(dict.fromkeys(s.spotify_url for s in songs if s.spotify_url and not s.album_only))
    path.write_text("\n".join(urls) + ("\n" if urls else ""), encoding="utf-8")
    return len(urls)


HTML_PAGE = string.Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title</title>
<style>
  :root { --bg:#fff; --fg:#191414; --muted:#6a6a6a; --line:#e6e6e6; --stripe:#fafafa; --accent:#1a8f47; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#121212; --fg:#f0f0f0; --muted:#a7a7a7; --line:#2a2a2a; --stripe:#181818; --accent:#1ed760; }
  }
  body { margin:0; padding:24px 16px 48px; background:var(--bg); color:var(--fg);
         font:15px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  main { max-width:1120px; margin:0 auto; }
  h1 { font-size:26px; margin:0 0 4px; }
  .summary { color:var(--muted); margin:0 0 16px; }
  .note { color:var(--muted); font-size:13px; margin:4px 0; }
  input { width:100%; max-width:420px; box-sizing:border-box; padding:9px 12px; font:inherit;
          color:var(--fg); background:var(--bg); border:1px solid var(--line); border-radius:8px; }
  .table { overflow-x:auto; margin-top:14px; }
  table { border-collapse:collapse; width:100%; }
  th, td { text-align:left; vertical-align:top; padding:8px 10px; border-bottom:1px solid var(--line); }
  th { font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted);
       cursor:pointer; user-select:none; white-space:nowrap; }
  tbody tr:nth-child(even) td { background:var(--stripe); }
  td.year { white-space:nowrap; }
  a { color:inherit; }
  a.play { color:var(--accent); font-weight:600; text-decoration:none; white-space:nowrap; }
  .missing { color:var(--muted); font-size:13px; }
</style>
</head>
<body>
<main>
  <h1>$heading</h1>
  <p class="summary">$summary</p>
  $profiles
  <input id="filter" type="search" placeholder="Filter by song, artist, role, album&hellip;" aria-label="Filter">
  <div class="table">
  <table id="songs">
    <thead><tr><th>Spotify</th><th>Song</th><th>Artist</th><th>Role</th><th>Album</th><th>Year</th><th>Found in</th></tr></thead>
    <tbody>
$rows
    </tbody>
  </table>
  </div>
  <p class="note">Generated $generated from MusicBrainz, Discogs, Genius and Spotify data.
  Credits are only as complete as those databases &mdash; missing songs can be added there.</p>
</main>
<script>
  const rows = Array.from(document.querySelectorAll("#songs tbody tr"));
  document.getElementById("filter").addEventListener("input", (e) => {
    const q = e.target.value.toLowerCase();
    rows.forEach((r) => { r.hidden = !r.textContent.toLowerCase().includes(q); });
  });
  document.querySelectorAll("#songs th").forEach((th, col) => {
    let asc = true;
    th.addEventListener("click", () => {
      const body = document.querySelector("#songs tbody");
      rows.sort((a, b) => a.cells[col].textContent.localeCompare(b.cells[col].textContent, undefined, {numeric: true}) * (asc ? 1 : -1));
      asc = !asc;
      rows.forEach((r) => body.appendChild(r));
    });
  });
</script>
</body>
</html>
""")


def write_html(songs: list, path: Path, person: str, profiles: list) -> None:
    esc = html.escape
    rows = []
    for s in songs:
        if s.spotify_url:
            label = "Open album" if s.album_only else "Play"
            spotify = f'<a class="play" href="{esc(s.spotify_url)}">{label} &#9654;</a>'
        else:
            spotify = '<span class="missing">&mdash;</span>'
        sources = ", ".join(
            f'<a href="{esc(url)}">{esc(name)}</a>' if url else esc(name) for name, url in s.sources.items())
        title = esc(s.title) + (' <span class="missing">(album)</span>' if s.album_only else "")
        rows.append(
            f"      <tr><td>{spotify}</td><td>{title}</td><td>{esc(', '.join(s.artists))}</td>"
            f"<td>{esc('; '.join(s.roles))}</td><td>{esc(s.album)}</td><td class=\"year\">{esc(s.year)}</td>"
            f"<td>{sources}</td></tr>")
    on_spotify = sum(1 for s in songs if s.spotify_url)
    profile_html = ""
    if profiles:
        links = ", ".join(f'<a href="https://open.spotify.com/artist/{esc(p["id"])}">{esc(p.get("name", ""))}</a>'
                          for p in profiles)
        profile_html = (f'<p class="note">Also included every track on these Spotify artist profiles: {links}. '
                        "Check they're really the same person.</p>")
    path.write_text(HTML_PAGE.substitute(
        title=esc(f"{person} credits"),
        heading=esc(f"Songs {person} worked on"),
        summary=f"{len(songs)} songs found &middot; {on_spotify} linked to Spotify",
        profiles=profile_html,
        rows="\n".join(rows),
        generated=time.strftime("%Y-%m-%d"),
    ), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", normalize(text)).strip("-") or "results"


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Find every Spotify track someone is credited on.")
    p.add_argument("--name", help=f"person to search for (default: {DEFAULT_NAME})")
    p.add_argument("--alias", action="append", help="another name they are credited as (repeatable)")
    p.add_argument("--sources", default=",".join(ALL_SOURCES),
                   help="comma-separated: musicbrainz,discogs,genius,spotify (default: all)")
    p.add_argument("--musicbrainz-id", action="append", default=[], help="MusicBrainz artist MBID (repeatable)")
    p.add_argument("--discogs-id", action="append", type=int, help="Discogs artist ID (repeatable)")
    p.add_argument("--genius-id", action="append", type=int, default=[], help="Genius artist ID (repeatable)")
    p.add_argument("--spotify-artist-id", action="append", default=[],
                   help="only use these Spotify artist profiles instead of searching by name (repeatable)")
    p.add_argument("--no-groups", action="store_true", help="skip releases by bands they were a member of")
    p.add_argument("--expand-albums", action="store_true",
                   help="list every track of albums where only an album-level credit was found")
    p.add_argument("--market", default="US", help="Spotify country code to match tracks in (default: US)")
    p.add_argument("--out", type=Path, help="output folder (default: output/<name>)")
    p.add_argument("--refresh", action="store_true", help="ignore cached API responses")
    p.add_argument("--contact", default=DEFAULT_CONTACT,
                   help="URL or email sent in the User-Agent, as MusicBrainz and Discogs ask")
    args = p.parse_args(argv)

    if args.name is None:
        args.name = DEFAULT_NAME
        args.alias = list(DEFAULT_ALIASES) if args.alias is None else args.alias
        args.discogs_id = list(DEFAULT_DISCOGS_IDS) if args.discogs_id is None else args.discogs_id
    args.alias = args.alias or []
    args.discogs_id = args.discogs_id or []
    args.sources = {s.strip().lower() for s in args.sources.split(",") if s.strip()}
    unknown = args.sources - set(ALL_SOURCES)
    if unknown:
        p.error(f"unknown source(s): {', '.join(sorted(unknown))}")
    args.out = args.out or HERE / "output" / slug(args.name)
    return args


def run(args, http: Http) -> dict:
    names = list(dict.fromkeys([args.name] + args.alias))
    matcher = NameMatcher(names)
    credits: list[Credit] = []

    # A failing source is logged and skipped so the others still produce a report.
    if "musicbrainz" in args.sources:
        log("Searching MusicBrainz...")
        try:
            mb = MusicBrainz(http, matcher)
            ids = list(dict.fromkeys(args.musicbrainz_id + [a["id"] for a in mb.find_artists(names)]))
            if not ids:
                log("  no matching MusicBrainz artist found")
            credits += mb.collect(ids)
        except Exception as err:
            log(f"  MusicBrainz failed, skipping it: {err!r}")

    if "discogs" in args.sources:
        log("Searching Discogs...")
        try:
            discogs = Discogs(http, matcher, os.environ.get("DISCOGS_TOKEN", ""))
            ids = list(dict.fromkeys(args.discogs_id + discogs.find_artists(names)))
            credits += discogs.collect(ids, include_groups=not args.no_groups)
        except Exception as err:
            log(f"  Discogs failed, skipping it: {err!r}")

    genius_token = os.environ.get("GENIUS_ACCESS_TOKEN", "")
    if "genius" in args.sources and not genius_token:
        log("Skipping Genius (no GENIUS_ACCESS_TOKEN set)")
    elif "genius" in args.sources:
        log("Searching Genius...")
        try:
            genius = Genius(http, matcher, genius_token)
            ids = set(args.genius_id) | genius.find_artists(names)
            if not ids:
                ids = genius.discover_from_credits(credits)
            if ids:
                credits += genius.collect(sorted(ids))
            else:
                log("  couldn't find them on Genius (pass --genius-id if you know it)")
        except Exception as err:
            log(f"  Genius failed, skipping it: {err!r}")

    spotify = None
    profiles: list = []
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
    if client_id and client_secret:
        spotify = Spotify(http, client_id, client_secret, args.market)
    else:
        log("No SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET set: results won't be linked to Spotify.")

    if spotify and "spotify" in args.sources:
        log("Looking for their own Spotify artist profiles...")
        try:
            if args.spotify_artist_id:
                profiles = [a for a in (spotify.artist(i) for i in args.spotify_artist_id) if a]
            else:
                profiles = spotify.find_artists(names, matcher)
            for profile in profiles:
                log(f"  Spotify: {profile.get('name')} (https://open.spotify.com/artist/{profile['id']})")
                credits += spotify.profile_credits(profile)
        except SpotifyAuthError as err:
            log(f"  {err}\n  Continuing without Spotify links.")
            spotify = None
        except Exception as err:
            log(f"  Spotify artist lookup failed, skipping it: {err!r}")

    songs = merge_credits(credits)
    log(f"Found {len(songs)} songs from {len(credits)} credits.")
    if spotify and songs:
        log("Finding each song on Spotify...")
        try:
            songs = dedupe_by_spotify(match_on_spotify(songs, spotify, args.expand_albums))
        except SpotifyAuthError as err:
            log(f"  {err}\n  Continuing without Spotify links.")
    songs = sort_songs(songs)

    args.out.mkdir(parents=True, exist_ok=True)
    write_csv(songs, args.out / "credits.csv")
    write_html(songs, args.out / "credits.html", args.name, profiles)
    link_count = write_links(songs, args.out / "spotify_links.txt")
    return {"songs": songs, "profiles": profiles, "links": link_count}


def main(argv=None) -> int:
    load_dotenv(HERE / ".env")
    args = parse_args(argv)
    user_agent = f"SpotifyCreditsFinder/1.0 ( {args.contact} )"
    http = Http(user_agent, cache_dir=HERE / ".cache", refresh=args.refresh)
    result = run(args, http)
    songs = result["songs"]
    on_spotify = sum(1 for s in songs if s.spotify_url)
    print(f"\n{len(songs)} songs found for {args.name}; {on_spotify} linked to Spotify.")
    print(f"  Report:        {args.out / 'credits.html'}")
    print(f"  Spreadsheet:   {args.out / 'credits.csv'}")
    print(f"  Spotify links: {args.out / 'spotify_links.txt'} ({result['links']} tracks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
