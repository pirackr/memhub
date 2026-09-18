from pathlib import Path
import pytest
from memhub import create_vault, open_vault, import_source, read_file, list_entries
from memhub.errors import Conflict, Unsupported


def vault(tmp_path):
    p=tmp_path/'v.db'; create_vault(p); return p


def test_file_exact_and_summary(tmp_path):
    vp=vault(tmp_path); src=tmp_path/'x'; src.write_bytes('café'.encode())
    with open_vault(vp) as v:
        result=import_source(v,src,'/docs/x')
        assert (result.files,result.encoding_counts)==(1,{'utf-8':1})
        assert read_file(v,'/docs/x').content=='café'


def test_directory_merge_empty_and_rollback(tmp_path):
    vp=vault(tmp_path); src=tmp_path/'src'; src.mkdir(); (src/'empty').mkdir(); (src/'a').write_text('a')
    with open_vault(vp) as v:
        r=import_source(v,src,'/dest')
        assert r.files==1 and r.directories==2
        assert [e.path for e in list_entries(v,'/dest',recursive=True).entries]==['/dest/a','/dest/empty']
        with pytest.raises(Conflict): import_source(v,src,'/dest')
        assert read_file(v,'/dest/a').content=='a'


def test_failure_rolls_back(tmp_path):
    vp=vault(tmp_path); src=tmp_path/'src'; src.mkdir(); (src/'a').write_text('a'); (src/'bad').write_bytes(b'\x00')
    with open_vault(vp) as v:
        with pytest.raises(Unsupported): import_source(v,src,'/dest')
        assert list_entries(v).entries==[]


def test_low_confidence_warning_after_success(tmp_path, monkeypatch):
    import memhub.importer as imp
    from memhub.models import DecodedText
    vp=vault(tmp_path); src=tmp_path/'x'; src.write_bytes(b'x'); seen=[]
    monkeypatch.setattr(imp,'decode_bytes',lambda b: DecodedText('x','latin-1',.2))
    with open_vault(vp) as v:
        r=import_source(v,src,'/x',seen.append)
    assert r.encoding_counts=={'latin-1':1} and seen[0].confidence==.2


def test_warning_spill_replays_after_commit_and_discards_on_rollback(tmp_path, monkeypatch):
    import memhub.importer as imp
    from memhub.models import DecodedText
    vp = vault(tmp_path)
    src = tmp_path / 'many'; src.mkdir()
    for index in range(800):
        (src / (f'{index:04d}-' + ('x' * 80))).write_bytes(b'x')
    monkeypatch.setattr(imp, 'decode_bytes', lambda b: DecodedText('x', 'latin-1', .2))
    seen = []
    with open_vault(vp) as v:
        result = import_source(v, src, '/ok', seen.append)
        assert result.files == 800 and len(seen) == 800

    failed = tmp_path / 'failed'; failed.mkdir()
    (failed / 'a').write_bytes(b'x')
    (failed / 'z').write_bytes(b'bad')
    def decode(data):
        if data == b'bad':
            raise Unsupported('unsupported_encoding', 'forced failure')
        return DecodedText('x', 'latin-1', .2)
    monkeypatch.setattr(imp, 'decode_bytes', decode)
    seen.clear()
    with open_vault(vp) as v:
        with pytest.raises(Unsupported):
            import_source(v, failed, '/rolled-back', seen.append)
        assert seen == []
        assert all(entry.path != '/rolled-back/a' for entry in list_entries(v, '/', recursive=True).entries)


def test_active_vault_blocked(tmp_path):
    vp=vault(tmp_path)
    with open_vault(vp) as v:
        with pytest.raises(Unsupported): import_source(v,vp,'/copy')
