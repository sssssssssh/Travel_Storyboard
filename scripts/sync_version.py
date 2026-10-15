"""Synchronize generated frontend package metadata from the root VERSION file.

Use --check in CI or before shipping. Runtime backend/config/launchers read
VERSION directly; the frontend UI and bundle use the generated package version.
"""
import argparse
import json
from pathlib import Path
import re


def synchronize(root: Path, *, check: bool) -> bool:
    target=(root/'VERSION').read_text(encoding='utf-8').strip()
    if not re.fullmatch(r'\d+\.\d+\.\d+',target):
        raise ValueError('invalid release VERSION')
    path=root/'frontend/package.json'
    data=json.loads(path.read_text(encoding='utf-8'))
    if data.get('version')==target:
        return True
    if check:
        return False
    data['version']=target
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return True


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    if not synchronize(Path(__file__).resolve().parents[1],check=args.check):
        parser.exit(1,'frontend/package.json release version differs from VERSION; run scripts/sync_version.py\n')
