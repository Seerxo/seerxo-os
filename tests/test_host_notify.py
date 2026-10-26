"""The host-level alert path works without the Company API and never echoes secrets."""
import os
from pathlib import Path
import subprocess


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "notify.sh"
TOKEN = "123456789:FAKE_TEST_TOKEN_not_a_real_credential"


def _send(tmp_path, body, code="200", env_file=None, recipients="111"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text("""#!/usr/bin/env bash
printf '%s\\n' "$@" > "$CURL_ARGS_FILE"
if [[ ${TELEGRAM_BOT_TOKEN+x} ]]; then
  echo present > "$CURL_TOKEN_ENV_FILE"
else
  echo absent > "$CURL_TOKEN_ENV_FILE"
fi
printf '%s' "${HTTPS_PROXY:-absent}" > "$CURL_PROXY_ENV_FILE"
cat > "$CURL_CONFIG_FILE"
printf '%s\\n%s' "$FAKE_BODY" "$FAKE_CODE"
""")
    curl.chmod(0o755)
    env = {**os.environ,
           "PATH": f"{bin_dir}:{os.environ['PATH']}",
           "ENV_FILE": str(env_file or tmp_path / "absent.env"),
           "TELEGRAM_BOT_TOKEN": TOKEN,
           "TELEGRAM_ADMIN_CHAT_IDS": recipients,
           "FAKE_BODY": body,
           "FAKE_CODE": code,
           "CURL_ARGS_FILE": str(tmp_path / "curl-args"),
           "CURL_TOKEN_ENV_FILE": str(tmp_path / "curl-token-env"),
           "CURL_PROXY_ENV_FILE": str(tmp_path / "curl-proxy-env"),
           "CURL_CONFIG_FILE": str(tmp_path / "curl-config")}
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy",
                "NO_PROXY", "no_proxy", "CURL_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        env.pop(key, None)
    if env_file:
        env.pop("TELEGRAM_BOT_TOKEN")
        env.pop("TELEGRAM_ADMIN_CHAT_IDS")
    return subprocess.run(["bash", str(SCRIPT), "API down", "check the host"],
                          env=env, capture_output=True, text=True, timeout=10)


def test_host_notifier_accepts_telegram_confirmation_without_api(tmp_path):
    result = _send(tmp_path, '{"ok": true, "result": {}}')
    assert result.returncode == 0


def test_host_notifier_rejects_failed_delivery_without_secret_output(tmp_path):
    result = _send(tmp_path, '{"ok": false, "description": "' + TOKEN + '"}')
    assert result.returncode == 1
    assert "recipient 1: HTTP 200" in result.stderr
    assert TOKEN not in result.stderr and "111" not in result.stderr


def test_host_notifier_rejects_empty_normalized_recipient_list_without_reporting_success(tmp_path):
    result = _send(tmp_path, '{"ok": true, "result": {}}', recipients=" , , ")

    assert result.returncode == 10
    assert "no valid Telegram recipients" in result.stderr
    assert not (tmp_path / "curl-args").exists()


def test_host_notifier_keeps_token_out_of_curl_argv_and_environment(tmp_path):
    result = _send(tmp_path, '{"ok": true, "result": {}}')
    assert result.returncode == 0
    assert TOKEN not in (tmp_path / "curl-args").read_text()
    assert (tmp_path / "curl-token-env").read_text().strip() == "absent"
    config = (tmp_path / "curl-config").read_text()
    assert config.startswith('url = "https://api.telegram.org/bot')
    assert config.endswith('/sendMessage"\n')


def test_host_notifier_loads_dotenv_without_exporting_credentials(tmp_path):
    env_file = tmp_path / "company.env"
    env_file.write_text(
        f'TELEGRAM_BOT_TOKEN="{TOKEN}"\nTELEGRAM_ADMIN_CHAT_IDS="111"\n'
        'HTTPS_PROXY="http://proxy.example:8080"\n')
    result = _send(tmp_path, '{"ok": true, "result": {}}', env_file=env_file)
    assert result.returncode == 0
    assert (tmp_path / "curl-token-env").read_text().strip() == "absent"
    assert (tmp_path / "curl-proxy-env").read_text() == "http://proxy.example:8080"
    assert (tmp_path / "curl-config").read_text().startswith(
        'url = "https://api.telegram.org/bot')
