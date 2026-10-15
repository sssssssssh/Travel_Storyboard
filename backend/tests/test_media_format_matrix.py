"""TSB-322 L1 only: generated signals through production API/Runner/FFmpeg.

The PQ/HLG inputs are mathematically encoded transfer-function ramps, not SDR
testsrc with an HDR tag. Readable previews do NOT establish color accuracy.
"""
import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

from _media import require_ffmpeg
from app.database import session_scope
from app.ffmpeg import run, locate_ffprobe, probe_media, extract_frame, safe_image_open
from app.models import Asset, Frame
from app.paths import resolve_in_project
from app.services.runner import runner, TASK_REGISTRY

pytestmark = pytest.mark.needs_ffmpeg
CASES = {
    'h264_mp4': {'id':'M322-01','codec':'h264','audio':True},
    'hevc_mp4': {'id':'M322-02','codec':'hevc','audio':True},
    'h264_mov': {'id':'M322-03','codec':'h264','audio':True,'suffix':'.mov'},
    'portrait': {'id':'M322-04','codec':'h264','audio':True,'size':'72x128'},
    'silent': {'id':'M322-05','codec':'h264','audio':False},
    'pq_10bit': {'id':'M322-06-PQ','codec':'hevc','audio':False,'transfer':'smpte2084'},
    'hlg_10bit': {'id':'M322-06-HLG','codec':'hevc','audio':False,'transfer':'arib-std-b67'},
    'fps60': {'id':'M322-07-CFR','codec':'h264','audio':True,'fps':'60'},
    'vfr': {'id':'M322-07-VFR','codec':'h264','audio':True,'vfr':True},
    'rotation': {'id':'M322-04-ROT','codec':'h264','audio':True,'size':'128x72','rotation':True},
}


def sha(path):
    with open(path,'rb') as f: return hashlib.file_digest(f,'sha256').hexdigest()


def raw_probe(path):
    return json.loads(run([locate_ffprobe(),'-v','error','-show_streams','-show_format','-of','json',str(path)]).stdout)


def synthesize(directory, name):
    exe=require_ffmpeg();case=CASES[name];directory.mkdir(parents=True,exist_ok=True)
    path=directory/(name+case.get('suffix','.mp4'));size=case.get('size','96x64');fps=case.get('fps','24')
    transfer=case.get('transfer')
    signal=f'testsrc2=size={size}:rate={fps}:duration=1.3'
    if transfer:
        x='X/(W-1)'
        if transfer=='smpte2084':
            # ST2084, 0..1000 cd/m² divided by 10000 cd/m², legal-range 10 bit.
            e=f'pow((0.8359375+18.8515625*pow(0.1*{x},0.1593017578125))/(1+18.6875*pow(0.1*{x},0.1593017578125)),78.84375)'
        else:
            # BT.2100 HLG OETF on a normalized scene-linear ramp.
            e=f'if(lte({x},1/12),sqrt(3*{x}),0.17883277*log(12*{x}-0.28466892)+0.55991073)'
        signal=f"nullsrc=s={size}:r={fps}:d=1.3,format=yuv420p10le,geq=lum='64+876*({e})':cb=512:cr=512"
    args=[exe,'-y','-f','lavfi','-i',signal]
    if case['audio']: args+=['-f','lavfi','-i','sine=frequency=440:duration=1.3','-c:a','aac']
    args+=['-c:v','libx265' if case['codec']=='hevc' else 'libx264','-preset','ultrafast','-threads','1']
    if case['codec']=='hevc':
        vui=f':colorprim=9:transfer={16 if transfer=="smpte2084" else 18}:colormatrix=9:range=limited' if transfer else ''
        args+=['-x265-params','pools=1:frame-threads=1:log-level=error'+vui]
    args+=['-pix_fmt','yuv420p10le' if transfer else 'yuv420p']
    if transfer:args+=['-color_primaries','bt2020','-color_trc',transfer,'-colorspace','bt2020nc']
    if case.get('vfr'):args+=['-vf',"setpts='if(lt(N,12),N/(24*TB),(0.5+(N-12)/12)/TB)'",'-fps_mode','vfr']
    args += ['-t','1.3',str(path)]
    run(args,expect_output=path)
    if case.get('rotation'):
        rotated=path.with_name('rotated.mp4')
        run([exe,'-y','-display_rotation:v:0','90','-i',str(path),'-c','copy',str(rotated)],expect_output=rotated)
        path=rotated
    return path


def project(client,tmp_path,sources):
    p=client.post('/api/projects',json={'name':'L1 format matrix','source_roots':[str(sources)],'output_dir':str(tmp_path/'workspace')})
    assert p.status_code==201;return p.json()['id']


def job(client,pid,kind,body=None):
    response=client.post(f'/api/projects/{pid}/jobs/{kind}',json=body or {})
    assert response.status_code==202,response.text
    jid=response.json()['id'];runner._run(jid,TASK_REGISTRY[kind])
    return client.get('/api/jobs/'+jid).json()


def report(record_property,record):
    record_property('format_matrix',json.dumps(record,ensure_ascii=False))


@pytest.mark.parametrize('name',CASES)
def test_format_runs_real_scan_detect_proxy_and_export(name,client,tmp_path,monkeypatch,record_property):
    monkeypatch.setattr(runner,'submit',lambda _:None)
    source=synthesize(tmp_path/'sources',name);before=(sha(source),source.stat().st_mtime_ns,source.stat().st_size)
    raw=raw_probe(source);video=next(s for s in raw['streams'] if s['codec_type']=='video');case=CASES[name]
    assert video['codec_name']==case['codec']
    assert any(s['codec_type']=='audio' for s in raw['streams'])==case['audio']
    if case.get('transfer'):
        assert video['color_transfer']==case['transfer'] and video['color_primaries']=='bt2020'
        assert '10' in video['pix_fmt']
    if name=='h264_mov':assert raw['format']['tags']['major_brand'].strip()=='qt'
    if case.get('rotation'):assert any(s.get('rotation') for s in video.get('side_data_list',[]))
    if name=='fps60':assert video['avg_frame_rate']=='60/1'
    if case.get('vfr'):
        frames=json.loads(run([locate_ffprobe(),'-v','error','-select_streams','v:0','-show_entries','frame=best_effort_timestamp_time','-of','json',str(source)]).stdout)['frames']
        times=[float(f['best_effort_timestamp_time']) for f in frames]
        assert len({round(b-a,4) for a,b in zip(times,times[1:])})>1
    pid=project(client,tmp_path,source.parent)
    scan=job(client,pid,'scan');assert scan['status']=='succeeded'
    detected=job(client,pid,'detect');assert detected['status']=='succeeded'
    shots=client.get(f'/api/projects/{pid}/segments').json()['items'];assert shots
    selected=shots[0];sid=selected['id']
    with session_scope() as s:
        asset=s.get(Asset,selected['asset_id'])
        assert asset.codec==case['codec'] and asset.has_audio==case['audio']
        display_ratio=(asset.height/asset.width if asset.rotation in (90,270) else asset.width/asset.height)
        frame=s.scalars(select(Frame).where(Frame.segment_id==sid)).first();assert frame
        path=resolve_in_project(pid,frame.image_path)
        with safe_image_open(path) as image:assert image.width/image.height==pytest.approx(display_ratio,rel=.025)
        assert client.get(f'/api/projects/{pid}/media',params={'path':frame.image_path}).status_code==200
    start=selected['start_sec']+.05;end=min(selected['end_sec']-.02,start+1.)
    item={'asset_id':selected['asset_id'],'segment_id':sid,'start_sec':start,'end_sec':end}
    admitted=client.post(f'/api/projects/{pid}/review-requests/import',json={'project_id':pid,'requests':[item]}).json()
    assert admitted['created']==1 and admitted['errors']==[]
    proxies=job(client,pid,'proxies');assert proxies['status']=='succeeded',proxies
    requests=client.get(f'/api/projects/{pid}/review-requests').json();rr=requests[0];assert rr['status']=='proxied'
    proxy=resolve_in_project(pid,rr['proxy_ref']);actual=probe_media(proxy)
    assert abs(actual.duration_sec-(end-start))<=max(.25,.1*(end-start))
    assert actual.video.width/actual.video.height==pytest.approx(display_ratio,rel=.025)
    plan={'asset_id':item['asset_id'],'segment_id':sid,'source_in_sec':start,'source_out_sec':end,'target_duration_sec':end-start,'review_status':'confirmed'}
    saved=client.post(f'/api/projects/{pid}/edit-plan',json={'shots':[plan]}).json();assert saved['created']==1
    for fmt in ('json','csv','otio'):
        exported=client.get(f'/api/projects/{pid}/export/{fmt}');assert exported.status_code==200 and exported.content
    rough=job(client,pid,'roughcut');assert rough['status']=='succeeded',rough
    output=resolve_in_project(pid,rough['resume_token']['path']);info=probe_media(output)
    assert info.has_video and info.has_audio
    assert (info.video.width,info.video.height)==(1280,720)
    assert abs(info.duration_sec-(end-start))<=max(.25,.1*(end-start))
    if not case.get('transfer'):
        preview=tmp_path/'rough-frame.jpg';extract_frame(output,.1,preview,width=1280)
        with Image.open(preview) as im:
            bounds=im.convert('L').point(lambda v:255 if v>16 else 0).getbbox();assert bounds
            width,height=bounds[2]-bounds[0],bounds[3]-bounds[1]
            assert width/height==pytest.approx(display_ratio,rel=.04)
    # A forced detect through the API preserves the stable mapping.
    ids={shot['id'] for shot in shots}
    assert job(client,pid,'detect',{'force':True})['status']=='succeeded'
    assert {shot['id'] for shot in client.get(f'/api/projects/{pid}/segments').json()['items']}==ids
    assert (sha(source),source.stat().st_mtime_ns,source.stat().st_size)==before
    record={'case_id':case['id'],'variant':name,'original_or_synthetic':'synthetic','codec':video['codec_name'],'container':raw['format']['format_name'],'dimensions':[video['width'],video['height']],'audio':case['audio'],'transfer':video.get('color_transfer','unknown'),'pixel_format':video['pix_fmt'],'ffmpeg_version':run([require_ffmpeg(),'-version']).stdout.decode().splitlines()[0],'scan':'passed','detect':'passed','frames':'readable; display aspect checked','proxy':'readable; aspect/duration checked','export':'JSON/CSV/OTIO + real 1280x720 roughcut with original aspect/padding and AAC/silence','result':'compatible_with_color_management_limit' if case.get('transfer') else 'passed','error_code':'','source_sha256_before_after':{'before':before[0],'after':sha(source)},'source_mtime_size_unchanged':True,'limits':['HDR preview color accuracy/tone mapping not established'] if case.get('transfer') else []}
    report(record_property,record)


@pytest.mark.parametrize('invalid',['corrupt','empty','zero_duration'])
def test_invalid_container_is_visible_without_changing_bytes(invalid,client,tmp_path,monkeypatch,record_property):
    monkeypatch.setattr(runner,'submit',lambda _:None);root=tmp_path/'sources';root.mkdir();source=root/'invalid.mp4'
    if invalid=='zero_duration':
        run([require_ffmpeg(),'-y','-f','lavfi','-i','color=s=64x64','-frames:v','0','-c:v','libx264',str(source)],expect_output=source)
    else:source.write_bytes(b'broken controlled container' if invalid=='corrupt' else b'')
    before=(sha(source),source.stat().st_mtime_ns,source.stat().st_size);pid=project(client,tmp_path,root);result=job(client,pid,'scan')
    assert result['status'] in ('partial_failed','failed') and result['stats']['failed']==1
    with session_scope() as s:
        asset=s.scalars(select(Asset).where(Asset.project_id==pid)).one();assert asset.scan_status=='failed' and asset.error_code
        code=asset.error_code
    assert (sha(source),source.stat().st_mtime_ns,source.stat().st_size)==before
    report(record_property,{'case_id':'M322-08-'+invalid,'original_or_synthetic':'synthetic','scan':'visible failure','result':'expected_failure','error_code':code,'source_sha256_before_after':{'before':before[0],'after':sha(source)},'source_mtime_size_unchanged':True})


def test_missing_source_after_scan_is_a_visible_detect_failure(client,tmp_path,monkeypatch,record_property):
    monkeypatch.setattr(runner,'submit',lambda _:None);source=synthesize(tmp_path/'sources','h264_mp4');pid=project(client,tmp_path,source.parent)
    assert job(client,pid,'scan')['status']=='succeeded';source.unlink()  # Controlled synthetic fixture only.
    result=job(client,pid,'detect');assert result['status'] in ('partial_failed','failed') and result['failures']['count']>0
    assert result['failures']['items'][0].get('error_code')=='source_unavailable'
    assert result['resume_token']['items'][0]['error_code']=='source_unavailable'
    report(record_property,{'case_id':'M322-08-missing','original_or_synthetic':'synthetic','scan':'passed','detect':'visible failure','result':'expected_failure','failure_codes':[i.get('error_code') for i in result['failures']['items']],'source_sha256_before_after':'deliberately removed owned synthetic fixture after scan; no user footage involved'})


def test_mixed_project_retains_good_formats_and_isolates_corruption(client,tmp_path,monkeypatch,record_property):
    monkeypatch.setattr(runner,'submit',lambda _:None);root=tmp_path/'sources'
    sources=[synthesize(root,name) for name in ('h264_mp4','hevc_mp4','h264_mov','portrait')]
    before={p.name:sha(p) for p in sources};(root/'bad.mp4').write_bytes(b'bad')
    pid=project(client,tmp_path,root);scan=job(client,pid,'scan');assert scan['stats']['done']==5 and scan['stats']['failed']==1
    detect=job(client,pid,'detect');assert detect['stats']['done']==4
    shots=client.get(f'/api/projects/{pid}/segments').json()['items'];assert len({s['asset_id'] for s in shots})==4
    assert before=={p.name:sha(p) for p in sources}
    report(record_property,{'case_id':'M322-10','original_or_synthetic':'synthetic','scan':'4 valid / 1 visible failure','detect':'4 valid assets','frames':'readable','result':'passed','source_sha256_before_after':before})


def test_matrix_is_explicitly_forced_in_the_media_job():
    workflow=Path(__file__).resolve().parents[2]/'.github/workflows/ci.yml'
    assert 'tests/test_media_format_matrix.py' in workflow.read_text()
