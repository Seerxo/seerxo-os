"""scripts/seed.py never touches a database that already has rows, and never runs without --fresh-install."""
from scripts import seed


class _Db:
    def __init__(self, count):
        self.count, self.closed, self.added = count, False, []

    def execute(self, _query):
        return type("R", (), {"scalar_one": lambda _self: self.count})()

    def add(self, obj):
        self.added.append(obj)

    def close(self):
        self.closed = True


def test_seed_refuses_without_the_flag(monkeypatch):
    monkeypatch.setattr(seed, "SessionLocal", lambda: (_ for _ in ()).throw(AssertionError("no db access")))
    assert seed.run([]) == 2 and seed.run(["--force"]) == 2


def test_seed_refuses_a_database_with_rows(monkeypatch):
    db = _Db(count=1)
    monkeypatch.setattr(seed, "SessionLocal", lambda: db)
    assert seed.run(["--fresh-install"]) == 1 and db.added == [] and db.closed
