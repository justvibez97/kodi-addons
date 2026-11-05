import hashlib
import os
import re
import shutil
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
LETTERBOXD_SRC = r"C:\Users\iNWFz\AppData\Roaming\Kodi\addons\plugin.video.letterboxd"
ZIPS_DIR = os.path.join(ROOT, "zips")

# The id stays "repository.letterboxd" so Shields that already have it keep
# receiving updates; only the display name changed once it hosted more add-ons.
REPO_ID = "repository.letterboxd"
REPO_NAME = "justvibez97 Add-ons"
REPO_VERSION = "1.0.1"

# (add-on id, folder its zip is built from)
ADDONS = [
    # Letterboxd is built from the copy installed in desktop Kodi (where it is edited and tested).
    ("plugin.video.letterboxd", LETTERBOXD_SRC),
    ("plugin.video.premiumize.downloader", os.path.join(ROOT, "plugin.video.premiumize.downloader")),
]

_RAW_BASE = "https://raw.githubusercontent.com/justvibez97/kodi-addons/main"

REPO_ADDON_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<addon id="{REPO_ID}" name="{REPO_NAME}" version="{REPO_VERSION}" provider-name="justvibez97">
    <extension point="xbmc.addon.repository" name="{REPO_NAME}">
        <dir>
            <info compressed="false">{_RAW_BASE}/addons.xml</info>
            <checksum>{_RAW_BASE}/addons.xml.md5</checksum>
            <datadir zip="true">{_RAW_BASE}/zips</datadir>
        </dir>
    </extension>
    <extension point="xbmc.addon.metadata">
        <summary lang="en_GB">justvibez97's personal Kodi add-ons</summary>
        <description lang="en_GB">Hosts the Letterboxd and Premiumize Downloader add-ons and pushes their updates automatically.</description>
        <platform>all</platform>
        <license>MIT</license>
    </extension>
</addon>
"""

# Files and folders never shipped inside an add-on zip.
EXCLUDE_DIRS = {"__pycache__", ".git", ".idea", ".vscode"}
EXCLUDE_EXT = {".pyc", ".pyo"}


def read_addon_xml(path):
    with open(path, encoding="utf-8") as f:
        content = f.read()
    # strip the XML declaration line — addons.xml wraps multiple <addon> blocks
    content = re.sub(r'<\?xml[^>]*\?>\s*', '', content, count=1)
    return content.strip()


def make_zip(src_dir, dest_zip, base_folder_name):
    if os.path.exists(dest_zip):
        os.remove(dest_zip)
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(src_dir):
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
            for fn in sorted(filenames):
                if os.path.splitext(fn)[1] in EXCLUDE_EXT:
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, src_dir)
                arcname = f"{base_folder_name}/{rel}".replace(os.sep, "/")
                zf.write(full, arcname)


def main():
    # ---- 1. Write the repository add-on's addon.xml to its own staging folder ----
    repo_stage = os.path.join(ROOT, REPO_ID)
    os.makedirs(repo_stage, exist_ok=True)
    with open(os.path.join(repo_stage, "addon.xml"), "w", encoding="utf-8", newline="\n") as f:
        f.write(REPO_ADDON_XML)

    # ---- 2. Read every add-on's addon.xml (from the folder its zip is built from) ----
    addons = []
    for addon_id, src in ADDONS:
        content = read_addon_xml(os.path.join(src, "addon.xml"))
        version = re.search(r'<addon[^>]*\sversion="([^"]+)"', content).group(1)
        addons.append((addon_id, src, version, content))

    # ---- 3. Build addons.xml (repo add-on + every add-on, concatenated) ----
    addons_xml = "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n<addons>\n"
    addons_xml += read_addon_xml(os.path.join(repo_stage, "addon.xml")) + "\n"
    for _id, _src, _version, content in addons:
        addons_xml += content + "\n"
    addons_xml += "</addons>\n"

    addons_xml_path = os.path.join(ROOT, "addons.xml")
    with open(addons_xml_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(addons_xml)

    # ---- 4. Compute addons.xml.md5 ----
    with open(addons_xml_path, "rb") as f:
        digest = hashlib.md5(f.read()).hexdigest()
    with open(os.path.join(ROOT, "addons.xml.md5"), "w", encoding="utf-8", newline="\n") as f:
        f.write(digest)

    # ---- 5. Build zips/ folder ----
    if os.path.exists(ZIPS_DIR):
        # Delete the files; an empty folder that Windows briefly locks (indexer,
        # antivirus) is harmless, so folder removal errors are ignored.
        for dirpath, _dirs, files in os.walk(ZIPS_DIR):
            for fn in files:
                os.remove(os.path.join(dirpath, fn))
        shutil.rmtree(ZIPS_DIR, ignore_errors=True)
    os.makedirs(ZIPS_DIR, exist_ok=True)

    built = {}
    for addon_id, src, version, _content in addons:
        # zips/<id>/<id>-<version>.zip (this exact path/naming is what Kodi's
        # repository code downloads)
        zip_dir = os.path.join(ZIPS_DIR, addon_id)
        os.makedirs(zip_dir, exist_ok=True)
        zip_path = os.path.join(zip_dir, f"{addon_id}-{version}.zip")
        make_zip(src, zip_path, addon_id)
        built[addon_id] = zip_path
        # Kodi's repository browser loads artwork from <datadir>/<id>/<asset path>.
        for asset in ("icon.png", "fanart.jpg"):
            if os.path.exists(os.path.join(src, asset)):
                shutil.copy2(os.path.join(src, asset), os.path.join(zip_dir, asset))
        print(f"Built {zip_path}")

    # zips/repository.letterboxd/repository.letterboxd-<version>.zip
    repo_zip_dir = os.path.join(ZIPS_DIR, REPO_ID)
    os.makedirs(repo_zip_dir, exist_ok=True)
    repo_zip_path = os.path.join(repo_zip_dir, f"{REPO_ID}-{REPO_VERSION}.zip")
    make_zip(repo_stage, repo_zip_path, REPO_ID)

    # Short-path duplicate at the repo root, purely for a shorter first-time
    # "Add source" URL. Kodi's own auto-update mechanism uses the zips/ copy
    # above and never touches this one.
    root_zip_path = os.path.join(ROOT, "repo.zip")
    make_zip(repo_stage, root_zip_path, REPO_ID)

    # Wrapper zip: raw.githubusercontent.com has no folder browsing, so
    # "Add source" must point straight at a .zip URL, and Kodi's VFS then
    # shows that zip's *unpacked* contents when browsed (repository.letterboxd/
    # addon.xml) — there is no actual .zip file left to select for "Install
    # from zip file". Wrapping repo.zip inside install.zip gives the browser
    # an actual zip file entry to pick, which Kodi can then install correctly.
    install_zip_path = os.path.join(ROOT, "install.zip")
    if os.path.exists(install_zip_path):
        os.remove(install_zip_path)
    with zipfile.ZipFile(install_zip_path, "w", zipfile.ZIP_STORED) as zf:
        zf.write(root_zip_path, "repo.zip")

    # Same wrapper trick, but for the Letterboxd add-on itself — a guaranteed-fresh,
    # direct "Install from zip file" path that bypasses Kodi's repository
    # browser entirely (which can show a cached listing rather than
    # live-fetching on every browse). Always reflects whatever version is
    # currently in plugin.video.letterboxd/addon.xml.
    lb_zip_path = built["plugin.video.letterboxd"]
    update_zip_path = os.path.join(ROOT, "update.zip")
    if os.path.exists(update_zip_path):
        os.remove(update_zip_path)
    with zipfile.ZipFile(update_zip_path, "w", zipfile.ZIP_STORED) as zf:
        zf.write(lb_zip_path, os.path.basename(lb_zip_path))

    print(f"Built addons.xml (md5={digest})")
    print(f"Built {repo_zip_path}")
    print(f"Built {root_zip_path}")
    print(f"Built {install_zip_path}")
    print(f"Built {update_zip_path}")


if __name__ == "__main__":
    main()
