from nova_api.memory_store import MemoryStore


def _embed(text: str):
    text = text.lower()
    # Deterministic fake semantic space for testing plumbing, not production semantics.
    return [
        1.0 if any(word in text for word in ("commit", "validation")) else 0.0,
        1.0 if any(word in text for word in ("train", "rail", "transport")) else 0.0,
        1.0 if any(word in text for word in ("erreur", "echec", "failure")) else 0.0,
    ]


def test_semantic_retrieval_complements_lexical_rules(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3", embedder=_embed)
    remembered = store.remember(memory_type="DECISION", source_type="user", provenance="USER_STATED",
        subject="Validation du projet", content="Ne jamais faire de commit automatique.", importance=8)
    results = store.search("Quelle règle de validation avons-nous choisie ?")
    assert results[0].item.memory_id == remembered.memory_id
    assert "semantic match" in results[0].reasons


def test_memory_without_embedder_preserves_lexical_behavior(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    item = store.remember(memory_type="FACT", source_type="user", provenance="USER_STATED",
        subject="provider principal", content="Groq est disponible")
    results = store.search("provider disponible")
    assert results and results[0].item.memory_id == item.memory_id
    assert "semantic match" not in results[0].reasons


def test_memory_v2_recall_benchmark_reports_topk(tmp_path):
    from nova_api.memory_benchmark import MemoryRecallCase, evaluate_recall

    store = MemoryStore(tmp_path / "memory.sqlite3", embedder=_embed)
    commit = store.remember(memory_type="DECISION", source_type="user", provenance="USER_STATED",
        subject="Validation", content="Ne jamais faire de commit automatique.", importance=8)
    train = store.remember(memory_type="FACT", source_type="user", provenance="USER_STATED",
        subject="Transport", content="Le train est le moyen de transport principal.", importance=5)
    report = evaluate_recall(store, [
        MemoryRecallCase("Quelle règle de validation avons-nous choisie ?", commit.memory_id),
        MemoryRecallCase("Quel moyen de transport utilise-t-on ?", train.memory_id),
    ], top_k=2)
    assert report.total == 2
    assert report.topk_hits == 2
    assert report.topk_rate == 1.0
    assert 0.0 <= report.top1_rate <= 1.0
