import json, sqlite3, subprocess, sys


def run(*args,input=None):
    return subprocess.run([sys.executable,'-m','memhub',*map(str,args)],input=input,capture_output=True)


def test_init_write_read_list_json_and_remove(tmp_path):
    v=tmp_path/'v.db'
    assert run('--vault',v,'init').returncode==0
    w=run('--vault',v,'--json','write','/d/x','--if-absent',input='hé\r\n'.encode())
    assert w.returncode==0; digest=json.loads(w.stdout)['result']['content_hash']
    r=run('--vault',v,'read','/d/x'); assert r.stdout=='hé\r\n'.encode()
    listing=run('--vault',v,'--json','ls','/d'); assert json.loads(listing.stdout)['result']['entries'][0]['path']=='/d/x'
    stale=run('--vault',v,'write','/d/x','--if-match','bad',input=b'x'); assert stale.returncode==4 and not stale.stdout
    edit=run('--vault',v,'--json','edit','/d/x','--if-match',digest,input=b'[{"old_text":"h\\u00e9","new_text":"yo"}]')
    assert edit.returncode==0
    assert run('--vault',v,'rm','/d','--recursive').returncode==0


def test_ranges_errors_invalid_stdin_and_import(tmp_path):
    v=tmp_path/'v'; run('--vault',v,'init')
    run('--vault',v,'write','/x',input=b'a\nb\n')
    assert run('--vault',v,'read','/x','--start-line','2','--lines','1').stdout==b'b\n'
    bad=run('--vault',v,'--json','read','relative'); assert bad.returncode==2 and json.loads(bad.stderr)['ok'] is False
    badutf=run('--vault',v,'write','/bad',input=b'\xff'); assert badutf.returncode==2
    src=tmp_path/'src'; src.write_text('hello')
    imported=run('--vault',v,'--json','import',src,'/imported')
    assert imported.returncode==0 and json.loads(imported.stdout)['result']['files']==1


def test_excluded_commands_and_missing_vault(tmp_path):
    for command in ('search','grep','mkdir','stat','find','mv'):
        assert run('--vault',tmp_path/'v',command).returncode==2
    assert run('--vault',tmp_path/'none','read','/x').returncode==3


def test_cli_statuses_diagnostics_and_regressions(tmp_path):
    vault=tmp_path/'v.db'; assert run('--vault',vault,'init').returncode==0
    conflict=run('--vault',vault,'--json','init')
    assert conflict.returncode==4 and not conflict.stdout and json.loads(conflict.stderr)['error']['code']=='vault_exists'
    malformed=run('--vault',vault,'--json','edit','/x',input=b'not-json')
    assert malformed.returncode==2 and b'Traceback' not in malformed.stderr
    target=tmp_path/'target'; target.write_text('x'); link=tmp_path/'link'; link.symlink_to(target)
    unsupported=run('--vault',vault,'--json','import',link,'/x')
    assert unsupported.returncode==5 and not unsupported.stdout
    bogus=tmp_path/'bogus'; bogus.write_text('not sqlite')
    schema=run('--vault',bogus,'--json','ls')
    assert schema.returncode==7 and b'Traceback' not in schema.stderr
    lock=sqlite3.connect(vault); lock.execute('BEGIN IMMEDIATE')
    try: busy=run('--vault',vault,'--json','write','/busy',input=b'x')
    finally: lock.rollback(); lock.close()
    assert busy.returncode==6 and json.loads(busy.stderr)['error']['code']=='database_busy'


def test_all_exit_statuses_are_subprocess_diagnostics(tmp_path):
    vault = tmp_path / 'v.db'
    assert run('--vault', vault, 'init').returncode == 0
    cases = [
        run('--vault', vault, '--json', 'ls', '--offset', str(10**100)),
        run('--vault', tmp_path / 'missing', '--json', 'read', '/x'),
        run('--vault', vault, '--json', 'write', '/x', '--if-match', 'stale', input=b'x'),
    ]
    fifo = tmp_path / 'fifo'; __import__('os').mkfifo(fifo)
    cases.append(run('--vault', vault, '--json', 'import', fifo, '/fifo'))
    lock = sqlite3.connect(vault); lock.execute('BEGIN IMMEDIATE')
    try:
        cases.append(run('--vault', vault, '--json', 'write', '/busy', input=b'x'))
    finally:
        lock.rollback(); lock.close()
    bogus = tmp_path / 'bogus'; bogus.write_text('not sqlite')
    cases.append(run('--vault', bogus, '--json', 'ls'))
    assert [case.returncode for case in cases] == [2, 3, 4, 5, 6, 7]
    for case in cases:
        assert not case.stdout and json.loads(case.stderr)['ok'] is False

    # Status 1 is reserved for genuinely unexpected failures; exercise the
    # adapter in a child process while replacing one operation with such a
    # failure, rather than adding a production crash switch.
    script = "import memhub.cli as c; c.list_entries=lambda *a,**k:(_ for _ in()).throw(RuntimeError('boom')); raise SystemExit(c.main(['--vault',%r,'--json','ls']))" % str(vault)
    internal = subprocess.run([sys.executable, '-c', script], capture_output=True)
    assert internal.returncode == 1 and not internal.stdout
    diagnostic = json.loads(internal.stderr)
    assert diagnostic['error']['code'] == 'internal_error' and b'Traceback' not in internal.stderr


def test_cli_import_detector_is_lazy():
    result=subprocess.run([sys.executable,'-c','import sys,memhub.cli; print("chardet" in sys.modules)'],capture_output=True,text=True)
    assert result.returncode==0 and result.stdout.strip()=='False'
