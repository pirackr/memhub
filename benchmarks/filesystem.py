"""Durable ordinary-file reference backend (no multi-file atomic import)."""
from __future__ import annotations
import argparse, json, os, shutil, sys, tempfile, threading
from datetime import datetime,timezone
from pathlib import Path
from memhub.edits import apply_edits
from memhub.errors import Conflict, Missing
from memhub.models import Edit,Entry,ListResult,ReadResult,WriteResult,ImportResult
from memhub.paths import normalize_path
from memhub.text import content_hash,validate_text

class FilesystemStore:
    def __init__(self,root:Path): self.root=Path(root); self._lock=threading.Lock()
    def __enter__(self): self.root.mkdir(parents=True,exist_ok=True); return self
    def __exit__(self,*a): return False
    def _path(self,p):
        p=normalize_path(p); return self.root.joinpath(*p.strip('/').split('/')) if p!='/' else self.root
    def _entry(self,p,path):
        st=p.stat(); kind='directory' if p.is_dir() else 'file'; stamp=datetime.fromtimestamp(st.st_mtime,timezone.utc).isoformat()
        return Entry(0,path,kind,None if kind=='directory' else st.st_size,stamp,stamp)
    def list_entries(self,path='/',recursive=False,limit=100,offset=0):
        base=self._path(path); canonical=normalize_path(path)
        if not base.exists(): raise Missing('no_such_path',f'missing {canonical}')
        paths=[p for p in (base.rglob('*') if recursive else base.iterdir())]
        pairs=[]
        for p in paths:
            rel=p.relative_to(self.root).as_posix(); pairs.append(('/'+rel,self._entry(p,'/'+rel)))
        vals=[e for _,e in sorted(pairs,key=lambda x:x[0])]; page=vals[offset:offset+limit]; more=offset+limit<len(vals)
        return ListResult(page,more,offset+limit if more else None)
    def read_file(self,path,start_line=1,lines=None):
        p=self._path(path)
        if not p.is_file(): raise Missing('no_such_path',f'missing {path}')
        with p.open('r',encoding='utf-8',newline='') as handle: text=handle.read()
        chunks=text.splitlines(keepends=True)
        selected=chunks[start_line-1:] if lines is None else chunks[start_line-1:start_line-1+lines]
        content=''.join(selected); end=None if not selected else start_line+len(selected)-1
        return ReadResult(self._entry(p,normalize_path(path)),content,content_hash(text),start_line,end,start_line-1+len(selected)<len(chunks))
    def write_file(self,path,content,if_match=None,if_absent=False):
        validate_text(content); p=self._path(path)
        with self._lock:
            if p.exists() and if_absent: raise Conflict('if_absent_exists','destination exists')
            if if_match is not None and (not p.is_file() or content_hash(_read_exact(p))!=if_match): raise Conflict('stale_hash','hash mismatch')
            p.parent.mkdir(parents=True,exist_ok=True); fd,tmp=tempfile.mkstemp(dir=p.parent)
            try:
                with os.fdopen(fd,'w',encoding='utf-8',newline='') as f: f.write(content); f.flush(); os.fsync(f.fileno())
                os.replace(tmp,p); d=os.open(p.parent,os.O_DIRECTORY); os.fsync(d); os.close(d)
            finally:
                if os.path.exists(tmp): os.unlink(tmp)
        return WriteResult(self._entry(p,normalize_path(path)),content_hash(content))
    def edit_file(self,path,operations,if_match=None):
        current=self.read_file(path); return self.write_file(path,apply_edits(current.content,operations),if_match=if_match)
    def remove_entry(self,path,recursive=False):
        p=self._path(path); parent=p.parent
        if p.is_dir():
            if recursive: shutil.rmtree(p)
            else: p.rmdir()
        elif p.exists(): p.unlink()
        else: raise Missing('no_such_path',f'missing {path}')
        _fsync_dir(parent)
    def import_source(self,source,destination,on_warning=None):
        source=Path(source); before={p for p in self.root.rglob('*') if p.is_dir()}
        files=0
        if source.is_file(): self.write_file(destination,source.read_text(encoding='utf-8'),if_absent=True); files=1
        else:
            dest=self._path(destination); dest.mkdir(parents=True,exist_ok=True); _fsync_dir(dest.parent)
            for p in source.rglob('*'):
                target=dest/p.relative_to(source)
                if p.is_dir(): target.mkdir(parents=True,exist_ok=True); _fsync_dir(target.parent)
                else: self.write_file('/'+target.relative_to(self.root).as_posix(),p.read_text(encoding='utf-8'),if_absent=True); files+=1
        after={p for p in self.root.rglob('*') if p.is_dir()}
        return ImportResult(files,len(after-before),{'utf-8':files})

def _fsync_dir(path):
    fd=os.open(path,os.O_RDONLY | getattr(os,'O_DIRECTORY',0))
    try: os.fsync(fd)
    finally: os.close(fd)

def _read_exact(path):
    with path.open('r',encoding='utf-8',newline='') as handle: return handle.read()

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument('--root',required=True); sub=p.add_subparsers(dest='command',required=True)
    ls=sub.add_parser('ls'); ls.add_argument('path',nargs='?',default='/'); ls.add_argument('--recursive',action='store_true'); ls.add_argument('--limit',type=int,default=100); ls.add_argument('--offset',type=int,default=0)
    rd=sub.add_parser('read'); rd.add_argument('path'); rd.add_argument('--start-line',type=int,default=1); rd.add_argument('--lines',type=int)
    wr=sub.add_parser('write'); wr.add_argument('path'); wr.add_argument('--if-match'); wr.add_argument('--if-absent',action='store_true')
    ed=sub.add_parser('edit'); ed.add_argument('path'); ed.add_argument('--if-match')
    rm=sub.add_parser('rm'); rm.add_argument('path'); rm.add_argument('--recursive',action='store_true')
    imp=sub.add_parser('import'); imp.add_argument('source'); imp.add_argument('destination')
    a=p.parse_args(argv)
    with FilesystemStore(Path(a.root)) as s:
        if a.command=='ls': result=s.list_entries(a.path,a.recursive,a.limit,a.offset)
        elif a.command=='read': result=s.read_file(a.path,a.start_line,a.lines)
        elif a.command=='write': result=s.write_file(a.path,sys.stdin.read(),a.if_match,a.if_absent)
        elif a.command=='edit': result=s.edit_file(a.path,[Edit(**x) for x in json.load(sys.stdin)],a.if_match)
        elif a.command=='rm': s.remove_entry(a.path,a.recursive); result=None
        else: result=s.import_source(Path(a.source),a.destination)
        print(json.dumps({'api_version':1,'ok':True,'result':result},default=lambda o:o.__dict__,ensure_ascii=False)); return 0

if __name__ == '__main__':
    raise SystemExit(main())
