import hashlib
import os
import re
import shutil
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
LETTERBOXD_SRC = r"C:\Users\iNWFz\AppData\Roaming\Kodi\addons\plugin.video.letterboxd"
ZIPS_DIR = os.path.join(ROOT, "zips")

REPO_ID = "repository.letterboxd"
REPO_VERSION = "1.0.0"

REPO_ADDON_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<addon id="{REPO_ID}" name="Letterboxd Repository" version="{REPO_VERSION}" provider-name="justvibez97">
    <extension point="xbmc.addon.repository" name="Letterboxd Repository">
        <dir>
            <info compressed="false">https://justvibez97.github.io/kodi-addons/addons.xml</info>
            <checksum>https://justvibez97.github.io/kodi-addons/addons.xml.md5</checksum>
            <datadir zip="true">https://justvibez97.github.io/kodi-addons/zips</datadir>
        </dir>
    </extension>
    <extension point="xbmc.addon.metadata">
        <summary lang="en_GB">Letterboxd addon repository</summary>
        <description lang="en_GB">Hosts the Letterboxd Kodi addon and pushes updates automatically.</description>
        <platform>all</platform>
        <license>MIT</license>
    </extension>
</addon>
"""

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
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, src_dir)
                arcname = f"{base_folder_name}/{rel}".replace(os.sep, "/")
                zf.write(full, arcname)

def main():
    # ---- 1. Write repository.letterboxd addon.xml to its own staging folder ----
    repo_stage = os.path.join(ROOT, REPO_ID)
    os.makedirs(repo_stage, exist_ok=True)
    with open(os.path.join(repo_stage, "addon.xml"), "w", encoding="utf-8", newline="\n") as f:
        f.write(REPO_ADDON_XML)

    # ---- 2. Read plugin.video.letterboxd's addon.xml to get its version ----
    letterboxd_addon_xml_path = os.path.join(ROOT, "plugin.video.letterboxd", "addon.xml")
    lb_content = read_addon_xml(letterboxd_addon_xml_path)
    m = re.search(r'version="([^"]+)"', lb_content)
    lb_version = m.group(1)
    lb_id = "plugin.video.letterboxd"

    # ---- 3. Build addons.xml (repo addon + plugin addon, concatenated) ----
    repo_content = read_addon_xml(os.path.join(repo_stage, "addon.xml"))
    addons_xml = "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n<addons>\n"
    addons_xml += repo_content + "\n"
    addons_xml += lb_content + "\n"
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
        shutil.rmtree(ZIPS_DIR)
    os.makedirs(ZIPS_DIR, exist_ok=True)

    # zips/plugin.video.letterboxd/plugin.video.letterboxd-<version>.zip
    lb_zip_dir = os.path.join(ZIPS_DIR, lb_id)
    os.makedirs(lb_zip_dir, exist_ok=True)
    lb_zip_path = os.path.join(lb_zip_dir, f"{lb_id}-{lb_version}.zip")
    make_zip(LETTERBOXD_SRC, lb_zip_path, lb_id)

    # zips/repository.letterboxd/repository.letterboxd-<version>.zip
    repo_zip_dir = os.path.join(ZIPS_DIR, REPO_ID)
    os.makedirs(repo_zip_dir, exist_ok=True)
    repo_zip_path = os.path.join(repo_zip_dir, f"{REPO_ID}-{REPO_VERSION}.zip")
    make_zip(repo_stage, repo_zip_path, REPO_ID)

    print(f"Built addons.xml (md5={digest})")
    print(f"Built {lb_zip_path}")
    print(f"Built {repo_zip_path}")

if __name__ == "__main__":
    main()
