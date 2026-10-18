from app.tools.retrieval import clear_retrieval_cache, retrieve_policy_snippets


def _fillers(tmp_path, count=4):
    # BM25's IDF goes to zero when a term is in every document. Keep a few
    # unrelated docs so a real match scores above zero.
    for index in range(count):
        (tmp_path / f"filler-{index}.md").write_text(f"unrelated office note {index}", encoding="utf-8")


def test_edited_policy_is_visible_on_the_next_retrieval(tmp_path, monkeypatch):
    _fillers(tmp_path)
    doc = tmp_path / "leave.md"
    doc.write_text("alphaunique parental leave rule", encoding="utf-8")
    monkeypatch.setenv("DOCS_DIR", str(tmp_path))
    clear_retrieval_cache()

    first = retrieve_policy_snippets("alphaunique")
    assert any("alphaunique" in snippet for snippet in first)

    doc.write_text("betaunique parental leave rule", encoding="utf-8")
    second = retrieve_policy_snippets("betaunique")
    assert any("betaunique" in snippet for snippet in second)
    assert not any("alphaunique" in snippet for snippet in second)


def test_pto_query_does_not_pull_unrelated_policies():
    clear_retrieval_cache()
    snippets = retrieve_policy_snippets(
        "Asked how PTO carryover works going into next year. Has about 30 unused hours."
    )
    names = [snippet.split("\n", 1)[0] for snippet in snippets]
    assert "pto-policy" in names
    assert "harassment-reporting-policy" not in names
    assert "code-of-conduct" not in names


def test_equal_scores_tie_break_on_filename(tmp_path, monkeypatch):
    _fillers(tmp_path)
    (tmp_path / "b-policy.md").write_text("shared token zeta", encoding="utf-8")
    (tmp_path / "a-policy.md").write_text("shared token zeta", encoding="utf-8")
    monkeypatch.setenv("DOCS_DIR", str(tmp_path))
    clear_retrieval_cache()

    snippets = retrieve_policy_snippets("zeta", top_k=2)
    assert [snippet.split("\n", 1)[0] for snippet in snippets] == ["a-policy", "b-policy"]
