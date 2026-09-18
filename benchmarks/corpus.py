"""Deterministic incremental UTF-8 benchmark corpus generation."""
from dataclasses import dataclass
from pathlib import Path
import hashlib, random

SMALL=(1_000,10_000_000)
SCALE=(100_000,1_000_000_000)

@dataclass(frozen=True)
class CorpusManifest:
    seed:int
    document_count:int
    input_bytes:int
    documents:dict[str,str]


def generate_corpus(root:Path,count:int,target_bytes:int,seed:int)->CorpusManifest:
    if count<1 or target_bytes<count: raise ValueError('count and byte target must be positive and target >= count')
    root=Path(root); root.mkdir(parents=True,exist_ok=True)
    rng=random.Random(seed); documents={}; total=0
    for i in range(count):
        # Wide and nested paths, plus Unicode names.
        rel=(f"wide/{i%257:03d}/doc-{i:06d}.txt" if i%3 else f"deep/{i%17:02d}/层/{i//17:05d}/dóc-{i:06d}.txt")
        path=root/rel; path.parent.mkdir(parents=True,exist_ok=True)
        remaining=target_bytes-total; left=count-i
        base=remaining//left
        jitter=0 if left==1 else rng.randint(-min(base//3,1024),min(base//3,1024))
        size=max(1,base+jitter)
        prefix=f"document {i} seed {seed} café λ\n".encode()
        pattern=f"line {rng.randrange(1_000_000):06d} lorem ipsum\n".encode()
        data=(prefix+(pattern*((max(0,size-len(prefix))+len(pattern)-1)//len(pattern))))[:size]
        # slicing UTF-8 can split prefix only for tiny files; fall back to ASCII padding.
        try: data.decode('utf-8')
        except UnicodeDecodeError: data=(b'x'*size)
        path.write_bytes(data); total+=len(data)
        documents[rel]=hashlib.sha256(data).hexdigest()
    return CorpusManifest(seed,count,total,documents)
