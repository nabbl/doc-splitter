import pytest

from doc_splitter.config import Config
from doc_splitter.ledger import Ledger


@pytest.fixture
def config(tmp_path):
    roots = {}
    for name in (
        "inbox",
        "consume",
        "staging",
        "archive",
        "review",
        "work",
        "state",
        "model_cache",
    ):
        roots[name] = tmp_path / name
        roots[name].mkdir()
    return Config(**roots, settle_seconds=0, ocr_languages="eng", render_dpi=72)


@pytest.fixture
def ledger(config):
    value = Ledger(config.state / "jobs.sqlite3")
    yield value
    value.close()
