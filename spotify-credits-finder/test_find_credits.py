"""Offline tests: every API is replaced by canned responses shaped like the real ones.

Run with:  python3 -m unittest -v
"""
import csv
import http.server
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import find_credits as fc

STEFAN_DISCOGS = {"id": 2707450, "name": "Stefan Litrownik", "anv": "", "role": "", "tracks": ""}


def discogs_track(position, title, extraartists=None):
    return {"position": position, "type_": "track", "title": title, "extraartists": extraartists or []}


ROUTES = [
    # --- MusicBrainz -------------------------------------------------------
    (r"musicbrainz\.org/ws/2/artist\?query=", {"artists": [
        {"id": "mb-stefan", "name": "Stefan Litrownik", "aliases": [{"name": "Stefan Lit"}]},
        {"id": "mb-other", "name": "Stefan Lithander"},
    ]}),
    (r"musicbrainz\.org/ws/2/artist/mb-stefan\?", {"id": "mb-stefan", "name": "Stefan Litrownik", "relations": [
        {"type": "instrument", "target-type": "recording", "attributes": ["drums (drum set)"],
         "recording": {"id": "rec-bse", "title": "Best Song Ever"}},
        {"type": "producer", "target-type": "release", "attributes": ["co"],
         "release": {"id": "rel-ep", "title": "Little EP"}},
        {"type": "composer", "target-type": "work", "attributes": [],
         "work": {"id": "work-1", "title": "Written Song"}},
    ]}),
    (r"musicbrainz\.org/ws/2/recording/rec-bse\?", {
        "id": "rec-bse", "title": "Best Song Ever", "first-release-date": "2013-07-22",
        "artist-credit": [{"name": "One Direction"}], "isrcs": ["GBHMU1300171"],
        "releases": [{"title": "Midnight Memories", "date": "2013-11-25"},
                     {"title": "Best Song Ever", "date": "2013-07-22"}]}),
    (r"musicbrainz\.org/ws/2/work/work-1\?", {"relations": [
        {"type": "performance", "target-type": "recording", "recording": {"id": "rec-written"}}]}),
    (r"musicbrainz\.org/ws/2/recording/rec-written\?", {
        "id": "rec-written", "title": "Written Song", "first-release-date": "2019",
        "artist-credit": [{"name": "Someone Else"}], "isrcs": []}),
    (r"musicbrainz\.org/ws/2/release/rel-ep\?", {
        "title": "Little EP", "date": "2016-05-01", "artist-credit": [{"name": "Indie Band"}],
        "media": [{"tracks": [
            {"title": "EP One", "recording": {"id": "r1", "isrcs": ["USXXX1600001"]}},
            {"title": "EP Two", "recording": {"id": "r2", "isrcs": []}},
        ]}]}),
    (r"musicbrainz\.org/ws/2/recording\?artist=mb-stefan", {"recording-count": 1, "recordings": [
        {"id": "rec-own", "title": "Back in the Game", "first-release-date": "2015-06-01",
         "artist-credit": [{"name": "Stefan Litrownik"}], "isrcs": []}]}),

    # --- Discogs -----------------------------------------------------------
    (r"api\.discogs\.com/artists/2707450$", {
        "id": 2707450, "name": "Stefan Litrownik", "namevariations": ["S. Litrownik"],
        "aliases": [], "groups": [{"id": 999, "name": "Street To Nowhere"}]}),
    (r"api\.discogs\.com/artists/2707450/releases", {"pagination": {"pages": 1}, "releases": [
        {"id": 5000, "type": "master", "main_release": 111, "title": "Midnight Memories",
         "artist": "One Direction", "role": "TrackAppearance", "year": 2013},
        {"id": 222, "type": "release", "title": "Magazines Or Novels", "artist": "Andy Grammer",
         "role": "Producer", "year": 2014},
        {"id": 333, "type": "release", "title": "Collide", "artist": "Boyz II Men",
         "role": "Percussion", "year": 2014, "trackinfo": "Better Half"},
    ]}),
    (r"api\.discogs\.com/releases/111$", {
        "title": "Midnight Memories", "year": 2013, "uri": "https://www.discogs.com/release/111",
        "artists": [{"name": "One Direction", "id": 1}], "extraartists": [],
        "tracklist": [discogs_track("1", "Best Song Ever", [dict(STEFAN_DISCOGS, role="Drums")]),
                      discogs_track("2", "Story Of My Life")]}),
    (r"api\.discogs\.com/releases/222$", {
        "title": "Magazines Or Novels", "year": 2014,
        "artists": [{"name": "Andy Grammer", "id": 2}],
        "extraartists": [{"name": "Stefan Litrownik", "anv": "Stefan Lit", "id": 2707450,
                          "role": "Producer", "tracks": "2 to 3"}],
        "tracklist": [discogs_track("1", "Back Home"), discogs_track("2", "Holding Out"),
                      discogs_track("3", "Blame It On The Boy")]}),
    (r"api\.discogs\.com/releases/333$", {
        "title": "Collide", "year": 2014, "artists": [{"name": "Boyz II Men", "id": 3}],
        "extraartists": [], "tracklist": [discogs_track("1", "Better Half")]}),
    (r"api\.discogs\.com/artists/999/releases", {"pagination": {"pages": 1}, "releases": [
        {"id": 444, "type": "release", "title": "Street To Nowhere", "artist": "Street To Nowhere",
         "role": "Main", "year": 2006},
        {"id": 445, "type": "release", "title": "Some Comp", "artist": "Various", "role": "Appearance"},
    ]}),
    (r"api\.discogs\.com/releases/444$", {
        "title": "Street To Nowhere", "year": 2006, "artists": [{"name": "Street To Nowhere", "id": 999}],
        "extraartists": [], "tracklist": [discogs_track("1", "Band Song A"), discogs_track("2", "Band Song B")]}),

    # --- Genius ------------------------------------------------------------
    (r"api\.genius\.com/search\?q=Stefan", {"response": {"hits": []}}),
    (r"api\.genius\.com/search\?q=Best\+Song\+Ever\+One\+Direction", {"response": {"hits": [
        {"result": {"id": 55, "title": "Best Song Ever", "primary_artist": {"id": 1, "name": "One Direction"}}}]}}),
    (r"api\.genius\.com/search", {"response": {"hits": []}}),
    (r"api\.genius\.com/songs/55\?", {"response": {"song": {
        "id": 55, "title": "Best Song Ever", "url": "https://genius.com/One-direction-best-song-ever-lyrics",
        "primary_artist": {"id": 1, "name": "One Direction"}, "release_date": "2013-07-22",
        "custom_performances": [{"label": "Drums", "artists": [{"id": 77, "name": "Stefan Litrownik"}]}]}}}),
    (r"api\.genius\.com/artists/77/songs", {"response": {"songs": [{"id": 55}, {"id": 56}], "next_page": None}}),
    (r"api\.genius\.com/songs/56\?", {"response": {"song": {
        "id": 56, "title": "Good in Goodbye (Ft. Someone)", "url": "https://genius.com/good-in-goodbye",
        "primary_artist": {"id": 5, "name": "Frank Walker"}, "featured_artists": [{"id": 6, "name": "Trivecta"}],
        "release_date": "2023-02-10", "album": None,
        "producer_artists": [{"id": 77, "name": "Stefan Litrownik"}],
        "writer_artists": [{"id": 77, "name": "Stefan Litrownik"}],
        "media": [{"provider": "spotify", "type": "audio",
                   "url": "https://open.spotify.com/track/0GoodInGoodbye00000000"}]}}}),

    # --- Spotify -----------------------------------------------------------
    (r"api\.spotify\.com/v1/search\?q=Stefan\+Litrownik&type=artist", {"artists": {"items": [
        {"id": "spArtStefan0000000000", "name": "Stefan Litrownik"}, {"id": "x", "name": "Stefan Lintner"}]}}),
    (r"api\.spotify\.com/v1/search\?.*type=artist", {"artists": {"items": []}}),
    (r"api\.spotify\.com/v1/artists/spArtStefan0000000000/albums", {"items": [
        {"id": "albOwn", "name": "Back in the Game", "release_date": "2015-06-01"}], "next": None}),
    (r"api\.spotify\.com/v1/albums/albOwn/tracks", {"items": [
        {"id": "0BackInTheGame00000000", "name": "Back in the Game",
         "artists": [{"id": "spArtStefan0000000000", "name": "Stefan Litrownik"}]}], "next": None}),
    (r"api\.spotify\.com/v1/search\?q=isrc%3AGBHMU1300171", {"tracks": {"items": [
        {"id": "5BestSongEver000000000", "name": "Best Song Ever", "artists": [{"name": "One Direction"}],
         "album": {"name": "Midnight Memories", "release_date": "2013-11-25"},
         "external_ids": {"isrc": "GBHMU1300171"}}]}}),
    (r"api\.spotify\.com/v1/search\?q=isrc", {"tracks": {"items": []}}),
    (r"api\.spotify\.com/v1/search\?q=Holding\+Out\+Andy\+Grammer", {"tracks": {"items": [
        {"id": "wrongArtist", "name": "Holding Out", "artists": [{"name": "Somebody Else"}]},
        {"id": "0HoldingOut00000000000", "name": "Holding Out", "artists": [{"name": "Andy Grammer"}],
         "album": {"name": "Magazines Or Novels", "release_date": "2014-08-12"}}]}}),
    (r"api\.spotify\.com/v1/search\?.*type=album", {"albums": {"items": []}}),
    (r"api\.spotify\.com/v1/search\?", {"tracks": {"items": []}}),
]


class FakeHttp:
    def __init__(self, routes=ROUTES):
        self.routes = [(re.compile(pattern), response) for pattern, response in routes]
        self.calls = []

    def get_json(self, url, headers=None, min_interval=0.0):
        self.calls.append(url)
        for pattern, response in self.routes:
            if pattern.search(url):
                return json.loads(json.dumps(response))
        return None  # like a 404

    def post_form(self, url, form, headers=None):
        assert url == fc.Spotify.TOKEN_URL and headers["Authorization"].startswith("Basic ")
        return {"access_token": "token", "expires_in": 3600}


class NameMatcherTest(unittest.TestCase):
    def test_matches_known_names_and_middle_names(self):
        m = fc.NameMatcher(["Stefan Litrownik", "Stefan Lit"])
        for name in ["Stefan Litrownik", "STEFAN LITROWNIK", "Stefan A. Litrownik", "Stefan Adam Litrownik",
                     "Stefan Lit", "Stefan Lit (2)", "Stéfan Litrownik"]:
            self.assertTrue(m.matches(name), name)
        for name in ["Stefan Little", "Alan Litrownik", "Stefan Litrownikson", "Stefan", "", "Lit"]:
            self.assertFalse(m.matches(name), name)


class HelpersTest(unittest.TestCase):
    def test_select_tracks(self):
        tracks = [{"position": p} for p in ["A1", "A2", "A3", "B1", "B2"]]
        pick = lambda spec: [t["position"] for t in fc.select_tracks(spec, tracks)]
        self.assertEqual(pick(""), ["A1", "A2", "A3", "B1", "B2"])
        self.assertEqual(pick("A2 to B1"), ["A2", "A3", "B1"])
        self.assertEqual(pick("A1, B2"), ["A1", "B2"])
        self.assertEqual(pick("a1 to a2, A2"), ["A1", "A2"])
        self.assertIsNone(fc.select_tracks("C9", tracks))

    def test_mb_role(self):
        self.assertEqual(fc.mb_role({"type": "instrument", "attributes": ["drums (drum set)"]}), "drums")
        self.assertEqual(fc.mb_role({"type": "producer", "attributes": ["co"]}), "co-producer")
        self.assertEqual(fc.mb_role({"type": "producer", "attributes": ["executive"]}), "executive producer")
        self.assertEqual(fc.mb_role({"type": "mix", "attributes": []}), "mixing engineer")
        self.assertEqual(fc.mb_role({"type": "vocal", "attributes": ["background vocals"]}), "background vocals")
        self.assertEqual(fc.mb_role({"type": "vocal", "attributes": []}), "vocals")

    def test_titles_and_scores(self):
        self.assertEqual(fc.core_title("Best Song Ever (feat. Somebody)"), "best song ever")
        self.assertEqual(fc.core_title("Best Song Ever - Remastered 2011"), "best song ever")
        self.assertEqual(fc.core_title("Without You (2011 Remaster)"), "without you")
        self.assertEqual(fc.core_title("Good in Goodbye (Ft. Someone)"), "good in goodbye")
        self.assertEqual(fc.match_score("Best Song Ever", ["One Direction"], "Best Song Ever", ["One Direction"]), 1.0)
        self.assertEqual(fc.match_score("Best Song Ever", ["One Direction"], "Best Song Ever", ["A Tribute Band"]), 0.0)
        self.assertEqual(fc.match_score("Good in Goodbye", ["Frank Walker & Trivecta"],
                                        "Good in Goodbye", ["Frank Walker", "Trivecta"]), 1.0)


class PipelineTest(unittest.TestCase):
    def run_pipeline(self, extra_args=(), env=None):
        out = Path(tempfile.mkdtemp())
        http = FakeHttp()
        environ = {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret",
                   "GENIUS_ACCESS_TOKEN": "g", "DISCOGS_TOKEN": ""}
        environ.update(env or {})
        with mock.patch.dict("os.environ", environ, clear=False):
            args = fc.parse_args(["--out", str(out), *extra_args])
            result = fc.run(args, http)
        return result, out, http

    def song(self, songs, title):
        found = [s for s in songs if s.title == title]
        self.assertEqual(len(found), 1, f"{title}: {[s.title for s in songs]}")
        return found[0]

    def test_full_run(self):
        result, out, http = self.run_pipeline()
        songs = result["songs"]

        # One row for Best Song Ever, with drums from all three databases merged.
        bse = self.song(songs, "Best Song Ever")
        self.assertEqual(bse.roles, ["Drums"])
        self.assertEqual(set(bse.sources), {"MusicBrainz", "Discogs", "Genius"})
        self.assertEqual(bse.spotify_id, "5BestSongEver000000000")
        self.assertEqual(bse.spotify_match, "ISRC")
        self.assertEqual(bse.album, "Best Song Ever")  # earliest release: the single

        # Discogs credit limited to tracks "2 to 3"; wrong-artist Spotify result rejected.
        holding = self.song(songs, "Holding Out")
        self.assertEqual(holding.roles, ["Producer"])
        self.assertEqual(holding.spotify_id, "0HoldingOut00000000000")
        self.assertEqual(self.song(songs, "Blame It On The Boy").roles, ["Producer"])
        self.assertFalse([s for s in songs if s.title == "Back Home"])

        # Listing fallback (trackinfo), band releases, MusicBrainz release + work credits.
        self.assertEqual(self.song(songs, "Better Half").roles, ["Percussion"])
        self.assertEqual(self.song(songs, "Band Song A").roles, ["Band member (Street To Nowhere)"])
        self.assertEqual(self.song(songs, "EP One").roles, ["Co-producer (album credit)"])
        self.assertEqual(self.song(songs, "Written Song").roles, ["Composer"])

        # Genius song with a Spotify link and his own Spotify profile track.
        goodbye = self.song(songs, "Good in Goodbye (Ft. Someone)")
        self.assertEqual(goodbye.roles, ["Producer", "Songwriter"])
        self.assertEqual(goodbye.spotify_id, "0GoodInGoodbye00000000")
        own = self.song(songs, "Back in the Game")
        self.assertEqual(own.spotify_id, "0BackInTheGame00000000")
        self.assertEqual(set(own.sources), {"MusicBrainz", "Spotify"})
        self.assertEqual([p["name"] for p in result["profiles"]], ["Stefan Litrownik"])

        # Not a match: only the exact-name MusicBrainz artist was looked up.
        self.assertFalse([u for u in http.calls if "mb-other" in u])
        # Discogs compilation listed as "Appearance" for the band isn't counted.
        self.assertFalse([u for u in http.calls if "releases/445" in u])

        # Output files.
        with (out / "credits.csv").open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), len(songs))
        self.assertIn("https://open.spotify.com/track/5BestSongEver000000000",
                      (out / "spotify_links.txt").read_text().splitlines())
        page = (out / "credits.html").read_text()
        self.assertIn("Songs Stefan Litrownik worked on", page)
        self.assertIn("Best Song Ever", page)
        self.assertEqual(rows[0]["year"], "2023")  # newest first

    def test_runs_without_any_keys(self):
        result, out, http = self.run_pipeline(env={"SPOTIFY_CLIENT_ID": "", "GENIUS_ACCESS_TOKEN": ""})
        self.assertTrue(result["songs"])
        self.assertFalse([u for u in http.calls if "spotify.com" in u or "genius.com" in u])
        self.assertEqual((out / "spotify_links.txt").read_text(), "")

    def test_spotify_refusal_still_writes_report(self):
        http = FakeHttp()
        original = http.get_json

        def get_json(url, headers=None, min_interval=0.0):
            if "api.spotify.com" in url:
                raise fc.urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
            return original(url, headers, min_interval)

        http.get_json = get_json
        out = Path(tempfile.mkdtemp())
        with mock.patch.dict("os.environ", {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "s",
                                            "GENIUS_ACCESS_TOKEN": ""}):
            result = fc.run(fc.parse_args(["--out", str(out)]), http)
        self.assertTrue(result["songs"])
        self.assertTrue((out / "credits.html").exists())

    def test_other_person(self):
        args = fc.parse_args(["--name", "Jane Doe"])
        self.assertEqual(args.alias, [])
        self.assertEqual(args.discogs_id, [])
        self.assertTrue(str(args.out).endswith("output/jane-doe"))


class HttpTest(unittest.TestCase):
    def test_retry_cache_and_404(self):
        hits = {"/flaky": 0, "/missing": 0}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits[self.path] += 1
                if self.path == "/flaky" and hits[self.path] == 1:
                    self.send_response(503)
                    self.send_header("Retry-After", "0")
                    self.end_headers()
                    return
                self.send_response(200 if self.path == "/flaky" else 404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True}).encode())

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            client = fc.Http("test", cache_dir=Path(tempfile.mkdtemp()))
            self.assertEqual(client.get_json(base + "/flaky"), {"ok": True})
            self.assertEqual(client.get_json(base + "/flaky"), {"ok": True})  # from cache
            self.assertEqual(hits["/flaky"], 2)
            self.assertIsNone(client.get_json(base + "/missing"))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
