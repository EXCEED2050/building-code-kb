"""Publish the PDFs already stored in private archive assets as individual PDFs."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import base64
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import threading
import time
from urllib.parse import quote
import zipfile

from private_complete import api, all_assets, download, BASE

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / '.pdf-publish-work'
SOURCE = BASE + '/releases/417916593'
TAG = 'pdfs-2026-10-22'
WRITE_LOCK = threading.Lock()
LAST_WRITE = 0.0

def payload_request(endpoint, value, method='POST'):
    path = WORK / 'request.json'
    path.write_text(json.dumps(value, ensure_ascii=False))
    return api(endpoint, '--method', method, '--input', str(path))

def title_for(row):
    path = PurePosixPath(row['path'])
    if row['path'].startswith('MinerU/') and '.pdf-' in path.parent.name:
        return path.parent.name.split('.pdf-', 1)[0] + '.pdf'
    return path.name

def category(row):
    if row['path'].startswith('建筑规范/'):
        return '规范原件'
    return 'MinerU 保留原件' if row['path'].startswith('MinerU/') else 'OCR 合并稿'

def markdown(value):
    return value.replace('\\', '\\\\').replace('[', '\\[').replace(']', '\\]').replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')

def main():
    assert os.environ['GITHUB_REPOSITORY'].lower() == 'exceed2050/building-code-kb'
    assert api(BASE)['private']
    WORK.mkdir(exist_ok=True)
    release_path = BASE + '/releases/' + str(int(os.environ['PDF_RELEASE_ID']))
    destination = api(release_path)
    assert destination['tag_name'] == TAG
    upload_url = destination['upload_url'].split('{')[0]
    manifest = json.loads((ROOT / 'kb/data/asset-manifest.json').read_text())
    pdfs = [r for r in manifest['files'] if r['path'].lower().endswith('.pdf')]
    pdfs.extend(json.loads((ROOT / '.github/data/nested-pdfs.json').read_text()))
    unique = {}
    for row in pdfs:
        if row['sha256'] not in unique:
            number = len(unique) + 1
            title = title_for(row)
            stem = re.sub(r'[^A-Za-z0-9._-]+', '_', title[:-4]).strip('_.-')[:90] or 'document'
            unique[row['sha256']] = {**row, 'asset_name': f'PDF-{number:03d}-{stem}.pdf', 'title': title}
    existing = {r['name']: r for r in all_assets(release_path)}
    received = {}
    for sha, row in unique.items():
        old = existing.get(row['asset_name'])
        if old and old['state'] == 'uploaded':
            assert old['size'] == row['bytes']
            if old.get('digest'):
                assert old['digest'] == 'sha256:' + sha
            received[sha] = old
    print('PDFS', len(pdfs), 'sources', len(unique), 'unique', len(received), 'already available', flush=True)

    def upload(item):
        global LAST_WRITE
        sha, row, path = item
        for attempt in range(4):
            old = existing.get(row['asset_name'])
            if old and old['state'] != 'uploaded':
                api(BASE + '/releases/assets/' + str(old['id']), '--method', 'DELETE')
                existing.pop(row['asset_name'], None)
            with WRITE_LOCK:
                time.sleep(max(0, 1.0 - (time.monotonic() - LAST_WRITE)))
                LAST_WRITE = time.monotonic()
            try:
                result = api(upload_url + '?name=' + quote(row['asset_name']) + '&label=' + quote(row['title']),
                    '--method', 'POST', '--header', 'Content-Type: application/pdf', '--input', str(path), timeout=600)
                assert result['name'] == row['asset_name'] and result['state'] == 'uploaded'
                assert result['size'] == row['bytes']
                if result.get('digest'):
                    assert result['digest'] == 'sha256:' + sha
                return sha, result
            except RuntimeError:
                current = {a['name']: a for a in all_assets(release_path)}
                old = current.get(row['asset_name'])
                if old and old['state'] == 'uploaded' and old['size'] == row['bytes']:
                    return sha, old
                if old:
                    existing[row['asset_name']] = old
                if attempt == 3:
                    raise
                time.sleep(5)

    source_assets = {r['name']: r for r in all_assets(SOURCE) if r['state'] == 'uploaded'}
    for asset in manifest['assets']:
        remaining = {r['path']: (sha, r) for sha, r in unique.items() if sha not in received}
        if not remaining:
            break
        archive = WORK / asset['file']
        download(source_assets[asset['file']], archive)
        extracted = []
        with zipfile.ZipFile(archive) as source:
            containers = {}
            for sha, row in remaining.values():
                if row.get('container'):
                    containers.setdefault(row['container'], []).append((sha, row))
            def extract_pdf(reader, member, sha, row):
                assert reader.getinfo(member).file_size == row['bytes']
                path = WORK / row['asset_name']
                with reader.open(member) as src, path.open('wb') as dst:
                    for block in iter(lambda: src.read(4 * 1024 * 1024), b''):
                        dst.write(block)
                extracted.append((sha, row, path))
            for name in source.namelist():
                if name in remaining:
                    sha, row = remaining[name]
                    extract_pdf(source, name, sha, row)
                if name in containers:
                    with zipfile.ZipFile(io.BytesIO(source.read(name))) as nested:
                        for sha, row in containers[name]:
                            extract_pdf(nested, row['member'], sha, row)
        with ThreadPoolExecutor(max_workers=2) as executor:
            tasks = {executor.submit(upload, item): item[2] for item in extracted}
            for task in as_completed(tasks):
                sha, result = task.result()
                received[sha] = result
                tasks[task].unlink()
                if len(received) % 10 == 0 or len(received) == len(unique):
                    print('PUBLISHED PDF', len(received), '/', len(unique), flush=True)
        archive.unlink()
    assert len(received) == len(unique)
    catalog = []
    for row in pdfs:
        result = received[row['sha256']]
        catalog.append({**row, 'title': title_for(row), 'category': category(row),
                        'asset_name': result['name'],
                        'url': 'https://github.com/EXCEED2050/building-code-kb/releases/download/' + TAG + '/' + quote(result['name'])})
    lines = ['# PDF 目录', '', '点击文件名即可单独下载正常 PDF，无需解压。仓库和文件均保持私有。', '',
             f'共 {len(pdfs)} 个 PDF 来源条目；相同内容共用下载文件，共 {len(unique)} 个独立 PDF。包含不同版本、单页图纸、解析原件和 OCR 合并稿，数量与已切条的 159 本标准不同。', '',
             '[全部 PDF 附件](https://github.com/EXCEED2050/building-code-kb/releases/tag/' + TAG + ') · [ZIP 完整备份与目录恢复](私有资料恢复.md)', '']
    for group in ['规范原件', 'MinerU 保留原件', 'OCR 合并稿']:
        records = [r for r in catalog if r['category'] == group]
        lines.extend([f'## {group}（{len(records)} 份）', '', '| PDF | 大小 |', '| --- | ---: |'])
        for row in records:
            lines.append(f"| [{markdown(row['title'])}]({row['url']}) | {row['bytes'] / 1_000_000:.2f} MB |")
        lines.append('')
    index_text = '\n'.join(lines)
    for attempt in range(3):
        head = api(BASE + '/git/ref/heads/main')['object']['sha']
        tree = api(BASE + '/git/commits/' + head)['tree']['sha']
        data = api(BASE + '/contents/README.md?ref=' + head)
        readme = base64.b64decode(data['content']).decode('utf-8')
        entry = '**逐份 PDF 下载：[打开 PDF 目录](docs/PDF目录.md)**，无需解压。ZIP 完整备份仍保留。\n\n'
        if 'docs/PDF目录.md' not in readme:
            marker = '规范 PDF/Word 原件'
            position = readme.find(marker)
            readme = readme[:position] + entry + readme[position:] if position >= 0 else entry + readme
        contents = {'docs/PDF目录.md': index_text,
                    'docs/pdf-catalog.json': json.dumps({'pdf_count': len(unique), 'source_count': len(pdfs), 'files': catalog}, ensure_ascii=False, indent=2) + '\n',
                    'README.md': readme}
        new_tree = payload_request(BASE + '/git/trees', {'base_tree': tree,
            'tree': [{'path': path, 'mode': '100644', 'type': 'blob', 'content': content} for path, content in contents.items()]})['sha']
        commit = head if new_tree == tree else payload_request(BASE + '/git/commits',
            {'message': 'Add direct PDF downloads and source catalog', 'tree': new_tree, 'parents': [head]})['sha']
        try:
            if commit != head:
                payload_request(BASE + '/git/refs/heads/main', {'sha': commit, 'force': False}, 'PATCH')
            break
        except RuntimeError:
            if attempt == 2:
                raise
    payload_request(release_path, {'draft': False, 'target_commitish': commit,
        'name': 'PDF 原件 · 逐份下载',
        'body': f'{len(unique)} 个独立 PDF，覆盖 {len(pdfs)} 个来源条目，可单独下载，无需解压。\n\n[打开可检索的 PDF 目录](https://github.com/EXCEED2050/building-code-kb/blob/main/docs/PDF%E7%9B%AE%E5%BD%95.md)\n\n原 ZIP 完整备份保留在资料快照 Release。仓库仍为私有。'}, 'PATCH')
    print('COMPLETE', json.dumps({'pdf_count': len(unique), 'source_count': len(pdfs), 'commit': commit,
        'catalog_url': 'https://github.com/EXCEED2050/building-code-kb/blob/main/docs/PDF%E7%9B%AE%E5%BD%95.md'}, ensure_ascii=False), flush=True)

if __name__ == '__main__':
    main()
