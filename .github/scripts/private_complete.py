"""Run on GitHub Actions to assemble private transport assets into full ZIPs."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
FOLDER = ROOT / '协作交付/full-20261022'
BASE = 'repos/EXCEED2050/building-code-kb'
RELEASE = BASE + '/releases/417916593'

def api(endpoint, *args, timeout=180):
    for attempt in range(4):
        r = subprocess.run(['gh', 'api', endpoint, *args], capture_output=True, text=True, timeout=timeout)
        if not r.returncode:
            return json.loads(r.stdout) if r.stdout.strip() else None
        if 'DELETE' in args and 'HTTP 404' in r.stderr:
            return None
        if attempt == 3:
            raise RuntimeError(r.stderr[-1000:])
        time.sleep(3)

def all_assets(release):
    result = []
    page = 1
    while True:
        rows = api(release + '/assets?per_page=100&page=' + str(page))
        result.extend(rows)
        if len(rows) < 100:
            return result
        page += 1

def download(row, destination):
    for attempt in range(4):
        with destination.open('wb') as stream:
            r = subprocess.run(['gh', 'api', BASE + '/releases/assets/' + str(row['id']),
                                '--header', 'Accept: application/octet-stream'],
                               stdout=stream, stderr=subprocess.PIPE, timeout=300)
        if r.returncode == 0 and destination.stat().st_size == row['size']:
            return
        if attempt == 3:
            raise RuntimeError('Download failed: ' + row['name'])
        time.sleep(3)

def main():
    assert os.environ['GITHUB_REPOSITORY'].lower() == 'exceed2050/building-code-kb'
    assert api(BASE)['private']
    transport = BASE + '/releases/' + str(int(os.environ['TRANSPORT_RELEASE_ID']))
    FOLDER.mkdir(parents=True, exist_ok=True)
    rows = {r['name']: r for r in all_assets(transport) if r['state'] == 'uploaded'}
    plan_path = FOLDER / 'transfer-plan.json'
    download(rows['transfer-plan.json'], plan_path)
    plan = json.loads(plan_path.read_text())
    manifest = plan['manifest']
    release = api(RELEASE)
    upload_url = release['upload_url'].split('{')[0]

    def upload(path, replace=False):
        existing = next((r for r in all_assets(RELEASE) if r['name'] == path.name), None)
        if existing and existing['state'] == 'uploaded' and not replace:
            assert existing['size'] == path.stat().st_size
            return
        if existing:
            api(BASE + '/releases/assets/' + str(existing['id']), '--method', 'DELETE')
        result = api(upload_url + '?name=' + quote(path.name), '--method', 'POST',
                     '--header', 'Content-Type: application/octet-stream', '--input', str(path), timeout=1200)
        assert result['state'] == 'uploaded' and result['size'] == path.stat().st_size
        print('COMPLETE ARCHIVE', path.name, flush=True)

    for archive in plan['archives']:
        assert Path(archive['file']).name == archive['file']
        existing = next((r for r in all_assets(RELEASE) if r['name'] == archive['file'] and r['state'] == 'uploaded'), None)
        if existing:
            assert existing['size'] == archive['bytes']
            continue
        parts = archive['parts']
        for part in parts:
            assert Path(part['file']).name == part['file']
            assert rows[part['file']]['size'] == part['bytes']
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda p: download(rows[p['file']], FOLDER / p['file']), parts))
        path = FOLDER / archive['file']
        with path.open('wb') as destination:
            for part in parts:
                source_path = FOLDER / part['file']
                with source_path.open('rb') as source:
                    for block in iter(lambda: source.read(4 * 1024 * 1024), b''):
                        destination.write(block)
                source_path.unlink()
        assert path.stat().st_size == archive['bytes']
        upload(path)
        path.unlink()
    (FOLDER / 'asset-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    (FOLDER / 'SHA256SUMS.txt').write_text(''.join(a['sha256'] + '  ' + a['file'] + '\n' for a in manifest['assets']))
    (FOLDER / 'README-downloads.txt').write_text(plan['readme'])
    for name in ['asset-manifest.json', 'SHA256SUMS.txt', 'README-downloads.txt']:
        upload(FOLDER / name, replace=True)
    expected = {a['file']: a['bytes'] for a in manifest['assets']}
    for name in ['asset-manifest.json', 'SHA256SUMS.txt', 'README-downloads.txt']:
        expected[name] = (FOLDER / name).stat().st_size
    current = all_assets(RELEASE)
    received = {a['name']: a for a in current if a['state'] == 'uploaded'}
    assert all(name in received and received[name]['size'] == size for name, size in expected.items())
    for row in current:
        if row['name'] not in expected:
            api(BASE + '/releases/assets/' + str(row['id']), '--method', 'DELETE')
    payload = FOLDER / 'publish-release.json'
    payload.write_text(json.dumps({'draft': False, 'tag_name': 'data-2026-10-20', 'target_commitish': 'main',
        'name': '完整协作资料快照 · 原件与 OCR',
        'body': '完整私有协作资料：7 个独立 ZIP，约 4.10 GB，包含 4,220 个原件及 OCR 文件。主分支包含应用代码、架构图、文档、159 本标准及 43,813 个条文块。\n\n下载全部 ZIP 和 asset-manifest.json 到同一目录，从 kb 目录运行 `python -m tools.restore_assets /下载目录`。\n\n仅限获授权的仓库协作者使用；条文、表格与效力状态仍需人工验收。'}, ensure_ascii=False))
    release = api(RELEASE, '--method', 'PATCH', '--input', str(payload))
    result = {'status': 'complete', 'release_url': release['html_url'], 'files': len(expected),
              'bytes': sum(expected.values()), 'source_files': len(manifest['files'])}
    (FOLDER / 'upload-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    from private_finalize import main as finalize
    finalize()
    api(transport, '--method', 'DELETE')
    print('ALL COMPLETE', json.dumps(result, ensure_ascii=False), flush=True)

if __name__ == '__main__':
    main()
