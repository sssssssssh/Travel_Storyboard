"""TSB-321: one release version, no algorithm/cache invalidation side effect."""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def test_release_version_has_one_authority_and_api_agrees(client):
    from app import __version__
    from app.config import APP_VERSION, ANALYSIS_VERSION, get_settings
    target = (ROOT/'VERSION').read_text(encoding='utf-8').strip()
    assert target == __version__ == APP_VERSION == '2.1.2'
    assert json.loads((ROOT/'frontend/package.json').read_text(encoding='utf-8'))['version'] == target
    assert client.get('/api/system/health').json()['version'] == target
    assert client.get('/openapi.json').json()['info']['version'] == target
    assert ANALYSIS_VERSION == '2.0.2'
    assert get_settings().vision_prompt_version == get_settings().vision_schema_version == '1.0'


def test_checked_generation_and_launchers_cannot_silently_drift():
    result = subprocess.run([sys.executable,str(ROOT/'scripts/sync_version.py'),'--check'],capture_output=True,text=True)
    assert result.returncode == 0, result.stdout+result.stderr
    for name in ('start.sh','start.ps1'):
        text=(ROOT/'scripts'/name).read_text(encoding='utf-8')
        assert 'VERSION' in text and 'Travel Storyboard v2.0"' not in text


def test_package_drift_is_rejected_without_modifying_files(tmp_path):
    import importlib.util
    path=ROOT/'scripts/sync_version.py'
    spec=importlib.util.spec_from_file_location('version_sync',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    (tmp_path/'frontend').mkdir();(tmp_path/'VERSION').write_text('2.1.2\n',encoding='utf-8')
    package=tmp_path/'frontend/package.json';package.write_text('{"version":"2.1.0","name":"test"}\n',encoding='utf-8')
    before=package.read_bytes()
    assert module.synchronize(tmp_path,check=True) is False
    assert package.read_bytes()==before
    assert module.synchronize(tmp_path,check=False) is True
    assert json.loads(package.read_text(encoding='utf-8'))['version']=='2.1.2'


def test_release_number_does_not_change_analysis_or_model_cache_keys(monkeypatch):
    from app import config
    from app.services.analyzer import _detect_config_key
    from app.services.vision.semantic import Candidate, source_signature
    candidate=Candidate(segment_id='stable-shot',asset_id='stable-asset',project_id='stable-project',
                        start_sec=1.,end_sec=2.,source_identity='verified-source',
                        actual_manifest=[['stable-frame','t=1.5','actual-jpeg-digest']])
    before=(_detect_config_key(config.get_settings()),source_signature([candidate]))
    monkeypatch.setattr(config,'APP_VERSION','999.0.0')
    assert before==(_detect_config_key(config.get_settings()),source_signature([candidate]))
    assert config.ANALYSIS_VERSION=='2.0.2'


def test_current_readme_and_generated_analysis_package_use_release_version(client):
    import zipfile
    from test_review_package_privacy import _seed_project
    from app.paths import resolve_in_project
    from app.services.review_package import _build_package
    assert (ROOT/'README.md').read_text(encoding='utf-8').splitlines()[0]=='# Travel Storyboard v2.1.2'
    pid,_,_=_seed_project(client,'release-package')
    result=_build_package(pid,{})
    with zipfile.ZipFile(resolve_in_project(pid,result['zip'])) as archive:
        manifests=[name for name in archive.namelist() if Path(name).name=='manifest.json']
        assert len(manifests)==1
        manifest=json.loads(archive.read(manifests[0]))
    assert manifest['app_version']=='2.1.2' and manifest['analysis_version']=='2.0.2'
