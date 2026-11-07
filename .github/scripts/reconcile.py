"""Release reconciliation and garbage collection for managed AltStore icons."""
import copy
import json
import re
import tempfile
import urllib.error
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit


def pages(api, path):
    result = []
    for page in range(1, 1001):
        sep = '&' if '?' in path else '?'
        batch = api('GET', path + sep + f'per_page=100&page={page}')
        if not isinstance(batch, list):
            raise RuntimeError('Expected a complete list from GitHub')
        result.extend(batch)
        if len(batch) < 100:
            return result
    raise RuntimeError('Pagination limit reached; refusing incomplete inventory')


def assets_for(api, repo, release_id):
    assets = pages(api, f'/repos/{repo}/releases/{release_id}/assets')
    if len({a['id'] for a in assets}) != len(assets):
        raise RuntimeError('Asset list changed while paginating; retry the workflow')
    return assets


def asset_snapshot(assets):
    return sorted((a['id'], a['name'], a['size'], a.get('updated_at'),
                   a.get('digest'), a.get('state')) for a in assets)


def owned_asset(url, download_base):
    """Only a URL in THIS exact repository and release tag is managed here."""
    parsed, base = urlsplit(str(url)), urlsplit(download_base)
    prefix = unquote(base.path).rstrip('/') + '/'
    path = unquote(parsed.path)
    if parsed.scheme != base.scheme or parsed.netloc != base.netloc:
        return None
    if not path.startswith(prefix):
        return None
    name = path[len(prefix):]
    return name if name and '/' not in name else None


def prune_missing(source, assets, download_base):
    """Drop deleted managed downloads, preserving external versions and metadata."""
    result = copy.deepcopy(source)
    present = {a['name'] for a in assets if a['name'].lower().endswith('.ipa')}
    removed = []
    kept_apps = []
    for app in result['apps']:
        versions, dropped = [], []
        for version in app.get('versions', []):
            filename = owned_asset(version.get('downloadURL', ''), download_base)
            if filename and filename.lower().endswith('.ipa') and filename not in present:
                dropped.append(filename)
            else:
                versions.append(version)
        if dropped:
            removed.append({'app': app['name'], 'files': sorted(set(dropped)),
                            'removed_app': not versions})
            app['versions'] = versions
        if not dropped or versions:
            kept_apps.append(app)
    result['apps'] = kept_apps
    return result, removed


def managed_icon_names(source, repo, branch, icon_dir):
    """Protect canonical icons and every in-repo icon URL mentioned in a source."""
    names = set()
    for app in source['apps']:
        bundle = str(app.get('bundleIdentifier', ''))
        if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', bundle):
            names.add(f'icon-{bundle}.png')
    prefix = f'/{repo}/{branch}/{icon_dir}/'
    def walk(obj):
        if isinstance(obj, dict):
            for value in obj.values(): walk(value)
        elif isinstance(obj, list):
            for value in obj: walk(value)
        elif isinstance(obj, str):
            u = urlsplit(obj)
            if u.netloc == 'raw.githubusercontent.com' and unquote(u.path).startswith(prefix):
                names.add(unquote(u.path)[len(prefix):])
    walk(source)
    return names


def cleanup_icons(api, download, repo, branch, icon_dir, release_id, source, dry_run=False):
    """Delete only generated icon-*.png files unreferenced by ANY release source.

    Called after the updated source is published. Failed/missing inventories never
    imply an empty source. An interrupted cleanup is retried on the next run.
    """
    protected = managed_icon_names(source, repo, branch, icon_dir)
    try:
        for release in pages(api, f'/repos/{repo}/releases'):
            if release['id'] == release_id:
                continue
            other_assets = assets_for(api, repo, release['id'])
            other_src = next((a for a in other_assets if a['name'] == 'altstore.json'), None)
            if other_src:
                with tempfile.TemporaryDirectory() as tmp:
                    path = str(Path(tmp) / 'altstore.json')
                    download(other_src['browser_download_url'], path, bust_cache=True)
                    other = json.loads(Path(path).read_text())
                if not isinstance(other.get('apps'), list):
                    raise ValueError('Another source has an invalid app list')
                protected.update(managed_icon_names(other, repo, branch, icon_dir))
            elif any(a['name'].lower().endswith('.ipa') for a in other_assets):
                print('Icon cleanup deferred: another release has IPAs but no source yet')
                return
        listing = api('GET', f'/repos/{repo}/contents/{quote(icon_dir, safe="/")}?ref={quote(branch, safe="")}')
        if not isinstance(listing, list) or len(listing) >= 1000:
            raise ValueError('Incomplete icons directory listing')
    except (urllib.error.HTTPError, OSError, ValueError, KeyError) as exc:
        if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
            print('Icon cleanup deferred: an inventory is not available')
        else:
            print(f'Icon cleanup deferred: cannot verify all references ({type(exc).__name__})')
        return
    for item in listing:
        name = item['name']
        if item['type'] != 'file' or not re.fullmatch(r'icon-[A-Za-z0-9][A-Za-z0-9._-]*\.png', name):
            continue
        if name in protected:
            continue
        if not dry_run:
            api('DELETE', f'/repos/{repo}/contents/{quote(item["path"], safe="/")}',
                {'message': f'icon: remove unused {name}', 'branch': branch, 'sha': item['sha']})
        print(f'{"[dry-run] " if dry_run else ""}- unused icon {item["path"]}')
