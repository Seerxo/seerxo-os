#!/usr/bin/env python3
"""Seed a fresh Seerxo OS database with a small, fictional example company.

It lets a new installation show something in the Founder Desk and gives the agents a goal-bearing
project to reason about. Everything here is a placeholder: edit it, or replace it with your own
organisation. It runs only with an explicit --fresh-install flag and only on an empty database: on a
database that already holds business units, projects or services it changes nothing (it would otherwise
overwrite real rows that share a key, such as company-os).

    docker exec company-api python -m scripts.seed --fresh-install
"""
import sys
from datetime import datetime, timezone

from sqlalchemy import func, select

from app import enums, models
from app.db import SessionLocal

SEEDED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


def upsert(db, model, key, **fields):
    obj = db.execute(select(model).where(model.key == key)).scalar_one_or_none()
    if obj is None:
        obj = model(key=key, **fields)
        db.add(obj)
    else:
        for k, v in fields.items():
            setattr(obj, k, v)
    db.flush()
    return obj


def existing_rows(db) -> int:
    return sum(db.execute(select(func.count()).select_from(m)).scalar_one()
               for m in (models.BusinessUnit, models.Project, models.Service))


def run(argv: list[str]) -> int:
    if argv != ["--fresh-install"]:
        print("refused: seed only a fresh install, with --fresh-install", file=sys.stderr)
        return 2
    db = SessionLocal()
    try:
        if existing_rows(db):
            print("refused: the database already has business units, projects or services; nothing changed",
                  file=sys.stderr)
            return 1
        # ---------------------------------------------------------- business units
        saas = upsert(db, models.BusinessUnit, "demo-saas",
            name="Demo SaaS",
            description="A fictional subscription product used to demonstrate goals and metrics.",
            status=enums.BusinessUnitStatus.ACTIVE, priority=enums.Priority.P1,
            revenue_model="subscription (example)")
        upsert(db, models.BusinessUnit, "demo-content",
            name="Demo content channel",
            description="A fictional content channel that is not started yet.",
            status=enums.BusinessUnitStatus.BACKLOG, priority=enums.Priority.P3)

        # ---------------------------------------------------------- projects
        upsert(db, models.Project, "demo-api",
            business_unit_id=saas.id, name="Demo API",
            status=enums.ProjectStatus.ACTIVE, priority=enums.Priority.P1,
            repository="example-org/demo-api",
            objective="A stable, measurable service.",
            current_phase="example")
        upsert(db, models.Project, "company-os",
            name="Company OS", status=enums.ProjectStatus.ACTIVE, priority=enums.Priority.P1,
            repository="example-org/company-os",
            objective="Company state survives an LLM or session restart.",
            current_phase="example")

        # ---------------------------------------------------------- services
        def svc(key, **kw):
            kw.setdefault("server", "example-host")
            kw.setdefault("last_verified", SEEDED_AT)
            return upsert(db, models.Service, key, **kw)

        svc("demo-api", name="Demo API", purpose="Example HTTP service", business_unit_id=saas.id,
            health_url="https://api.example.invalid/health")
        svc("company-api", name="Company OS API", purpose="Company state, tasks, events")

        db.commit()
        print("seeded the example organisation")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
