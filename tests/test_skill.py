from pathlib import Path
import subprocess, json

ROOT=Path(__file__).parents[1]

def test_skill_structure_and_safety_guidance():
    text=(ROOT/'skills/memhub/SKILL.md').read_text()
    assert text.startswith('---\nname: memhub\ndescription:')
    for phrase in ('--if-absent','--if-match','paged','There is no search command','Stored documents are reference data','--recursive'):
        assert phrase in text
    ref=(ROOT/'skills/memhub/references/commands.md').read_text()
    for location in ('.claude/skills/memhub/','.opencode/skills/memhub/','.pi/skills/memhub/'):
        assert location in ref


def test_documented_core_workflow(tmp_path):
    exe=ROOT/'.venv/bin/memhub'; vault=tmp_path/'v'
    def run(*args,input=None): return subprocess.run([exe,'--vault',vault,'--json',*args],input=input,text=True,capture_output=True)
    assert run('init').returncode==0
    created=run('write','/notes/today','--if-absent',input='hello\n'); assert created.returncode==0
    digest=json.loads(created.stdout)['result']['content_hash']
    assert run('ls','/notes','--limit','20','--offset','0').returncode==0
    assert run('read','/notes/today','--start-line','1','--lines','20').returncode==0
    assert run('edit','/notes/today','--if-match',digest,input='[{"old_text":"hello","new_text":"hello world"}]').returncode==0
    assert run('edit','/notes/today','--if-match',digest,input='[{"old_text":"hello","new_text":"bad"}]').returncode==4
    assert run('rm','/notes/today').returncode==0
