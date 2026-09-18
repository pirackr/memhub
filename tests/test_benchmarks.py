from pathlib import Path
import hashlib
from benchmarks.corpus import generate_corpus
from benchmarks.filesystem import FilesystemStore
from benchmarks.run import validate_report, OPS, MODES, _stats
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


def test_statistics_and_report_validation():
    assert _stats(list(range(1,21)))['p95_seconds']==19
    metric={op:{'samples_seconds':[.1] * (3 if op == 'bulk_ingestion' else 30),'median_seconds':.1,'p95_seconds':.1} for op in OPS}
    env={k:'x' for k in ('python','sqlite','chardet','platform','cpu','filesystem','cache_policy','durability')}; env.update(logical_cpus=1,memory_bytes=1)
    report={'report_version':2,'corpus':{'document_count':2,'input_bytes':20},'environment':env,
            'metrics':{m:{op:dict(value) for op,value in metric.items()} for m in MODES},
            'resources':{'runner_peak_rss_kib':1,'peak_cli_rss_kib':1,'database_bytes':1,'filesystem_bytes':1,'peak_journal_bytes':1},
            'throughput':{'x':1},'encoding_detection':{'x':1},'checks':{'x':1},'caveats':['warm']}
    validate_report(report,2,20)
    broken=dict(report); broken['metrics']={m:dict(metric) for m in MODES}; broken['metrics']['sqlite_library'].pop('whole_read')
    import pytest
    with pytest.raises(ValueError): validate_report(broken,2,20)
    with pytest.raises(ValueError): validate_report(report,100000,1000000000)
