import os
from pathlib import Path
import pytest
from memhub.errors import Unsupported, VaultFailure
from memhub.sources import iter_sources


def test_file_and_tree_incremental(tmp_path):
    root=tmp_path/'src'; root.mkdir(); (root/'empty').mkdir(); (root/'nested').mkdir()
    (root/'nested'/'a.txt').write_bytes(b'a')
    items=list(iter_sources(root))
    assert [(i.relative_path,i.kind,i.data) for i in items] == [('', 'directory', None), ('empty','directory',None), ('nested','directory',None), ('nested/a.txt','file',b'a')]


def test_single_file(tmp_path):
    p=tmp_path/'x'; p.write_bytes(b'x')
    item=list(iter_sources(p))[0]
    assert item.relative_path=='' and item.data==b'x'


def test_symlink_and_fifo_rejected(tmp_path):
    target=tmp_path/'target'; target.write_text('x')
    link=tmp_path/'link'; link.symlink_to(target)
    with pytest.raises(Unsupported): list(iter_sources(link))
    realdir=tmp_path/'real'; realdir.mkdir(); (realdir/'x').write_text('x')
    alias=tmp_path/'alias'; alias.symlink_to(realdir, target_is_directory=True)
    with pytest.raises(Unsupported): list(iter_sources(alias/'x'))
    root=tmp_path/'root'; root.mkdir(); (root/'l').symlink_to(target)
    with pytest.raises(Unsupported): list(iter_sources(root))
    if hasattr(os, 'mkfifo'):
        fifo=tmp_path/'fifo'; os.mkfifo(fifo)
        with pytest.raises(Unsupported): list(iter_sources(fifo))


def test_ancestor_replacement_cannot_redirect_open(tmp_path, monkeypatch):
    parent=tmp_path/'parent'; parent.mkdir(); source_dir=parent/'source'; source_dir.mkdir()
    (source_dir/'doc').write_bytes(b'safe')
    evil=tmp_path/'evil'; evil.mkdir(); (evil/'doc').write_bytes(b'evil')
    original=os.open; swapped=False
    def racing_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path=='doc' and kwargs.get('dir_fd') is not None and not swapped:
            swapped=True
            source_dir.rename(parent/'detached')
            source_dir.symlink_to(evil, target_is_directory=True)
        return original(path,flags,*args,**kwargs)
    monkeypatch.setattr(os,'open',racing_open)
    items=list(iter_sources(source_dir))
    assert swapped and items[1].data==b'safe'


def test_blocked_inode(tmp_path):
    p=tmp_path/'vault'; p.write_bytes(b'x')
    with pytest.raises(Unsupported): list(iter_sources(p,{p}))


def test_missing_source(tmp_path):
    with pytest.raises(VaultFailure): list(iter_sources(tmp_path/'missing'))
