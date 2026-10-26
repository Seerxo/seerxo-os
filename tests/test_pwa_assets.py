"""PWA files are fixed, uncached assets behind the existing Access boundary."""
import struct
import re

from fastapi.testclient import TestClient

from app import api_auth, cloudflare_access, main


def test_install_assets_have_correct_types_and_no_sensitive_cache():
    client = TestClient(main.app, client=("172.18.0.5", 50000))
    shell = client.get("/")
    assert shell.status_code == 200
    manifest_link = re.search(r'<link\b(?=[^>]*\brel="manifest")(?=[^>]*\bcrossorigin="use-credentials")[^>]*>', shell.text, re.I)
    assert manifest_link, "the PWA manifest must send credentials through the Cloudflare Access redirect"

    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    assert manifest.headers["cache-control"] == "no-store"
    assert manifest.json()["id"] == "/"
    assert manifest.json()["scope"] == "/"
    assert manifest.json()["display"] == "standalone"

    worker = client.get("/sw.js")
    assert worker.status_code == 200
    assert worker.headers["content-type"].startswith("text/javascript")
    assert worker.headers["service-worker-allowed"] == "/"
    assert worker.headers["cache-control"] == "no-store"

    for name, size in (("sez-192.png", 192), ("sez-512.png", 512), ("apple-touch-icon.png", 180)):
        icon = client.get(f"/icons/{name}")
        assert icon.status_code == 200 and icon.headers["content-type"].startswith("image/png")
        assert icon.headers["cache-control"] == "no-store"
        assert icon.content[:8] == b"\x89PNG\r\n\x1a\n"
        assert struct.unpack(">II", icon.content[16:24]) == (size, size)

    assert client.get("/icons/other.png").status_code == 401
    assert client.get("/icons/../desk.html").status_code != 200
    assert client.get("/desk/state").status_code == 401
    assert api_auth.allowed("anonymous", "POST", "/sw.js") is False


def test_install_assets_still_require_access_on_the_desk_host(monkeypatch):
    monkeypatch.setenv("CF_ACCESS_DESK_HOST", "desk.example.invalid")

    async def verified(assertion):
        return "founder@example.invalid" if assertion == "founder" else None

    monkeypatch.setattr(cloudflare_access, "verify_founder_identity", verified)
    client = TestClient(main.app, base_url="https://desk.example.invalid", client=("127.0.0.1", 1234))
    for path in ("/", "/manifest.webmanifest", "/sw.js", "/icons/sez-192.png"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Cf-Access-Jwt-Assertion": "founder"}).status_code == 200
