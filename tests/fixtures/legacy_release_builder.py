# Exact build_release source from public 16083a7. The complete module SHA256 matches the installed legacy host:
# 563261ede57df78b2a5c834dc6233ff71a2274898c3988cc40f2eeab64ec02a3
# Test-only compatibility fixture; never installed on production.
def build_release(cfg: Config, sha: str, directory: Path) -> None:
    """The staged transport's three files, from the host clone at exactly `sha` (same commands as the driver)."""
    try:
        tar = rg.release_archive(cfg.clone, sha)      # checked in host_facts already; again here, after pending
    except rg.Refused as error:
        raise RuntimeError(str(error)) from None      # after `pending`: APPLY_FAILED, never a silent reject
    listing = rg.payload_listing(cfg.clone, sha)
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(tar)
    (directory / "src.tgz").write_bytes(buf.getvalue())
    (directory / "main.sha256").write_text(listing)
    driver = _git(cfg, "show", f"{sha}:scripts/deploy-company-os.sh")
    if REMOTE_START not in driver or REMOTE_END not in driver:
        raise rg.Refused("the deploy driver at this commit has no root-side script")
    remote = driver[driver.index(REMOTE_START) + len(REMOTE_START):driver.index(REMOTE_END)]
    (directory / "company-os-deploy.sh").write_text(remote + "\n")
    for name in ("src.tgz", "main.sha256", "company-os-deploy.sh"):
        os.chmod(directory / name, 0o600)
