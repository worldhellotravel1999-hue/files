import copy
import importlib.util
import json
import os
from pathlib import Path
import struct
import unittest
from urllib.error import HTTPError
import zlib

os.environ.setdefault('GITHUB_TOKEN', 'test-placeholder')
os.environ.setdefault('GITHUB_REPOSITORY', 'owner/repo')
import reconcile as r
spec = importlib.util.spec_from_file_location('sync', Path(__file__).with_name('sync-altstore.py'))
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)
BASE = 'https://github.com/owner/repo/releases/download/nightly'
ICON = 'https://raw.githubusercontent.com/owner/repo/main/icons/'


def source(versions=None):
    return {'name':'test source', 'news':[], 'apps':[{'name':'应用 A',
        'bundleIdentifier':'com.test.a', 'developerName':'Custom',
        'iconURL':ICON+'icon-com.test.a.png', 'versions': versions or [
        {'version':'1.0', 'downloadURL':BASE+'/App-1.0.ipa'},
        {'version':'2.0', 'downloadURL':BASE+'/App-2.0.ipa'}]}]}


class ReconcileTests(unittest.TestCase):
    def test_remove_one_version_preserves_app_and_metadata(self):
        old = source()
        new, removed = r.prune_missing(old, [{'name':'App-2.0.ipa'}], BASE)
        self.assertEqual([v['version'] for v in new['apps'][0]['versions']], ['2.0'])
        self.assertEqual(new['apps'][0]['developerName'], 'Custom')
        self.assertEqual(new['apps'][0]['iconURL'], old['apps'][0]['iconURL'])
        self.assertFalse(removed[0]['removed_app'])
        self.assertEqual(len(old['apps'][0]['versions']), 2)

    def test_remove_last_app_and_all_ipa(self):
        new, removed = r.prune_missing(source(), [{'name':'altstore.json'}], BASE)
        self.assertEqual(new['apps'], [])
        self.assertEqual(new['name'], 'test source')
        self.assertTrue(removed[0]['removed_app'])

    def test_external_versions_remain(self):
        data = source([{'version':'1', 'downloadURL':BASE+'/gone.ipa'},
            {'version':'2', 'downloadURL':'https://example.com/gone.ipa'}])
        new, changes = r.prune_missing(data, [], BASE)
        self.assertEqual(len(new['apps']), 1)
        self.assertEqual(new['apps'][0]['versions'][0]['version'], '2')

    def test_encoded_names_query_and_uppercase(self):
        name = '中文 A+B.IPA'
        from urllib.parse import quote
        data = source([{'downloadURL':BASE+'/'+quote(name)+'?download=1'}])
        new, changes = r.prune_missing(data, [{'name':name}], BASE)
        self.assertEqual(new, data)
        self.assertEqual(changes, [])

    def test_other_repo_and_tag_do_not_match(self):
        self.assertIsNone(r.owned_asset(BASE.replace('nightly','stable')+'/a.ipa', BASE))
        self.assertIsNone(r.owned_asset(BASE.replace('owner/repo','other/repo')+'/a.ipa', BASE))
        self.assertIsNone(r.owned_asset('https://example.com/a.ipa', BASE))

    def test_complete_pagination(self):
        for count in (100, 101, 200, 201):
            calls = []
            def api(method, path):
                calls.append(path)
                page = int(path.rsplit('page=',1)[1])
                return [{'id':i} for i in range((page-1)*100, min(page*100, count))]
            self.assertEqual(len(r.pages(api, '/list')), count)
            self.assertEqual(len(calls), count//100+1)

    def test_failed_page_is_not_an_empty_inventory(self):
        def api(method, path):
            if 'page=1' in path: return [{'id':i} for i in range(100)]
            raise RuntimeError('network failed')
        with self.assertRaises(RuntimeError): r.pages(api, '/list')

    def test_asset_change_detection(self):
        a = {'id':1,'name':'a.ipa','size':5,'updated_at':'T','state':'uploaded'}
        b = dict(a, id=2)
        self.assertNotEqual(r.asset_snapshot([a]), r.asset_snapshot([b]))

    def test_noop(self):
        old = source()
        new, removed = r.prune_missing(old, [{'name':'App-1.0.ipa'},{'name':'App-2.0.ipa'}], BASE)
        self.assertEqual(new, old)
        self.assertEqual(removed, [])

    def cleanup(self, other_source=None, fail=False, dry=False):
        deleted = []
        def api(method, path, data=None):
            if method == 'DELETE':
                deleted.append((path, data)); return {}
            if '/releases?' in path: return [{'id':1}, {'id':2}] if other_source or fail else [{'id':1}]
            if '/releases/2/assets' in path:
                if fail: raise HTTPError(path, 403, 'forbidden', {}, None)
                return [{'id':3,'name':'altstore.json','browser_download_url':'https://example.com/source'}]
            if '/contents/icons?' in path:
                return [dict(name=n,path='icons/'+n,type='file',sha='abc') for n in
                    ('icon-com.test.a.png','icon-com.test.b.png','manual.png')]
            raise AssertionError(path)
        def download(url, dest, **kwargs):
            Path(dest).write_text(json.dumps(other_source))
        r.cleanup_icons(api, download, 'owner/repo', 'main', 'icons', 1, {'apps':[]}, dry)
        return deleted

    def test_unreferenced_icons_deleted_manual_file_preserved(self):
        deleted = self.cleanup()
        self.assertEqual(len(deleted), 2)
        self.assertFalse(any('/manual.png' in path for path,_ in deleted))

    def test_shared_release_icon_is_preserved(self):
        deleted = self.cleanup(source())
        self.assertEqual(len(deleted), 1)
        self.assertTrue(deleted[0][0].endswith('icon-com.test.b.png'))

    def test_other_source_error_blocks_cleanup(self):
        self.assertEqual(self.cleanup(fail=True), [])

    def test_dry_run_never_deletes(self):
        self.assertEqual(self.cleanup(dry=True), [])

    def test_same_source_shared_icon_reference(self):
        src = source()
        src['apps'][0]['bundleIdentifier'] = 'com.test.other'
        self.assertIn('icon-com.test.a.png', r.managed_icon_names(src,'owner/repo','main','icons'))

    def test_cgbi_up_filter_uses_unconverted_previous_row(self):
        def chunk(kind, payload):
            data = kind + payload
            return struct.pack('>I',len(payload))+data+struct.pack('>I',zlib.crc32(data)&0xffffffff)
        row = bytes([10,20,30,255,40,50,60,255])
        comp = zlib.compressobj(wbits=-15)
        raw = comp.compress(b'\0'+row+b'\2'+bytes(8))+comp.flush()
        png = b'\x89PNG\r\n\x1a\n'+chunk(b'CgBI',b'')
        png += chunk(b'IHDR',struct.pack('>IIBBBBB',2,2,8,6,0,0,0))
        png += chunk(b'IDAT',raw)+chunk(b'IEND',b'')
        result = sync.normalize_png(png)
        decoded = zlib.decompress(b''.join(p for k,p in sync._chunks(result) if k==b'IDAT'))
        rgba = bytes([30,20,10,255,60,50,40,255])
        self.assertEqual(decoded, b'\0'+rgba+b'\0'+rgba)


if __name__ == '__main__': unittest.main()
