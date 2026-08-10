"""Embedder 并发安全测试：首次加载只加载一份模型（防双份 ~2.3GB 打满 4C8G）。"""
import threading

import numpy as np

from app.embed import Embedder


def test_concurrent_first_encode_loads_model_once(monkeypatch):
    """并发首次 encode：模型只加载一次（_load 双检锁）。"""
    calls = {"n": 0}
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    class FakeST:
        def __init__(self, name):
            with lock:
                calls["n"] += 1

        def encode(self, texts, **kwargs):
            return np.zeros((len(texts), 8), dtype=np.float32)

    monkeypatch.setattr("sentence_transformers.SentenceTransformer", FakeST)
    embedder = Embedder()
    results = []

    def worker():
        barrier.wait()
        results.append(embedder.encode(["并发首载测试"]))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert calls["n"] == 1  # 4 个线程并发首载只加载一份
    assert len(results) == 4  # 全部正常完成
