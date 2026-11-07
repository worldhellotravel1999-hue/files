#!/usr/bin/env python3
"""Keep the altstore.json of a GitHub release in sync with the IPAs published in it.

For every .ipa asset in the release the script ensures the AltStore source has an
app + version entry: version, build, min iOS, download URL, byte size, sha256 and
an icon extracted from the bundle.

Identity rules (important, because re-signed IPAs often carry a rewritten
CFBundleIdentifier):
  1. an asset whose file name matches an existing downloadURL -> same app
  2. else the longest app-name prefix match on the file name -> same app
  3. else a brand new app, identified from the IPA's Info.plist

Existing metadata (developerName, description, category, icon, version numbers of
the currently published file) is never overwritten. Set DRY_RUN=1 to only print
what would change.
"""
import base64
import hashlib
import json
import os
import plistlib
import re
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.request
import urllib.parse
import zipfile
import zlib
from pathlib import Path
from reconcile import assets_for, asset_snapshot, prune_missing, cleanup_icons, owned_asset

API = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["GITHUB_REPOSITORY"]
TAG = os.environ.get("TAG", "nightly")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
SOURCE_NAME = "altstore.json"
ICON_DIR = os.environ.get("ICON_DIR", "icons")
BRANCH = os.environ.get("TARGET_BRANCH", "main")
UA = {"User-Agent": "altstore-sync", "Accept": "application/vnd.github+json"}


# ---------------------------------------------------------------- GitHub API
def api(method, path, data=None):
    url = API + path
    headers = dict(UA)
    headers["Authorization"] = "Bearer " + TOKEN
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=180) as resp:
        payload = resp.read()
    return json.loads(payload) if payload else None


def download(url, dest, bust_cache=False):
    if bust_cache:      # release assets are CDN cached; force a fresh copy
        url += ("&" if "?" in url else "?") + f"t={int(time.time() * 1000)}"
    req = urllib.request.Request(url, headers={"User-Agent": "altstore-sync"})
    with urllib.request.urlopen(req, timeout=600) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)


def upload_asset(release_id, name, path, content_type):
    """Delete an existing asset with the same name, then upload the file."""
    for a in assets_for(api, REPO, release_id):
        if a["name"] == name:
            if DRY_RUN:
                print(f"  [dry-run] would replace asset {name}")
                return {"name": name}
            api("DELETE", f"/repos/{REPO}/releases/assets/{a['id']}")
    if DRY_RUN:
        print(f"  [dry-run] would upload asset {name} ({os.path.getsize(path)} bytes)")
        return {"name": name}
    with open(path, "rb") as f:
        payload = f.read()
    headers = dict(UA)
    headers["Authorization"] = "Bearer " + TOKEN
    headers["Content-Type"] = content_type
    req = urllib.request.Request(
        f"https://uploads.github.com/repos/{REPO}/releases/{release_id}/assets?name={name}",
        data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read())


def put_repo_file(path, data, message):
    """Create/update a file on the default branch (used for icons/)."""
    url = f"/repos/{REPO}/contents/{path}"
    blob_sha = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
    current_sha = None
    try:
        cur = api("GET", f"{url}?ref={BRANCH}")
        if cur.get("sha") == blob_sha:
            return False                      # identical file already committed
        current_sha = cur.get("sha")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    if DRY_RUN:
        print(f"  [dry-run] would write {path}")
        return True
    payload = {"message": message, "branch": BRANCH,
               "content": base64.b64encode(data).decode()}
    if current_sha:
        payload["sha"] = current_sha
    api("PUT", url, payload)
    return True


# ------------------------------------------------------------- PNG / icons
def _chunks(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a png")
    i = 8
    while i < len(data):
        (length,) = struct.unpack(">I", data[i:i + 4])
        yield data[i + 4:i + 8], data[i + 8:i + 8 + length]
        i += 12 + length


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _encode_png(rgba, w, h):
    def chunk(ctype, payload):
        body = ctype + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    stride = w * 4
    raw = b"".join(b"\x00" + rgba[y * stride:(y + 1) * stride] for y in range(h))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def normalize_png(data):
    """Convert Apple CgBI ("crushed") PNGs into standard RGBA PNGs."""
    try:
        chunks = list(_chunks(data))
    except Exception:
        return data
    if not any(t == b"CgBI" for t, _ in chunks):
        return data
    ihdr, idat = None, b""
    for ctype, payload in chunks:
        if ctype == b"IHDR":
            ihdr = payload
        elif ctype == b"IDAT":
            idat += payload
    if ihdr is None:
        return data
    w, h, depth, color = struct.unpack(">IIBB", ihdr[:10])
    if depth != 8 or color != 6:
        return data
    raw = zlib.decompressobj(-15).decompress(idat)
    bpp, stride = 4, w * 4
    out = bytearray(h * stride)
    prev = bytearray(stride)
    pos = 0
    for y in range(h):
        ft = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if ft == 1:
            for x in range(bpp, stride):
                line[x] = (line[x] + line[x - bpp]) & 0xFF
        elif ft == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 0xFF
        elif ft == 3:
            for x in range(stride):
                left = line[x - bpp] if x >= bpp else 0
                line[x] = (line[x] + ((left + prev[x]) >> 1)) & 0xFF
        elif ft == 4:
            for x in range(stride):
                left = line[x - bpp] if x >= bpp else 0
                upleft = prev[x - bpp] if x >= bpp else 0
                line[x] = (line[x] + _paeth(left, prev[x], upleft)) & 0xFF
        prev = bytearray(line)  # next scanline filters use original BGRA bytes
        for x in range(0, stride, 4):
            b, g, r, a = line[x], line[x + 1], line[x + 2], line[x + 3]
            if 0 < a < 255:                      # CgBI = premultiplied BGRA
                r = min(255, (r * 255 + a // 2) // a)
                g = min(255, (g * 255 + a // 2) // a)
                b = min(255, (b * 255 + a // 2) // a)
            line[x], line[x + 1], line[x + 2] = r, g, b
        out[y * stride:(y + 1) * stride] = line
    return _encode_png(bytes(out), w, h)


def png_size(data):
    for ctype, payload in _chunks(data):
        if ctype == b"IHDR":
            return struct.unpack(">II", payload[:8])
    raise ValueError("no IHDR")


def find_icon(ipa_path, hint=""):
    """Largest square icon PNG inside Payload/*.app, converted to standard PNG."""
    best = None
    with zipfile.ZipFile(ipa_path) as z:
        for name in z.namelist():
            low = name.lower()
            if not low.endswith(".png") or name.count("/") != 2:
                continue
            base = name.rsplit("/", 1)[-1]
            if any(k in low for k in ("launch", "splash", "shadow", "settings",
                                      "notification", "spotlight", "tile")):
                continue
            hinted = ("icon" in low or "@2x" in base or "@3x" in base
                      or (hint and base.lower().startswith(hint.lower()[:6])))
            if not hinted:
                continue
            try:
                data = normalize_png(z.read(name))
                w, h = png_size(data)
            except Exception:
                continue
            if w != h or w < 60:
                continue
            if best is None or (w, len(data)) > best[0]:
                best = ((w, len(data)), data)
    return best[1] if best else None


# ------------------------------------------------------------ IPA metadata
def ipa_info(path):
    with zipfile.ZipFile(path) as z:
        apps = sorted({n.split("/")[1] for n in z.namelist()
                       if n.startswith("Payload/") and n.endswith(".app/Info.plist")})
        if not apps:
            raise RuntimeError("no Payload/*.app/Info.plist in " + path)
        main = apps[0]
        info = plistlib.loads(z.read(f"Payload/{main}/Info.plist"))
    return main, info


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------- matching
def norm(s):
    return ''.join(c for c in s.casefold() if c.isalnum())


def pick_app(apps, asset_name):
    """Return (app, existing_version) for an IPA asset, or (None, None)."""
    download_base = f'https://github.com/{REPO}/releases/download/{TAG}'
    for app in apps:
        for v in app.get("versions", []):
            if owned_asset(v.get('downloadURL', ''), download_base) == asset_name:
                return app, v
    stem = norm(asset_name[:-4] if asset_name.lower().endswith(".ipa") else asset_name)
    best = None
    for app in apps:
        n = norm(app.get("name", ""))
        if len(n) >= 3 and stem.startswith(n):
            if best is None or len(n) > len(norm(best.get("name", ""))):
                best = app
    return best, None


def version_token(filename):
    m = re.search(r"(?<![\d.])(\d+(?:\.\d+){1,3})(?![\d.])", filename)
    return m.group(1) if m else None


def same_version(a, b):
    """'1.0' and '1.0.0' are the same version; '1.1.0' and '1.0.0' are not."""
    def parts(v):
        out = []
        for p in str(v).split("."):
            out.append(int(p) if p.isdigit() else 0)
        while len(out) > 1 and out[-1] == 0:
            out.pop()
        return out
    return parts(a) == parts(b)


def main():
    global BRANCH
    BRANCH = api("GET", f"/repos/{REPO}").get("default_branch") or BRANCH
    icon_base = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{ICON_DIR}"
    release = api("GET", f"/repos/{REPO}/releases/tags/{TAG}")
    release_id = release["id"]
    assets = assets_for(api, REPO, release_id)
    if any(a.get('state') != 'uploaded' for a in assets):
        raise RuntimeError('Release has an unfinished upload; retry after it completes')
    download_base = f"https://github.com/{REPO}/releases/download/{TAG}"

    src_asset = next((a for a in assets if a["name"] == SOURCE_NAME), None)
    if src_asset:
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, SOURCE_NAME)
            download(src_asset["browser_download_url"], p, bust_cache=True)
            old_bytes = open(p, "rb").read()
        source = json.loads(old_bytes)
    else:
        old_bytes = b""
        source = {
            "name": release["name"] or TAG,
            "identifier": f"io.github.{REPO.split('/')[0].lower()}.{TAG}",
            "sourceURL": f"{download_base}/{SOURCE_NAME}",
            "website": f"https://github.com/{REPO}",
            "apps": [],
            "news": [],
        }
    source.setdefault("apps", [])
    source.setdefault("news", [])
    if not isinstance(source['apps'], list):
        raise ValueError('Source apps must be a list; refusing to overwrite it')
    for app in source['apps']:
        if not isinstance(app.get('versions', []), list):
            raise ValueError('Invalid app versions; refusing to overwrite the source')
        for version_entry in app.get('versions', []):
            if version_entry.get('minOSVersion') is not None:
                version_entry['minOSVersion'] = str(version_entry['minOSVersion'])
    # Small manifest distinguishes a replacement asset from an unchanged file.
    state_path = f'.github/altstore-state/{release_id}.json'
    state_url = f'/repos/{REPO}/contents/{urllib.parse.quote(state_path, safe="/")}?ref={BRANCH}'
    old_state = {}
    try:
        content = api('GET', state_url)
        old_state = json.loads(base64.b64decode(content['content']))
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    new_state = {'releaseID': release_id, 'assets': {}}
    for asset in assets:
        if asset['name'].lower().endswith('.ipa'):
            new_state['assets'][asset['name']] = {k: asset.get(k) for k in
                ('id', 'size', 'updated_at', 'digest')}

    ipas = [a for a in assets if a["name"].lower().endswith(".ipa")]
    for asset in sorted(ipas, key=lambda a: a["name"]):
        url = asset["browser_download_url"]
        app, current = pick_app(source['apps'], asset['name'])
        unchanged = (old_state.get('releaseID') == release_id and
                     old_state.get('assets', {}).get(asset['name']) == new_state['assets'][asset['name']])
        if current and app.get('iconURL') and '/releases/download/' not in str(app['iconURL']):
            expected = asset.get('digest') or ''
            unchanged = unchanged or (expected == 'sha256:' + str(current.get('sha256'))
                and current.get('size') == asset['size']
                and current.get('date') == asset['updated_at'])
            icon_missing = False
            if str(app['iconURL']).startswith(icon_base + '/'):
                icon_file = str(app['iconURL'])[len(icon_base) + 1:]
                try:
                    api('GET', f'/repos/{REPO}/contents/{ICON_DIR}/{urllib.parse.quote(icon_file, safe="")}?ref={BRANCH}')
                except urllib.error.HTTPError as exc:
                    if exc.code != 404: raise
                    icon_missing = True
            if unchanged and not icon_missing:
                print(f"= {asset['name']} unchanged")
                continue
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, asset["name"])
            download(url, path, bust_cache=True)
            asset_app_dir, info = ipa_info(path)
            size = os.path.getsize(path)
            digest = asset.get("digest") or ""
            sha = sha256_of(path)
            if size != asset['size'] or (digest.startswith('sha256:') and digest != 'sha256:' + sha):
                raise RuntimeError('Downloaded IPA differs from the asset snapshot; retry')

            app, current = pick_app(source["apps"], asset["name"])
            fname = asset["name"]
            fname_version = version_token(fname)
            bundle_id = info.get("CFBundleIdentifier")
            display = info.get("CFBundleDisplayName") or info.get("CFBundleName") or fname[:-4]
            if not isinstance(bundle_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', bundle_id):
                raise ValueError(f'Invalid IPA bundle identifier in {fname}')
            if app is None:
                matches = [a for a in source['apps'] if a['bundleIdentifier'] == bundle_id]
                if len(matches) > 1:
                    raise ValueError('Ambiguous bundle ID; refusing to merge distinct apps')
                app = matches[0] if matches else None

            if app is None:                       # brand new app
                app = {
                    "name": display,
                    "bundleIdentifier": bundle_id,
                    "developerName": "Travel",
                    "localizedDescription": f"{display} iOS application.",
                    "category": "utilities",
                    "versions": [],
                }
                source["apps"].append(app)
                print(f"+ app {bundle_id} ({display})")

            if current is not None:
                # same file name as before: keep its identity, unless the build
                # inside the IPA now reports a genuinely different version
                plist_version = info.get("CFBundleShortVersionString")
                if plist_version and not same_version(plist_version, str(current.get("version", ""))):
                    version = plist_version
                    build = str(info.get("CFBundleVersion") or plist_version)
                else:
                    version, build = current.get("version"), current.get("buildVersion")
            else:
                version = fname_version or info.get("CFBundleShortVersionString") or "1.0"
                build = str(info.get("CFBundleVersion") or version)
                if build in ("0", "1") and fname_version:
                    build = fname_version

            entry = {
                "version": version,
                "buildVersion": build,
                "date": asset["updated_at"].replace("+00:00", "Z"),
                "downloadURL": url,
                "size": size,
                "sha256": sha,
            }
            min_os = info.get("MinimumOSVersion") or info.get("LSMinimumSystemVersion")
            if min_os:
                entry["minOSVersion"] = str(min_os)

            versions = app.setdefault("versions", [])
            # A replaced filename serves only the current binary. Do not retain
            # old versions pointing to that same now-replaced download.
            versions[:] = [v for v in versions if not (
                owned_asset(v.get('downloadURL', ''), download_base) == fname
                and (v.get('version'), v.get('buildVersion')) != (version, build))]
            same = next((v for v in versions
                         if v.get("version") == version and v.get("buildVersion") == build), None)
            if same is None:
                versions.insert(0, entry)
                print(f"+ version {app['bundleIdentifier']} {version} ({build}) {size}B")
            elif (same.get("sha256"), same.get("size")) != (sha, size):
                same.update(entry)
                print(f"~ version {app['bundleIdentifier']} {version} ({build}) refreshed ({size}B)")
            else:
                same["date"] = entry["date"]
                same["downloadURL"] = url
            versions.sort(key=lambda v: str(v.get("date", "")), reverse=True)

            # Refresh our own icons when their IPA changes; keep custom links.
            icon_name = f"icon-{app['bundleIdentifier']}.png"
            icon_url = f"{icon_base}/{icon_name}"
            existing_icon = str(app.get('iconURL') or '')
            if not existing_icon or existing_icon.startswith(icon_base + '/') or '/releases/download/' in existing_icon:
                data = find_icon(path, info.get("CFBundleExecutable") or display)
                if data:
                    if put_repo_file(f"{ICON_DIR}/{icon_name}", data, f"icon: {icon_name}"):
                        print(f"+ icon {ICON_DIR}/{icon_name} ({len(data)} bytes)")
                    app['iconURL'] = icon_url
                elif not existing_icon:
                    raise RuntimeError(f'No extractable icon in {fname}; source left unchanged')

    # Never prune from an incomplete inventory, or from one changed mid-run.
    fresh = assets_for(api, REPO, release_id)
    if asset_snapshot(fresh) != asset_snapshot(assets):
        raise RuntimeError('Release changed during sync; retry after uploads finish')
    source, removed = prune_missing(source, fresh, download_base)
    for change in removed:
        kind = 'app' if change['removed_app'] else 'version'
        print(f"- {kind} {change['app']}: {', '.join(change['files'])}")
    new_bytes = (json.dumps(source, ensure_ascii=False, indent=2) + "\n").encode()
    if new_bytes != old_bytes:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / SOURCE_NAME)
            Path(path).write_bytes(new_bytes)
            try:
                upload_asset(release_id, SOURCE_NAME, path, 'application/json')
            except Exception:
                # Preserve the previous working source if replacement upload fails.
                if old_bytes and not DRY_RUN:
                    Path(path).write_bytes(old_bytes)
                    upload_asset(release_id, SOURCE_NAME, path, 'application/json')
                raise
            if not DRY_RUN:
                verify = str(Path(tmp) / 'verify.json')
                download(f'{download_base}/{SOURCE_NAME}', verify, bust_cache=True)
                if Path(verify).read_bytes() != new_bytes:
                    raise RuntimeError('Published source not confirmed; leaving old icons in place')
        print(f"{'[dry-run] ' if DRY_RUN else ''}updated {SOURCE_NAME}: "
              f"{len(new_bytes)} bytes, {len(source['apps'])} apps")
    else:
        print('altstore.json already up to date')
    # Save a small cache in the repository, keeping the release asset list tidy.
    if old_state != new_state:
        put_repo_file(state_path, (json.dumps(new_state, indent=2) + '\n').encode(),
                      f'ci: record {TAG} IPA asset snapshot')
    # Recheck IPA inventory before irreversible cleanup (source itself changed).
    current_assets = assets_for(api, REPO, release_id)
    current_ipas = [a for a in current_assets if a['name'].lower().endswith('.ipa')]
    if asset_snapshot(current_ipas) != asset_snapshot(ipas):
        raise RuntimeError('IPA list changed before icon cleanup; retry')
    cleanup_icons(api, download, REPO, BRANCH, ICON_DIR, release_id, source, DRY_RUN)
    return 0


if __name__ == "__main__":
    sys.exit(main())
