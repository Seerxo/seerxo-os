#!/usr/bin/env python3
"""Seed a fresh Seerxo OS database with a small, fictional example company.

It lets a new installation show something in the Founder Desk and gives the agents a goal-bearing
project to reason about. Everything here is a placeholder: edit it, or replace it with your own
organisation. Idempotent: re-running updates rows by key rather than duplicating them.

    docker exec company-api python -m scripts.seed
"""
from datetime import datetime, timezone

from sqlalchemy import select

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


def run() -> None:
    db = SessionLocal()
    try:
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
    finally:
        db.close()


if __name__ == "__main__":
    run()
