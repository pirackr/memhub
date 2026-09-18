"""Command-line adapter for the public memhub library."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path

from .documents import edit_file, read_file, write_file
from .edits import parse_edits
from .errors import InvalidInput, MemhubError
from .importer import import_source
from .tree import list_entries, remove_entry
from .vault import audit_vault, create_vault, open_vault

API_VERSION = 1

class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise InvalidInput("invalid_arguments", message)


def _parser():
    p=Parser(prog="memhub")
    p.add_argument("--vault", required=True)
    p.add_argument("--json", action="store_true")
    p.add_argument("--verify", action="store_true", help="run a full integrity audit before the command")
    sub=p.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    ls=sub.add_parser("ls"); ls.add_argument("path",nargs="?",default="/"); ls.add_argument("--recursive",action="store_true"); ls.add_argument("--limit",type=int,default=100); ls.add_argument("--offset",type=int,default=0)
    read=sub.add_parser("read"); read.add_argument("path"); read.add_argument("--start-line",type=int,default=1); read.add_argument("--lines",type=int)
    write=sub.add_parser("write"); write.add_argument("path"); write.add_argument("--if-match"); write.add_argument("--if-absent",action="store_true")
    edit=sub.add_parser("edit"); edit.add_argument("path"); edit.add_argument("--if-match")
    rm=sub.add_parser("rm"); rm.add_argument("path"); rm.add_argument("--recursive",action="store_true")
    imp=sub.add_parser("import"); imp.add_argument("source"); imp.add_argument("destination")
    return p


def _obj(value):
    return asdict(value) if is_dataclass(value) else value


def _emit_success(value, json_mode):
    payload=_obj(value)
    if json_mode:
        print(json.dumps({"api_version":API_VERSION,"ok":True,"result":payload},ensure_ascii=False))
    elif value is not None:
        if hasattr(value,"entries"):
            for entry in value.entries: print(f"{entry.kind}\t{entry.path}")
        else: print(json.dumps(payload,ensure_ascii=False))


def _diagnostic(error, json_mode):
    body={"code":error.code,"message":error.message}
    if json_mode:
        print(json.dumps({"api_version":API_VERSION,"ok":False,"error":body},ensure_ascii=False),file=sys.stderr)
    else: print(f"memhub: {error.code}: {error.message}",file=sys.stderr)


def _stdin_utf8():
    try: return sys.stdin.buffer.read().decode("utf-8",errors="strict")
    except UnicodeDecodeError as exc: raise InvalidInput("invalid_utf8","stdin must be strict UTF-8") from exc


def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    json_requested="--json" in argv
    try:
        args=_parser().parse_args(argv)
        if args.command == 'ls' and (args.limit > 2**63 - 1 or args.offset > 2**63 - 1):
            raise InvalidInput('invalid_pagination', 'limit and offset exceed the supported range')
        if args.command=="init":
            create_vault(Path(args.vault))
            if args.verify:
                audit_vault(Path(args.vault))
            result=None
        else:
            with open_vault(Path(args.vault), audit=args.verify) as vault:
                if args.command=="ls": result=list_entries(vault,args.path,args.recursive,args.limit,args.offset)
                elif args.command=="read":
                    result=read_file(vault,args.path,args.start_line,args.lines)
                    if not args.json:
                        sys.stdout.write(result.content); return 0
                elif args.command=="write": result=write_file(vault,args.path,_stdin_utf8(),args.if_match,args.if_absent)
                elif args.command=="edit":
                    raw=_stdin_utf8()
                    try: payload=json.loads(raw)
                    except json.JSONDecodeError as exc: raise InvalidInput("invalid_json",f"invalid edit JSON: {exc.msg}") from exc
                    result=edit_file(vault,args.path,parse_edits(payload),args.if_match)
                elif args.command=="rm": remove_entry(vault,args.path,args.recursive); result=None
                elif args.command=="import":
                    def warning(w):
                        body={"api_version":API_VERSION,"ok":True,"warning":_obj(w)} if args.json else None
                        print(json.dumps(body,ensure_ascii=False) if body else f"memhub: warning: {w.message} [{w.source_path}]",file=sys.stderr)
                    result=import_source(vault,Path(args.source),args.destination,warning)
        _emit_success(result,args.json)
        return 0
    except MemhubError as exc:
        _diagnostic(exc,json_requested)
        return exc.exit_status
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception:
        error=MemhubError("internal_error","unexpected internal failure")
        _diagnostic(error,json_requested)
        return 1
