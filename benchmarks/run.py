"""Reproducible warm-cache library/fresh-process benchmark runner."""
from __future__ import annotations
import json, os, platform, resource, shutil, sqlite3, statistics, subprocess, sys, tempfile, threading, time
from pathlib import Path
from memhub import create_vault,open_vault,import_source,list_entries,read_file,write_file,edit_file,remove_entry
from memhub.models import Edit
from memhub.encoding import decode_bytes
from .corpus import generate_corpus,SMALL,SCALE
from .filesystem import FilesystemStore

OPS=('immediate_listing','recursive_listing','whole_read','range_read','create','overwrite','exact_edit','single_delete','subtree_delete','bulk_ingestion')
MODES=('sqlite_library','filesystem_library','sqlite_cli','filesystem_cli')

def _stats(samples):
    ordered=sorted(samples); return {'median_seconds':statistics.median(ordered),'p95_seconds':ordered[max(0,(95*len(ordered)+99)//100-1)],'samples_seconds':samples}
def _measure(fn,n):
    values=[]
    for _ in range(n):
        start=time.perf_counter(); fn(); values.append(time.perf_counter()-start)
    return _stats(values)
def _size(root): return sum(p.stat().st_size for p in Path(root).rglob('*') if p.is_file())
def _cpu():
    try:
        for line in Path('/proc/cpuinfo').read_text().splitlines():
            if line.startswith('model name'): return line.split(':',1)[1].strip()
    except OSError: pass
    return platform.processor() or 'unknown'
def _storage(path):
    result={'mount_source':'unknown','mount_target':'unknown','filesystem_type':'unknown','rotational':'unknown'}
    try:
        fields=subprocess.run(['findmnt','-no','SOURCE,TARGET,FSTYPE','-T',str(path)],capture_output=True,text=True,check=True).stdout.strip().split()
        if len(fields) >= 3: result.update(mount_source=fields[0],mount_target=fields[1],filesystem_type=fields[2])
        device=Path(result['mount_source']).name.rstrip('0123456789')
        rotational=Path('/sys/class/block')/device/'queue/rotational'
        if rotational.exists(): result['rotational']='rotational' if rotational.read_text().strip()=='1' else 'non-rotational'
    except (OSError,subprocess.SubprocessError): pass
    return result

def _peak_rss(pid):
    try:
        for line in Path(f'/proc/{pid}/status').read_text().splitlines():
            if line.startswith('VmRSS:'): return int(line.split()[1])
    except OSError: pass
    return 0

def _transient_size(path):
    try: return Path(path).stat().st_size
    except OSError: return 0
def _run(cmd,stdin=None,journal=None):
    p=subprocess.Popen(cmd,stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
    peak=journal_peak=0
    if stdin is not None: p.stdin.write(stdin); p.stdin.close()
    while p.poll() is None:
        peak=max(peak,_peak_rss(p.pid))
        if journal: journal_peak=max(journal_peak,_transient_size(journal))
        time.sleep(.001)
    err=p.stderr.read()
    if p.returncode: raise RuntimeError(f"benchmark command failed {p.returncode}: {' '.join(cmd)}: {err[-500:]}")
    return peak,journal_peak
def _timed_cmd(cmd,stdin=None,journal=None):
    start=time.perf_counter(); rss,journal_size=_run(cmd,stdin,journal); return time.perf_counter()-start,rss,journal_size

def run_profile(profile,seed,output):
    if profile not in ('small','scale'): raise ValueError('profile must be small or scale')
    count,target=SMALL if profile=='small' else SCALE
    samples=30
    bulk_samples=3
    work=Path(tempfile.mkdtemp(prefix='memhub-bench-')); started=time.perf_counter()
    try:
        corpus=work/'corpus'; manifest=generate_corpus(corpus,count,target,seed)
        vault_path=work/'vault.db'; create_vault(vault_path); fsroot=work/'files'
        metrics={m:{} for m in MODES}; peak_journal=peak_child=0
        # Library imports and read/list fixtures.
        with open_vault(vault_path) as vault:
            stop=False; observed=[0]
            def watch():
                j=Path(str(vault_path)+'-journal')
                while not stop:
                    observed[0]=max(observed[0],_transient_size(j))
                    time.sleep(.001)
            thread=threading.Thread(target=watch); thread.start(); t=time.perf_counter(); ir=import_source(vault,corpus,'/data'); sqlite_bulk=[time.perf_counter()-t]; stop=True; thread.join(); peak_journal=max(peak_journal,observed[0])
            for sample in range(1, bulk_samples):
                sample_path=work/f'vault-bulk-{sample}.db'; create_vault(sample_path)
                with open_vault(sample_path) as sample_vault:
                    t=time.perf_counter(); import_source(sample_vault,corpus,'/data'); sqlite_bulk.append(time.perf_counter()-t)
                sample_path.unlink()
            with FilesystemStore(fsroot) as fs:
                t=time.perf_counter(); fr=fs.import_source(corpus,'/data'); fs_bulk=[time.perf_counter()-t]
                for sample in range(1, bulk_samples):
                    sample_root = work/f'files-bulk-{sample}'
                    with FilesystemStore(sample_root) as sample_fs:
                        t=time.perf_counter(); sample_fs.import_source(corpus,'/data'); fs_bulk.append(time.perf_counter()-t)
                    shutil.rmtree(sample_root)
                rel=next(iter(manifest.documents)); vp='/data/'+rel
                pairs={'sqlite_library':(vault,lambda n,*a,**k: globals()[n](vault,*a,**k)), 'filesystem_library':(fs,lambda n,*a,**k:getattr(fs,n)(*a,**k))}
                for mode,(store,call) in pairs.items():
                    metrics[mode]['immediate_listing']=_measure(lambda c=call:c('list_entries','/data'),samples)
                    metrics[mode]['recursive_listing']=_measure(lambda c=call:c('list_entries','/data',True,100),samples)
                    metrics[mode]['whole_read']=_measure(lambda c=call:c('read_file',vp),samples)
                    metrics[mode]['range_read']=_measure(lambda c=call:c('read_file',vp,1,10),samples)
                    vals={x:[] for x in OPS[4:9]}
                    for i in range(samples):
                        p=f'/bench/lib-{mode}-{i}'; st=time.perf_counter(); call('write_file',p,'alpha',if_absent=True); vals['create'].append(time.perf_counter()-st)
                        h=call('read_file',p).content_hash; st=time.perf_counter(); call('write_file',p,'beta',if_match=h); vals['overwrite'].append(time.perf_counter()-st)
                        h=call('read_file',p).content_hash; st=time.perf_counter(); call('edit_file',p,[Edit('beta','gamma')],if_match=h); vals['exact_edit'].append(time.perf_counter()-st)
                        st=time.perf_counter(); call('remove_entry',p); vals['single_delete'].append(time.perf_counter()-st)
                        tree=f'/bench/tree-lib-{mode}-{i}'; call('write_file',tree+'/x','x'); st=time.perf_counter(); call('remove_entry',tree,recursive=True); vals['subtree_delete'].append(time.perf_counter()-st)
                    for op,v in vals.items(): metrics[mode][op]=_stats(v)
                metrics['sqlite_library']['bulk_ingestion']=_stats(sqlite_bulk); metrics['filesystem_library']['bulk_ingestion']=_stats(fs_bulk)
                # Every CLI sample is a fresh process. Fixtures are reset outside timing.
                py=sys.executable
                bases={'sqlite_cli':[py,'-m','memhub','--vault',str(vault_path),'--json'], 'filesystem_cli':[py,'-m','benchmarks.filesystem','--root',str(fsroot)]}
                for mode,base in bases.items():
                    is_sql=mode=='sqlite_cli'; cmd=lambda *x:base+list(x)
                    command_sets={
                      'immediate_listing':(cmd('ls','/data'),None), 'recursive_listing':(cmd('ls','/data','--recursive','--limit','100'),None),
                      'whole_read':(cmd('read',vp),None), 'range_read':(cmd('read',vp,'--start-line','1','--lines','10'),None)}
                    for op,(args,data) in command_sets.items(): metrics[mode][op]=_stats([_timed_cmd(args,data)[0] for _ in range(samples)])
                    vals={x:[] for x in OPS[4:9]}
                    for i in range(samples):
                        p=f'/bench/cli-{mode}-{i}'
                        elapsed,rss,j=_timed_cmd(cmd('write',p,'--if-absent'),'alpha',str(vault_path)+'-journal' if is_sql else None); vals['create'].append(elapsed); peak_child=max(peak_child,rss); peak_journal=max(peak_journal,j)
                        h=(read_file(vault,p) if is_sql else fs.read_file(p)).content_hash
                        elapsed,rss,j=_timed_cmd(cmd('write',p,'--if-match',h),'beta',str(vault_path)+'-journal' if is_sql else None); vals['overwrite'].append(elapsed); peak_child=max(peak_child,rss); peak_journal=max(peak_journal,j)
                        h=(read_file(vault,p) if is_sql else fs.read_file(p)).content_hash
                        elapsed,rss,j=_timed_cmd(cmd('edit',p,'--if-match',h),json.dumps([{'old_text':'beta','new_text':'gamma'}]),str(vault_path)+'-journal' if is_sql else None); vals['exact_edit'].append(elapsed); peak_child=max(peak_child,rss); peak_journal=max(peak_journal,j)
                        elapsed,rss,j=_timed_cmd(cmd('rm',p),journal=str(vault_path)+'-journal' if is_sql else None); vals['single_delete'].append(elapsed); peak_child=max(peak_child,rss); peak_journal=max(peak_journal,j)
                        tree=f'/bench/tree-cli-{mode}-{i}'; (write_file(vault,tree+'/x','x') if is_sql else fs.write_file(tree+'/x','x'))
                        elapsed,rss,j=_timed_cmd(cmd('rm',tree,'--recursive'),journal=str(vault_path)+'-journal' if is_sql else None); vals['subtree_delete'].append(elapsed); peak_child=max(peak_child,rss); peak_journal=max(peak_journal,j)
                    for op,v in vals.items(): metrics[mode][op]=_stats(v)
        # Measure validation separately so operation timings expose open cost.
        fast_open=[]
        for _ in range(samples):
            t=time.perf_counter()
            with open_vault(vault_path): pass
            fast_open.append(time.perf_counter()-t)
        full_audit=[]
        for _ in range(bulk_samples):
            t=time.perf_counter()
            with open_vault(vault_path,audit=True): pass
            full_audit.append(time.perf_counter()-t)
        # Fresh-process bulk imports into fresh stores (setup excluded).
        sqlite_cli_bulk=[]; filesystem_cli_bulk=[]
        for sample in range(bulk_samples):
            cli_vault=work/f'cli-vault-{sample}.db'; create_vault(cli_vault)
            e,r,j=_timed_cmd([sys.executable,'-m','memhub','--vault',str(cli_vault),'--json','import',str(corpus),'/data'],journal=str(cli_vault)+'-journal'); sqlite_cli_bulk.append(e); peak_child=max(peak_child,r); peak_journal=max(peak_journal,j)
            cli_vault.unlink()
            cli_fs=work/f'cli-files-{sample}'; e,r,j=_timed_cmd([sys.executable,'-m','benchmarks.filesystem','--root',str(cli_fs),'import',str(corpus),'/data']); filesystem_cli_bulk.append(e); peak_child=max(peak_child,r)
            shutil.rmtree(cli_fs)
        metrics['sqlite_cli']['bulk_ingestion']=_stats(sqlite_cli_bulk)
        metrics['filesystem_cli']['bulk_ingestion']=_stats(filesystem_cli_bulk)
        # Decoding-only costs: file reads and all storage writes are outside the
        # timer. Direct UTF-8 must not be mislabeled bulk ingestion.
        utf8_seconds=0.0
        for source in corpus.rglob('*'):
            if source.is_file():
                data=source.read_bytes(); t=time.perf_counter(); decode_bytes(data); utf8_seconds += time.perf_counter()-t
        legacy_data=[('café %d'%i).encode('cp1252') for i in range(min(count,100))]
        t=time.perf_counter()
        for data in legacy_data: decode_bytes(data)
        detector_seconds=time.perf_counter()-t
        storage=_storage(work)
        env={'python':platform.python_version(),'sqlite':sqlite3.sqlite_version,'chardet':__import__('chardet').__version__,'platform':platform.platform(),'cpu':_cpu(),'logical_cpus':os.cpu_count(),'memory_bytes':os.sysconf('SC_PAGE_SIZE')*os.sysconf('SC_PHYS_PAGES'),'filesystem':storage['filesystem_type'],'storage':storage,'cache_policy':'warm OS cache; no global cache dropping','durability':{'sqlite':'rollback journal DELETE, synchronous FULL, atomic transactions','filesystem':'file and parent-directory fsync per mutation; bulk is durable per file but not atomic'}}
        report={'report_version':2,'profile':profile,'environment':env,'corpus':{'seed':seed,'document_count':manifest.document_count,'input_bytes':manifest.input_bytes},'metrics':metrics,'validation_phases':{'fast_open':_stats(fast_open),'full_audit':_stats(full_audit)},'resources':{'runner_peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,'peak_cli_rss_kib':peak_child,'database_bytes':vault_path.stat().st_size,'filesystem_bytes':_size(fsroot),'peak_journal_bytes':peak_journal},'throughput':{'sqlite_library_documents_per_second':count/statistics.median(sqlite_bulk),'filesystem_library_documents_per_second':count/statistics.median(fs_bulk),'sqlite_cli_documents_per_second':count/statistics.median(sqlite_cli_bulk),'filesystem_cli_documents_per_second':count/statistics.median(filesystem_cli_bulk)},'encoding_detection':{'method':'decoding only; source reads and destination writes excluded','direct_utf8_documents':count,'direct_utf8_seconds':utf8_seconds,'legacy_detector_documents':len(legacy_data),'legacy_detector_seconds':detector_seconds},'checks':{'sqlite_files':ir.files,'filesystem_files':fr.files,'hash_manifest_entries':len(manifest.documents)},'caveats':['Warm OS caches; fresh process does not imply cold cache.','Storage medium is reported only when the OS exposes it; tmpfs or unknown media are not described as SSD.','Filesystem bulk import is durable per file but has no equivalent multi-file rollback.','Every ordinary operation has 30 samples and every bulk ingestion has three samples in every mode.'],'elapsed_seconds':time.perf_counter()-started}
        validate_report(report,count,target); output=Path(output); output.parent.mkdir(parents=True,exist_ok=True); output.write_text(json.dumps(report,indent=2)); return report
    finally: shutil.rmtree(work,ignore_errors=True)

def validate_report(report,expected_count,expected_bytes):
    if report.get('report_version')!=2: raise ValueError('unsupported report version')
    if report.get('corpus',{}).get('document_count')!=expected_count or report.get('corpus',{}).get('input_bytes',0)<expected_bytes: raise ValueError('corpus mismatch or undersized')
    env=report.get('environment',{}); required_env=('python','sqlite','chardet','platform','cpu','logical_cpus','memory_bytes','filesystem','cache_policy','durability')
    if any(not env.get(k) for k in required_env): raise ValueError('missing environment evidence')
    if 'cold' in env['cache_policy'].lower() and 'method' not in env['cache_policy'].lower(): raise ValueError('undocumented cold cache')
    identities=[]
    for mode in MODES:
        for op in OPS:
            metric=report.get('metrics',{}).get(mode,{}).get(op,{})
            samples=metric.get('samples_seconds')
            expected_samples = 3 if op == 'bulk_ingestion' else 30
            if not samples or len(samples) != expected_samples or any(x<0 for x in samples) or 'median_seconds' not in metric or 'p95_seconds' not in metric: raise ValueError(f'missing/invalid metric {mode}/{op}')
            identities.append((mode,op,id(samples)))
    resources=report.get('resources',{})
    for key in ('runner_peak_rss_kib','peak_cli_rss_kib','database_bytes','filesystem_bytes','peak_journal_bytes'):
        if resources.get(key) is None or resources[key]<0: raise ValueError(f'missing resource {key}')
    if resources['peak_journal_bytes']==0: raise ValueError('journal was not observed')
    encoding=report.get('encoding_detection',{})
    if not report.get('throughput') or 'decoding only' not in encoding.get('method',''): raise ValueError('missing throughput/decoding-only separation')
    phases=report.get('validation_phases',{})
    if len(phases.get('fast_open',{}).get('samples_seconds',[])) != 30 or len(phases.get('full_audit',{}).get('samples_seconds',[])) != 3: raise ValueError('missing validation phase measurements')
    if not env.get('storage') or not report.get('checks') or not report.get('caveats'): raise ValueError('missing checks/caveats/storage')

def main(argv=None):
    import argparse
    p=argparse.ArgumentParser(); p.add_argument('profile',choices=['small','scale']); p.add_argument('--seed',type=int,default=20260917); p.add_argument('--output',type=Path,required=True); a=p.parse_args(argv); run_profile(a.profile,a.seed,a.output); return 0
if __name__=='__main__': raise SystemExit(main())
