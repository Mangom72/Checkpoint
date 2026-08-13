import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from checkpoint.github_client import GitHubClient, GitHubError, NotFound
from tests.fake_github import FakeGitHub, Reply, Sequence


def client_for(server: FakeGitHub) -> GitHubClient:
    return GitHubClient("t0ken", api_url=server.url, graphql_url=f"{server.url}/graphql", max_retries=1)


def test_pagination_follows_link_header():
    items = [{"id": n} for n in range(5)]
    with FakeGitHub({"/things": items}, page_size=2) as server:
        got = client_for(server).paginate_list("/things")
    assert [row["id"] for row in got] == [0, 1, 2, 3, 4]


def test_pagination_respects_limit():
    items = [{"id": n} for n in range(10)]
    with FakeGitHub({"/things": items}, page_size=2) as server:
        got = client_for(server).paginate_list("/things", limit=3)
    assert len(got) == 3


def test_wrapped_collections_are_unwrapped():
    with FakeGitHub({"/actions/workflows": {"total_count": 1, "workflows": [{"id": 7}]}}) as server:
        got = client_for(server).paginate_list("/actions/workflows")
    assert got == [{"id": 7}]


def test_404_raises_notfound():
    with FakeGitHub({"/gone": Reply(404, {"message": "Not Found"})}) as server:
        with pytest.raises(NotFound):
            client_for(server).get_json("/gone")


def test_server_errors_are_retried_then_succeed():
    route = Sequence(Reply(502, {"message": "bad gateway"}), [{"id": 1}])
    with FakeGitHub({"/flaky": route}) as server:
        got = client_for(server).paginate_list("/flaky")
    assert got == [{"id": 1}]


def test_permission_error_raises_forbidden():
    from checkpoint.github_client import Forbidden

    body = {"message": "Must have admin rights to Repository."}
    with FakeGitHub({"/hooks": Reply(403, body)}) as server:
        with pytest.raises(Forbidden):
            client_for(server).get_json("/hooks")


def test_graphql_errors_surface():
    with FakeGitHub({"__graphql__": {"repository": None}}) as server:
        data = client_for(server).graphql("query{__typename}")
    assert data == {"repository": None}


def test_token_never_leaks_into_logs(caplog):
    from checkpoint.util import redact

    assert redact("using ghp_abcdefghijklmnopqrstuvwxyz012345") == "using ***"
    assert redact("secret=hunter2", ["hunter2"]) == "secret=***"
