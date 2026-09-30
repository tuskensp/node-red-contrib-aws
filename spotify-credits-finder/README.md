# Spotify credits finder

Finds every song on Spotify that a behind-the-scenes musician worked on, such as
a session drummer, producer, engineer or songwriter. It's set up for
**Stefan Litrownik**, who also appears in credits as *Stefan Lit*,
*Stefan A. Litrownik* and *Stefan Adam Litrownik*. For example, he played drums
on One Direction's "Best Song Ever".

## Why this needs a tool

Spotify shows credits in its apps (right-click a song, then **Show credits**),
but you can't search by them. Its public API only lists a song's main
artists, so the only way to find a drummer's songs inside Spotify is to open
songs one by one.

This script works in the other direction. It collects credits from open music
databases, then finds each song on Spotify:

| Source | What it adds | Key needed? |
| --- | --- | --- |
| [MusicBrainz](https://musicbrainz.org) | Per-song credits (drums, producer, mixing and so on) plus the song's ISRC code | No |
| [Discogs](https://www.discogs.com/artist/2707450-Stefan-Litrownik) | Credits from album liner notes, plus releases by bands he was in | Optional (makes it faster and searches by name) |
| [Genius](https://genius.com) | Producer, songwriter and musician credits on recent pop songs | Yes (free) |
| Spotify | His own artist profile(s), and the Spotify link for every song above | Yes (free, but see below) |

Songs are matched to Spotify by **ISRC** when one is known. That's the unique ID
printed on every commercial recording, so the exact version is found. Otherwise
they're matched by title and artist.

## Quick start

1. Install [Python 3](https://www.python.org/downloads/). No other packages are needed.
2. Get the keys described in the next section.
3. In this folder, copy `.env.example` to `.env` and paste the keys in.
4. Run:

   ```sh
   python3 find_credits.py
   ```

   The first run takes a few minutes because MusicBrainz and Discogs ask
   callers to go slowly. Responses are cached in `.cache/`, so later runs are
   quick. Use `--refresh` to fetch fresh data.

5. Open `output/stefan-litrownik/credits.html` in a browser.

## Getting the keys

**Spotify (needed for the Spotify links)**

1. Go to <https://developer.spotify.com/dashboard> and log in.
2. Click **Create app**. Enter any name and description. For *Redirect URI*,
   enter `http://127.0.0.1:8888/callback` (the script doesn't use it, but the
   form requires one). Tick **Web API**, then save.
3. Open the app's **Settings** and copy the **Client ID** and **Client secret**
   into `.env`.

Since February 2026, Spotify only lets these developer apps work while the
account that created them has **Spotify Premium**.

**Discogs (optional)**: <https://www.discogs.com/settings/developers>, then
**Generate new token**.

**Genius (optional, recommended)**: <https://genius.com/api-clients>. Create
an API client (any name, and any website such as `https://example.com`), then
click **Generate Access Token**.

Without any keys, the script still produces the list of credits from
MusicBrainz and Discogs, just without Spotify links.

## What you get

All results go in `output/stefan-litrownik/`:

- `credits.html` is a page you can search and sort. Each row shows the song,
  artist, his role(s), the year, a **Play** link to Spotify, and links back to
  where each credit was found.
- `credits.csv` has the same data for Excel, Numbers or Google Sheets.
- `spotify_links.txt` has one Spotify link per line.

**To turn it into a playlist:** in the Spotify desktop app, create a new
playlist. Open `spotify_links.txt`, select all and copy. Click in the
playlist's track area and paste (Ctrl+V or Cmd+V).

## Options

```text
--alias "Name"          another name he's credited as (repeatable)
--expand-albums         when a credit only says "album", list every track on that album
--no-groups             leave out releases by bands he was a member of
--spotify-artist-id ID  use this Spotify profile instead of searching by name
--discogs-id / --musicbrainz-id / --genius-id   add a profile the search misses
--market GB             match songs in another Spotify country (default US)
--name "Someone Else"   search for a different person
```

Run `python3 find_credits.py --help` for the full list.

## How accurate is it?

- **Only credited songs appear.** If no database lists his part on a song, it
  won't show up. Anyone can add missing credits to MusicBrainz or Discogs, and
  the next run will pick them up.
- **Check the "Spotify artist profiles" line** at the top of the report. The
  script includes every track on Spotify profiles whose name matches exactly.
  A different musician with the same name would slip in, so confirm the
  profile is really him.
- Roles marked **(album credit)** mean the liner notes credit him on the whole
  album rather than on a named track.
- **Band member** rows come from releases by bands he belonged to (such as
  Street To Nowhere). They're included even when the release doesn't list his
  instrument.
- A song showing **—** in the Spotify column wasn't found on Spotify. It may
  not be released there, or the title may be spelled differently.

Other places that list his credits, useful for cross-checking by hand:
[AllMusic](https://www.allmusic.com/artist/stefan-litrownik-mn0000566187/credits),
[Discogs](https://www.discogs.com/artist/2707450-Stefan-Litrownik),
[TIDAL](https://tidal.com/browse/artist/20293281) (as *Stefan Adam Litrownik*)
and [Apple Music](https://music.apple.com/us/artist/stefan-a-litrownik/1009209995)
(as *Stefan A. Litrownik*).

## Tests

```sh
python3 -m unittest -v
```

The tests run offline against sample responses shaped like each API's.
