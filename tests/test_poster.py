from app.github.poster import post_or_update_pr_comment


class FakeComment:
    def __init__(self, body):
        self.body = body
        self.edited = None

    def edit(self, body):
        self.edited = body
        self.body = body


class FakeIssue:
    def __init__(self, comments=None):
        self.comments = comments or []
        self.created = []

    def get_comments(self):
        return self.comments

    def create_comment(self, body):
        self.created.append(body)


class FakeRepo:
    def __init__(self, issue):
        self.issue = issue

    def get_issue(self, number):
        assert number == 7
        return self.issue


class FakeClient:
    def __init__(self, issue):
        self.issue = issue

    def get_repo(self, full_name):
        assert full_name == "octo/repo"
        return FakeRepo(self.issue)


def test_post_or_update_pr_comment_updates_existing_marker():
    comment = FakeComment("<!-- marker -->\nold")
    issue = FakeIssue([comment])

    post_or_update_pr_comment(FakeClient(issue), "octo", "repo", 7, "<!-- marker -->", "new")

    assert comment.edited == "<!-- marker -->\nnew"
    assert issue.created == []


def test_post_or_update_pr_comment_creates_when_marker_missing():
    issue = FakeIssue()

    post_or_update_pr_comment(FakeClient(issue), "octo", "repo", 7, "<!-- marker -->", "new")

    assert issue.created == ["<!-- marker -->\nnew"]
