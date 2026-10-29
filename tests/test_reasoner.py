"""Stage B CLIs run in company-reasoner; the worker sends bounded JSON and holds no login."""
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import yaml

from app import providers, reasoner
from app.providers import Health, ProviderResult, RemoteProvider

SCHEMA = {"type": "object"}


class Fake(providers.Provider):
    def __init__(self):
        super().__init__(name="claude", binary="claude", environ={})
        self.calls = []

    def health_check(self):
        return Health.AVAILABLE, None

    def version(self):
        return "2.1.286 (Claude Code)"

    def _run(self, prompt, schema, *, cwd, timeout_s, max_turns):
        self.calls.append((prompt, cwd, timeout_s, max_turns))
        return ProviderResult("claude", Health.AVAILABLE, output={"verdict": "ok"}, model="sonnet",
                              input_tokens=10, output_tokens=2)


def _serve(local):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), reasoner.handler(local))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_a_remote_run_returns_the_same_result_the_local_cli_would():
    fake = Fake()
    srv, url = _serve({"claude": fake})
    try:
        remote = RemoteProvider("claude", url, environ={})
        assert remote.health_check() == (Health.AVAILABLE, None)
        assert remote.version() == "2.1.286 (Claude Code)"
        res = remote.run("envelope", SCHEMA, cwd="/worker/scratch", timeout_s=600, max_turns=6)
        assert res.ok and res.output == {"verdict": "ok"} and res.input_tokens == 10
        prompt, cwd, timeout_s, turns = fake.calls[0]
        # Break caught: the worker's own path is used on the reasoner, which cannot see it.
        assert prompt == "envelope" and cwd != "/worker/scratch" and (timeout_s, turns) == (600, 6)
        assert RemoteProvider("codex", url, environ={}).health_check()[0] == Health.DISABLED
    finally:
        srv.shutdown()


def test_an_unreachable_reasoner_is_capacity_not_a_task_failure():
    remote = RemoteProvider("claude", "http://127.0.0.1:9", environ={})
    assert remote.health_check()[0] == Health.BROKEN
    res = remote._run("p", SCHEMA, cwd="/", timeout_s=5, max_turns=1)
    # BROKEN is in FALLBACK_HEALTH: the worker tries the next provider or waits.
    assert res.health == Health.BROKEN and res.health in providers.FALLBACK_HEALTH


def test_metered_credentials_are_refused_before_any_request_leaves():
    remote = RemoteProvider("claude", "http://127.0.0.1:9", environ={"ANTHROPIC_API_KEY": "x"})
    assert remote.health_check()[0] == Health.METERED_BLOCKED


def test_requests_are_bounded():
    local = {"claude": Fake()}
    ok = {"provider": "claude", "prompt": "p", "schema": SCHEMA, "timeout_s": 600, "max_turns": 6}
    assert reasoner.run(local, ok)[0] == 200
    for bad in ({"provider": "bash"}, {"timeout_s": 10**6}, {"max_turns": 0}, {"schema": "x"}):
        assert reasoner.run(local, ok | bad)[0] == 400, bad


def test_default_providers_go_remote_only_when_configured():
    assert all(isinstance(p, RemoteProvider)
               for p in providers.default_providers({"REASONER_URL": "http://r:8100"}).values())
    assert not any(isinstance(p, RemoteProvider) for p in providers.default_providers({}).values())


def test_compose_keeps_control_plane_secrets_and_logins_apart():
    services = yaml.safe_load((Path(__file__).resolve().parent.parent / "docker-compose.yml")
                              .read_text())["services"]
    worker, rsn = services["company-worker"], services["company-reasoner"]
    # Break caught: a model CLI shares a container or network with the database and tokens.
    assert set(rsn["environment"]).isdisjoint(
        {"DATABASE_URL", "GH_TOKEN", "TELEGRAM_BOT_TOKEN", "POSTGRES_PASSWORD"})
    assert rsn["networks"] == ["reasoner-net", "review-net"] and "ports" not in rsn
    assert not any("docker.sock" in v for v in rsn.get("volumes", []))
    assert {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_2"}.isdisjoint(worker["environment"])
    assert not any(".codex" in v or ".claude" in v for v in worker.get("volumes", []))
    assert worker["environment"]["REASONER_URL"] == "http://company-reasoner:8100"
    assert rsn["read_only"] and rsn["cap_drop"] == ["ALL"]


def test_compose_reasoner_egress_is_the_allowlist_proxy_only():
    root = Path(__file__).resolve().parent.parent
    cfg = yaml.safe_load((root / "docker-compose.yml").read_text())
    rsn, egress = cfg["services"]["company-reasoner"], cfg["services"]["company-reasoner-egress"]
    # Break caught: provider logins in a container that can reach any host on the internet.
    assert cfg["networks"]["reasoner-net"]["internal"] is True
    assert rsn["environment"]["HTTPS_PROXY"] == "http://company-reasoner-egress:8888"
    assert egress["build"]["target"] == "egress" and set(egress["networks"]) == {"reasoner-net", "reasoner-egress"}
    assert "HTTPS_PROXY" in providers.PASS_ENV  # the CLIs must see the proxy
    allow = (root / "scripts" / "coder" / "allowlist").read_text().split()
    assert allow == [r"^api\.anthropic\.com$", r"^chatgpt\.com$", r"^auth\.openai\.com$"]


def test_the_reasoner_image_carries_only_stdlib_modules_it_copies():
    import subprocess
    import sys
    root = Path(__file__).resolve().parent.parent
    # Break caught: reasoner.py starts importing app code or packages its image does not contain.
    probe = ("import sys, app.reasoner; "
             "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
             "{'httpx','sqlalchemy','fastapi','pydantic','yaml','psycopg'} or "
             "(m.startswith('app.') and m not in ('app.reasoner','app.providers','app.liveness'))); print(bad)")
    out = subprocess.run([sys.executable, "-c", probe], cwd=root, capture_output=True, text=True)
    assert out.stdout.strip() == "[]", out.stdout + out.stderr
    docker = (root / "Dockerfile").read_text()
    stage = docker[docker.index("AS reasoner"):docker.index("AS egress")]
    assert "COPY app/__init__.py app/liveness.py app/providers.py app/reasoner.py ./app/" in stage
    assert "pip install" not in stage and " gh" not in stage and " git" not in stage
    worker = docker[docker.index("AS worker"):docker.index("AS coder")]
    # Break caught: a model CLI back in the container that holds DATABASE_URL and GH_TOKEN.
    assert "claude-code" not in worker and "@openai/codex" not in worker
    rsn = yaml.safe_load((root / "docker-compose.yml").read_text())["services"]["company-reasoner"]
    assert rsn["build"]["target"] == "reasoner"
    assert rsn["image"] == "${COMPANY_OS_IMAGE_REASONER:?bound image ID required}"


def test_release_components_require_bound_ids_and_auxiliary_images_keep_the_release_tag():
    services = yaml.safe_load((Path(__file__).resolve().parent.parent / "docker-compose.yml")
                              .read_text())["services"]
    bound = {"company-api": "API", "company-worker": "WORKER", "company-reasoner": "REASONER",
             "company-reasoner-egress": "EGRESS"}
    for service, component in bound.items():
        assert services[service]["image"] == "${COMPANY_OS_IMAGE_" + component + ":?bound image ID required}"
    assert services["company-worker"]["environment"]["WORKER_IMAGE"] == "${COMPANY_OS_IMAGE_WORKER}"
    auxiliary = {n: s["image"] for n, s in services.items() if s.get("image", "").startswith("company-os-")}
    assert set(auxiliary) == {"company-egress", "company-review-signer"}
    assert all(i.endswith(":${COMPANY_OS_IMAGE_TAG:-latest}") for i in auxiliary.values()), auxiliary
