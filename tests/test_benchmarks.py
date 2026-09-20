from pathlib import Path
import hashlib
from benchmarks.corpus import generate_corpus
from benchmarks.filesystem import FilesystemStore
from benchmarks.run import validate_report, OPS, MODES, _stats, _storage
from memhub.models import Edit


def test_corpus_repeatable_budget_and_shape(tmp_path):
    a=generate_corpus(tmp_path/'a',30,30000,42); b=generate_corpus(tmp_path/'b',30,30000,42)
    assert a==b and a.input_bytes==30000 and len(a.documents)==30
    assert any('层' in p for p in a.documents)
    for rel,digest in a.documents.items(): assert hashlib.sha256((tmp_path/'a'/rel).read_bytes()).hexdigest()==digest


def test_filesystem_backend_workload(tmp_path):
    src=tmp_path/'src'; generate_corpus(src,5,5000,1)
    with FilesystemStore(tmp_path/'store') as s:
        result=s.import_source(src,'/docs'); assert result.files==5
        entries=s.list_entries('/docs',recursive=True,limit=3); assert len(entries.entries)==3 and entries.has_more
        w=s.write_file('/x','a\r\nb',if_absent=True); assert s.read_file('/x',2,1).content=='b'
        w=s.edit_file('/x',[Edit('b','c')],if_match=w.content_hash); assert s.read_file('/x').content=='a\r\nc'
        s.remove_entry('/docs',recursive=True)


def test_main_propagates_explicit_work_dir(monkeypatch, tmp_path):
    import benchmarks.run as runner
    seen=[]
    monkeypatch.setattr(runner,'run_profile',lambda *args,**kwargs: seen.append((args,kwargs)))
    output=tmp_path/'report.json'; work_root=tmp_path/'ssd-work'
    assert runner.main(['small','--seed','17','--output',str(output),'--work-dir',str(work_root)])==0
    assert seen==[(('small',17,output),{'work_dir':work_root})]


def test_run_profile_cleans_own_child_after_corpus_failure(monkeypatch, tmp_path):
    import benchmarks.run as runner
    import pytest
    parent=tmp_path/'ssd-work'; parent.mkdir(); sentinel=parent/'keep'; sentinel.write_text('unrelated')
    output=tmp_path/'outside'/'report.json'
    def fail(corpus,*args):
        assert corpus.parent.parent==parent
        assert corpus.parent.name.startswith('memhub-bench-')
        raise RuntimeError('injected corpus failure')
    monkeypatch.setattr(runner,'generate_corpus',fail)
    with pytest.raises(RuntimeError,match='injected corpus failure'):
        runner.run_profile('small',17,output,work_dir=parent)
    assert sentinel.read_text()=='unrelated'
    assert list(parent.iterdir())==[sentinel]
    assert not output.exists()


def test_run_profile_cleans_own_child_after_success(monkeypatch, tmp_path):
    import contextlib, io, sys
    import benchmarks.run as runner
    from benchmarks import filesystem
    from memhub import cli

    parent=tmp_path/'ssd-work'; parent.mkdir(); sentinel=parent/'keep'; sentinel.write_text('unrelated')
    output=tmp_path/'outside'/'report.json'
    monkeypatch.setattr(runner,'SMALL',(2,300))

    def fake_timed(cmd,stdin=None,journal=None):
        module=cmd[cmd.index('-m')+1]
        argv=cmd[cmd.index(module)+1:]
        raw=io.BytesIO((stdin or '').encode())
        fake_stdin=io.TextIOWrapper(raw,encoding='utf-8')
        original_stdin=sys.stdin
        try:
            sys.stdin=fake_stdin
            with contextlib.redirect_stdout(io.StringIO()):
                result=(cli.main(argv) if module=='memhub' else filesystem.main(argv))
        finally:
            sys.stdin=original_stdin
        assert result==0
        return .001,7,1

    monkeypatch.setattr(runner,'_timed_cmd',fake_timed)
    report=runner.run_profile('small',17,output,work_dir=parent)
    assert report['corpus']['document_count']==2
    assert output.is_file() and parent not in output.parents
    assert sentinel.read_text()=='unrelated'
    assert list(parent.iterdir())==[sentinel]


def test_storage_btrfs_nvme_partition_evidence(monkeypatch):
    import json, subprocess
    replies=[
      {'filesystems':[{'source':'/dev/nvme0n1p2[/@home]','target':'/home','fstype':'btrfs','options':'rw,compress=zstd:3'}]},
      {'blockdevices':[{'name':'nvme0n1p2','kname':'nvme0n1p2','pkname':'nvme0n1','type':'part','rota':False,'model':None}]},
      {'blockdevices':[{'name':'nvme0n1','kname':'nvme0n1','pkname':None,'type':'disk','rota':False,'model':'WD_BLACK SN7100 2TB'}]},
    ]
    def fake_run(*args,**kwargs): return subprocess.CompletedProcess(args[0],0,json.dumps(replies.pop(0)),'')
    monkeypatch.setattr(subprocess,'run',fake_run)
    found=_storage('/home/example')
    assert found['mount_source']=='/dev/nvme0n1p2[/@home]'
    assert found['mount_options']=='rw,compress=zstd:3'
    assert found['block_device']=='/dev/nvme0n1'
    assert found['block_model']=='WD_BLACK SN7100 2TB'
    assert found['rotational']=='non-rotational'


def test_storage_fallback_keeps_unknown_physical_evidence(monkeypatch):
    import subprocess
    def fail(*args,**kwargs): raise subprocess.CalledProcessError(1,args[0])
    monkeypatch.setattr(subprocess,'run',fail)
    found=_storage('/not-provable')
    assert found['mount_source']=='unknown' and found['block_device']=='unknown'
    assert found['block_model']=='unknown' and found['rotational']=='unknown'


def test_statistics_and_report_validation():
    assert _stats(list(range(1,21)))['p95_seconds']==19
    metric={op:{'samples_seconds':[.1] * (3 if op == 'bulk_ingestion' else 30),'median_seconds':.1,'p95_seconds':.1} for op in OPS}
    env={k:'x' for k in ('python','sqlite','chardet','platform','cpu','filesystem','cache_policy','durability')}; env.update(logical_cpus=1,memory_bytes=1,storage={'filesystem_type':'x'})
    report={'report_version':2,'corpus':{'document_count':2,'input_bytes':20},'environment':env,
            'metrics':{m:{op:dict(value) for op,value in metric.items()} for m in MODES},
            'validation_phases':{'fast_open':{'samples_seconds':[.1]*30},'full_audit':{'samples_seconds':[.1]*3}},
            'resources':{'runner_peak_rss_kib':1,'peak_cli_rss_kib':1,'database_bytes':1,'filesystem_bytes':1,'peak_journal_bytes':1},
            'throughput':{'x':1},'encoding_detection':{'method':'decoding only; reads excluded'},'checks':{'x':1},'caveats':['warm']}
    validate_report(report,2,20)
    broken=dict(report); broken['metrics']={m:dict(metric) for m in MODES}; broken['metrics']['sqlite_library'].pop('whole_read')
    import pytest
    with pytest.raises(ValueError): validate_report(broken,2,20)
    with pytest.raises(ValueError): validate_report(report,100000,1000000000)
