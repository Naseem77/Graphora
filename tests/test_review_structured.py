from app.review import reviewer
from app.review.reviewer import FileNote, InlineComment, StructuredReview


def test_parse_review_extracts_structure():
    raw = """```json
{
  "summary": "Adds a cache to speed up lookups.",
  "walkthrough": [{"path": "app/cache.py", "summary": "New LRU cache"}],
  "comments": [
    {"path": "app/cache.py", "line": 12, "severity": "warning", "comment": "Unbounded cache can leak memory."}
  ]
}
```"""

    review = reviewer._parse_review(raw)

    assert review.summary == "Adds a cache to speed up lookups."
    assert review.walkthrough == [FileNote("app/cache.py", "New LRU cache")]
    assert review.comments == [InlineComment("app/cache.py", 12, "warning", "Unbounded cache can leak memory.")]


def test_parse_review_falls_back_to_plain_text():
    review = reviewer._parse_review("No issues found in this PR.")
    assert review.summary == "No issues found in this PR."
    assert review.walkthrough == []
    assert review.comments == []


def test_render_review_comment_includes_walkthrough_and_findings():
    review = StructuredReview(
        summary="Refactors the parser.",
        walkthrough=[FileNote("app/parser.py", "Split into helpers")],
        comments=[InlineComment("app/parser.py", 5, "critical", "Null deref on empty input")],
    )

    body = reviewer.render_review_comment(review)

    assert reviewer.REVIEW_HEADER in body
    assert "Refactors the parser." in body
    assert "| `app/parser.py` | Split into helpers |" in body
    assert "Critical" in body
    assert "`app/parser.py`:5" in body


def test_render_review_comment_no_issues():
    body = reviewer.render_review_comment(StructuredReview(summary="All good."))
    assert "_No blocking issues found._" in body
