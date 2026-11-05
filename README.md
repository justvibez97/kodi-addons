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

## plugin.video.premiumize.downloader

Download files from your premiumize.me cloud to the device (built for an Nvidia Shield with a USB
drive) at full line speed, and play them locally in Kodi's player.

- **Fast:** each file comes down over several parallel connections (8 by default, up to 16).
  Long-press any video → *Speed test with this file* measures 1/4/8/12/16 connections on your
  line and offers to keep the fastest.
- **Watch right away:** *Play now* starts playback immediately while the download keeps going.
  Parts already on the drive play from the drive. Parts not downloaded yet (including seeking
  ahead) stream from premiumize.
- **Clear progress:** a progress bar in the top corner (speed, size, time left), a *Downloads*
  screen, and a live *Download progress* window. You get a notification when a download finishes.
- **Plays like local media:** finished files play straight from the drive, with Kodi's own resume
  points and watched marks. Subtitles next to a video (and in a `Subs/` folder) are saved beside it
  under names Kodi picks up automatically.
- **Survives restarts:** unfinished downloads resume where they stopped after Kodi restarts or the
  Shield reboots. Failed downloads retry on their own. Files are written in order, so a partial
  file is always valid up to the point it reached.
- Downloads mirror your cloud folders (`Movies/Some.Movie.2024/…`). A FAT32 drive (4 GB file limit)
  and a lack of free space are both caught before a download starts.

### Install on the Shield

1. If this repository is already installed (it is if you installed Letterboxd from it):
   **Add-ons → Install from repository → justvibez97 Add-ons** (it may still be called
   *Letterboxd Repository* until it updates itself) **→ Video add-ons → Premiumize Downloader →
   Install.** If it isn't listed yet, open **Add-ons → My add-ons → Add-on repository**, long-press
   the repository, and choose **Check for updates**.
   Otherwise, install the repository first: in Kodi's File manager, add the source
   `https://raw.githubusercontent.com/justvibez97/kodi-addons/main/install.zip`. Then go to
   **Add-ons → Install from zip file**, pick that source, and choose `repo.zip`.
2. Let Kodi write to the USB drive: Shield **Settings → Apps → Kodi → Permissions → Files and
   media → Allow management of all files**.
3. Open the add-on's settings and paste your API key from <https://www.premiumize.me/account>.
   The Google TV app's keyboard on your phone can paste it.
4. Pick a video in **My cloud**. The first time, it asks where downloads go: pick the USB drive.

### Using it

| You want to… | Do this |
| --- | --- |
| Watch now, keep it afterwards | Select the video → **Play now** |
| Download for later | Select → **Download only** (or long-press → *Download*) |
| A whole season or folder | Long-press the folder → **Download whole folder** |
| See progress | Corner progress bar, **Downloads**, or **Show download progress** |
| Pause / resume / cancel | **Downloads** → select the item |
| Watch a downloaded file | Select it in **My cloud** (marked *downloaded*) or in **Downloads** |
| Free up space | Long-press a finished item → **Delete from this device** |

Settings let you skip the question when selecting a video (always play now, always download, or
always stream), and set what happens when a download finishes. They also cover connections,
downloads running at once, subtitles, and resume-on-start.

Sign-in uses the API key. Code pairing (enter a code at premiumize.me/device) is built in but needs
an OAuth client ID registered at premiumize.me/registerclient. Paste that ID in **Settings →
Account** and the *Pair with a code* button turns on.

### Tests

`tests/premiumize_downloader/` holds a fault-injecting test of the download engine
(`test_engine.py`), a headless integration test of the service against a mock premiumize API
(`test_service.py`, `mock_premiumize.py`, Kodi stand-ins in `kodistub/`), and a test-video
generator (`gen_avi.py`). The mock is only used when the `PMDL_API_BASE` environment variable is
set, which normal installs never do.
