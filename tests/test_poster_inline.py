from app.github import poster


class FakeCommit:
    sha = "headsha"


class FakeCommits:
    def __init__(self):
        self.reversed = [FakeCommit()]


class FakePull:
    def __init__(self):
        self.created = []
        self.raise_on_line = None

    def get_commits(self):
        return FakeCommits()

    def create_review_comment(self, body, commit, path, line, side):
        if line == self.raise_on_line:
            raise ValueError("line not in diff")
        self.created.append((path, line, body, side))


class FakeRepo:
    def __init__(self, pull):
        self._pull = pull

    def get_pull(self, number):
        return self._pull


class FakeClient:
    def __init__(self, pull):
        self._repo = FakeRepo(pull)

    def get_repo(self, full_name):
        return self._repo


def test_post_inline_review_comments_posts_each():
    pull = FakePull()
    client = FakeClient(pull)

    posted = poster.post_inline_review_comments(
        client,
        "octo",
        "repo",
        7,
        [
            {"path": "a.py", "line": 3, "body": "issue A"},
            {"path": "b.py", "line": 9, "body": "issue B"},
        ],
    )

    assert posted == 2
    assert pull.created[0] == ("a.py", 3, "issue A", "RIGHT")


def test_post_inline_review_comments_skips_failures():
    pull = FakePull()
    pull.raise_on_line = 3
    client = FakeClient(pull)

    posted = poster.post_inline_review_comments(
        client,
        "octo",
        "repo",
        7,
        [
            {"path": "a.py", "line": 3, "body": "bad line"},
            {"path": "b.py", "line": 9, "body": "good line"},
        ],
    )

    assert posted == 1
    assert pull.created == [("b.py", 9, "good line", "RIGHT")]


def test_post_inline_review_comments_ignores_missing_line():
    pull = FakePull()
    client = FakeClient(pull)

    posted = poster.post_inline_review_comments(
        client, "octo", "repo", 7, [{"path": "a.py", "line": None, "body": "x"}]
    )

    assert posted == 0
    assert pull.created == []
