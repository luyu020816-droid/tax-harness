"""CPU native model objects must stay on one inference thread across callers."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, get_ident
from types import SimpleNamespace

import pytest

from app.retrieval_engine import LocalRetrieval


def test_model_execution_stays_on_one_thread_across_background_callers():
    torch = pytest.importorskip("torch")
    threads = []

    class Tokenizer:
        def encode(self, *args, **kwargs):
            return [1, 2]

        def __call__(self, texts, **kwargs):
            return {"size": len(texts)}

    class Engine(LocalRetrieval):
        def _tokenizer(self, kind):
            return Tokenizer()

        def _load(self, kind):
            threads.append(get_ident())

            def model(size):
                threads.append(get_ident())
                return SimpleNamespace(
                    last_hidden_state=torch.ones(size, 1, 1024), logits=torch.ones(size, 1)
                )

            return model

    engine = Engine("unused-by-protocol-test", isolated=False)
    barrier = Barrier(4)

    def caller(index):
        barrier.wait()
        return engine.embed(["协议样例"]) if index % 2 else engine.rerank("问题", ["协议样例"])

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(caller, range(4)))
    assert len(results) == 4
    assert len(set(threads)) == 1, "Native model load/inference moved between request threads"
