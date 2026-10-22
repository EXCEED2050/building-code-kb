"""Update the repository's attachment manifest and recovery docs after upload."""
import base64
import json
import os
from pathlib import Path
import re
from urllib.parse import quote
from private_complete import api

ROOT = Path(__file__).resolve().parents[2]
FOLDER = ROOT / '协作交付/full-20261022'
BASE = 'repos/EXCEED2050/building-code-kb'

def request(endpoint, payload):
    path = FOLDER / 'git-request.json'
    path.write_text(json.dumps(payload, ensure_ascii=False))
    return api(endpoint, '--method', 'POST', '--input', str(path))

def read_file(path, ref):
    result = api(BASE + '/contents/' + quote(path, safe='/') + '?ref=' + ref)
    return base64.b64decode(result['content']).decode('utf-8')

def main():
    result = json.loads((FOLDER / 'upload-result.json').read_text())
    assert result['status'] == 'complete'
    assert api(BASE)['private']
    ref = api(BASE + '/git/ref/heads/main')['object']['sha']
    tree = api(BASE + '/git/commits/' + ref)['tree']['sha']
    readme = read_file('README.md', ref)
    docs = read_file('docs/私有资料恢复.md', ref)
    notice = '> 附件上传状态（2026-10-22）：完整原件、MinerU 与 OCR 资料已上传到私有 Release，共 7 个独立 ZIP，约 4.10 GB、4,220 个文件。完整条文、台账、代码和文档在主分支；恢复方法见下文。'
    readme = re.sub(r'^> 附件上传状态[^\n]*', notice, readme, count=1)
    readme = readme.replace('大文件附件约 4.11 GB，单独下载不会增加每次克隆代码的体积。为便于中断后补传，OCR/原件卷拆为约 64 MiB 的传输片段，恢复工具会自动合并；详见资料恢复说明。', '大文件附件约 4.10 GB，以 7 个完整、独立 ZIP 提供；单独下载不会增加每次克隆代码的体积，无需传输片段或合并。详见资料恢复说明。')
    docs = re.sub(r'^> 附件上传状态[^\n]*', notice, docs, count=1, flags=re.MULTILINE)
    docs = docs.replace('总下载量约 4.11 GB', '总下载量约 4.10 GB')
    docs = re.sub(r'这些 ZIP 是独立资料卷[^\n]*', '这些 ZIP 是完整、独立的资料卷，无需传输片段或 upload-parts.json。要恢复当前全部处理资料，请下载全部 7 个 ZIP，以及 asset-manifest.json；不要把不同 ZIP 按字节拼接。', docs, count=1)
    changed = {'README.md': readme, 'docs/私有资料恢复.md': docs,
               'kb/data/asset-manifest.json': (FOLDER / 'asset-manifest.json').read_text()}
    entries = [{'path': path, 'mode': '100644', 'type': 'blob', 'content': content}
               for path, content in changed.items()]
    # The published release uses whole ZIPs, so the old transport map is obsolete.
    existing = api(BASE + '/git/trees/' + tree + '?recursive=1')['tree']
    if any(row['path'] == 'kb/data/upload-parts.json' for row in existing):
        entries.append({'path': 'kb/data/upload-parts.json', 'mode': '100644', 'type': 'blob', 'sha': None})
    new_tree = request(BASE + '/git/trees', {'base_tree': tree, 'tree': entries})['sha']
    commit = ref
    if new_tree != tree:
        commit = request(BASE + '/git/commits', {'message': 'Complete private original and OCR archive delivery',
                                              'tree': new_tree, 'parents': [ref]})['sha']
        payload = FOLDER / 'git-ref-request.json'
        payload.write_text(json.dumps({'sha': commit, 'force': False}))
        api(BASE + '/git/refs/heads/main', '--method', 'PATCH', '--input', str(payload))
    # Keep the newly published snapshot's source ZIP aligned with its final manifest/docs.
    tag_payload = FOLDER / 'tag-ref-request.json'
    tag_payload.write_text(json.dumps({'sha': commit, 'force': False}))
    api(BASE + '/git/refs/tags/data-2026-10-20', '--method', 'PATCH', '--input', str(tag_payload))
    for path, content in changed.items():
        (ROOT / path).write_text(content)
    (ROOT / 'kb/data/upload-parts.json').unlink(missing_ok=True)
    result['commit'] = commit
    (FOLDER / 'upload-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False), flush=True)

if __name__ == '__main__':
    main()
