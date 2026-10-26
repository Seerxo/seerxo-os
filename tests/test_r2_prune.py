"""Off-site retention (TASK-0022): prune keeps the newest copies and never empties a prefix."""
from datetime import datetime, timedelta, timezone

from scripts import r2


class FakeS3:
    def __init__(self, keys):
        base = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.objs = {k: {"Key": k, "Size": 100, "LastModified": base + timedelta(days=i)}
                     for i, k in enumerate(keys)}
        self.deleted = []

    def get_paginator(self, _):
        objs = self.objs

        class P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [o for k, o in objs.items() if k.startswith(Prefix)]}
        return P()

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)
        del self.objs[Key]


def test_keeps_newest_and_only_touches_its_prefix():
    keys = [f"cc/volumes/daily/{i}.tar.gz" for i in range(10)] + ["cc/volumes/weekly/w.tar.gz",
                                                                  "cc/company-db/daily/d.dump"]
    s3 = FakeS3(keys)
    gone = r2.prune(s3, "b", "cc/volumes/daily/", 7)
    assert gone == [f"cc/volumes/daily/{i}.tar.gz" for i in (2, 1, 0)]  # the three oldest
    assert "cc/volumes/weekly/w.tar.gz" in s3.objs and "cc/company-db/daily/d.dump" in s3.objs


def test_never_deletes_the_last_copy():
    s3 = FakeS3(["cc/renders/weekly/a.tar.gz", "cc/renders/weekly/b.tar.gz"])
    # Break caught: keep=0 from a typo in the environment wiping the archive.
    r2.prune(s3, "b", "cc/renders/weekly/", 0)
    assert list(s3.objs) == ["cc/renders/weekly/b.tar.gz"]


def test_usage_sums_sizes():
    assert r2.usage(FakeS3(["cc/a", "cc/b", "other/c"]), "b", "cc/") == 200
