# kodi-addons

Personal Kodi addons, published as a Kodi repository (`repository.letterboxd`, shown in Kodi as
**justvibez97 Add-ons**). Once the repository is installed, Kodi updates every add-on in it
automatically.

## plugin.video.letterboxd

Browse Letterboxd watchlist, diary, and lists inside Kodi; enrich with TMDb/OMDb metadata;
hide watched films using Trakt or Letterboxd as the watch-history source.

### Install

1. Download `plugin.video.letterboxd.zip` from [Releases](../../releases) (or zip the
   `plugin.video.letterboxd/` folder yourself).
2. In Kodi: **Settings → Add-ons → Install from zip file** → select the zip.
3. Configure your Letterboxd username (and optional API keys / TMDb / OMDb keys) in the
   addon's settings.

## plugin.video.premiumize.downloader (Debrid Downloader)

Download files from your premiumize.me cloud and your Real-Debrid torrents to the device (built for
an Nvidia Shield with a USB drive) at full line speed, and play them locally in Kodi's player.
Until 1.1.0 it was called *Premiumize Downloader*; the add-on ID stays the same, so updates,
settings and downloads carry over.

- **Fast:** each file comes down over several parallel connections (8 by default, up to 16).
  Long-press any video → *Speed test with this file* measures 1/4/8/12/16 connections on your
  line and offers to keep the fastest.
- **Watch right away:** *Play now* starts playback immediately while the download keeps going.
  Parts already on the drive play from the drive. Parts not downloaded yet (including seeking
  ahead) stream from the cloud.
- **Clear progress:** a progress bar in the top corner (speed, size, time left), a *Downloads*
  screen, and a live *Download progress* window. You get a notification when a download finishes.
- **Plays like local media:** finished files play straight from the drive, with Kodi's own resume
  points and watched marks. Subtitles next to a video (and in a `Subs/` folder) are saved beside it
  under names Kodi picks up automatically.
- **Survives restarts:** unfinished downloads resume where they stopped after Kodi restarts or the
  Shield reboots. Failed downloads retry on their own. Files are written in order, so a partial
  file is always valid up to the point it reached.
- Downloads mirror your cloud folders (`Movies/Some.Movie.2024/…`); Real-Debrid files go in a folder
  named after their torrent. A FAT32 drive (4 GB file limit) and a lack of free space are both
  caught before a download starts.

### Install on the Shield

1. If this repository is already installed (it is if you installed Letterboxd from it):
   **Add-ons → Install from repository → justvibez97 Add-ons** (it may still be called
   *Letterboxd Repository* until it updates itself) **→ Video add-ons → Debrid Downloader →
   Install.** If it isn't listed yet, open **Add-ons → My add-ons → Add-on repository**, long-press
   the repository, and choose **Check for updates**.
   Otherwise, install the repository first: in Kodi's File manager, add the source
   `https://raw.githubusercontent.com/justvibez97/kodi-addons/main/install.zip`. Then go to
   **Add-ons → Install from zip file**, pick that source, and choose `repo.zip`.
2. Let Kodi write to the USB drive: Shield **Settings → Apps → Kodi → Permissions → Files and
   media → Allow management of all files**.
3. Sign in to one or both services:
   - **premiumize.me:** open the add-on's settings and paste your API key from
     <https://www.premiumize.me/account>. The Google TV app's keyboard on your phone can paste it.
   - **Real-Debrid:** select **Add Real-Debrid** in the add-on's main menu, then enter the code it
     shows at <https://real-debrid.com/device> on your phone. (A private API token from
     real-debrid.com/apitoken also works: **Settings → Real-Debrid**, advanced level.)
4. Pick a video in **premiumize.me cloud** or **Real-Debrid torrents**. The first time, it asks where downloads go: pick the USB drive
   (listed as *USB drive <id>* with its free space and size). To change it later, use
   **Settings → Downloads → Choose download folder...**. Kodi's own folder browser often doesn't
   list USB drives on the Shield, and its "External storage" is the Shield's internal storage.
   The drive must be set up as **removable storage**. If the Shield's Storage settings list it under
   *Device storage*, Android keeps it private to the system and no app can save files to it: select
   it there and format it as removable storage (this erases it).

### Using it

| You want to… | Do this |
| --- | --- |
| Watch now, keep it afterwards | Select the video → **Play now** |
| Download for later | Select → **Download only** (or long-press → *Download*) |
| A whole season or folder | Long-press the folder → **Download whole folder** |
| See progress | Corner progress bar, **Downloads**, or **Show download progress** |
| Pause / resume / cancel | **Downloads** → select the item |
| Watch a downloaded file | Select it in your cloud (marked *downloaded*) or in **Downloads** |
| A whole Real-Debrid torrent | Long-press the torrent → **Download whole torrent** |
| Delete a downloaded video | Long-press → **Delete from device and cloud**: it goes from both |
| Delete a cloud folder or torrent | Long-press → **Delete from premiumize cloud** / **Delete torrent from Real-Debrid**: copies downloaded from it go too |

Settings let you skip the question when selecting a video (always play now, always download, or
always stream), and set what happens when a download finishes. They also cover connections,
downloads running at once, subtitles, and resume-on-start.

premiumize sign-in uses the API key. Code pairing (enter a code at premiumize.me/device) is built in
but needs an OAuth client ID registered at premiumize.me/registerclient. Paste that ID in **Settings →
premiumize.me** and the *Pair with a code* button turns on.

Deleting always covers both places, after one confirmation that says exactly what goes. On premiumize, a
video's subtitles go with it, and its folder too once no other video is left in it (never the top level).
Real-Debrid can only delete whole torrents, so deleting one downloaded episode removes its whole torrent
from Real-Debrid; other episodes already on the device stay. Deleting files outside the add-on (with a
file manager, or on the services' websites) is not synced: a missing file could just be an unplugged drive.

Real-Debrid lists torrents newest first. A torrent with a single video plays straight from the list;
others open as folders. Torrents Real-Debrid hasn't finished yet show their state. Real-Debrid can
only delete whole torrents, not single files from one. Search looks in both services.

### Tests

`tests/premiumize_downloader/` holds a fault-injecting test of the download engine
(`test_engine.py`), a headless integration test of the service against a mock premiumize API
(`test_service.py`, `mock_premiumize.py`, Kodi stand-ins in `kodistub/`), the same for Real-Debrid
(`test_realdebrid.py`, `mock_realdebrid.py`: code sign-in, token refresh, menus, download, stream,
delete), and a test-video generator (`gen_avi.py`). The mocks are only used when the
`PMDL_API_BASE` / `PMDL_RD_BASE` environment variables are set, which normal installs never do.
