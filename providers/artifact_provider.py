"""Resolve GitHub Actions artifacts for Endstone and Spark.

Discovery deliberately runs inside the GitHub Actions runner and uses GH_TOKEN.
No artifact name is hard-coded: artifacts are ranked for the current platform.
By default, development artifacts come from the configured branch. Release and
pre-merge validation can pin either component to an exact successful workflow
head SHA.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from typing import Any

API = "https://api.github.com"
COMPONENTS = {
    "endstone": {"repo": "EndstoneMC/endstone", "branch": "develop"},
    "spark": {"repo": "ReallocAll/spark", "branch": "develop"},
}


class ArtifactResolutionError(RuntimeError):
    pass


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Do not forward GitHub authorization to the signed artifact host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is None:
            return None
        old_host = urllib.parse.urlparse(req.full_url).netloc
        new_host = urllib.parse.urlparse(newurl).netloc
        if old_host != new_host:
            for key in list(new_req.headers):
                if key.lower() == "authorization":
                    del new_req.headers[key]
        return new_req


def _token() -> str:
    token = os.environ.get("GH_TOKEN", "").strip()
    if not token:
        raise ArtifactResolutionError("GH_TOKEN is not set; expected repository secret REPO_PAT")
    return token


def _request(url: str, *, accept: str = "application/vnd.github+json") -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "Authorization": f"Bearer {_token()}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "bds-test-lab",
        },
    )


def _get_json(path: str) -> dict[str, Any]:
    url = path if path.startswith("http") else f"{API}{path}"
    try:
        with urllib.request.urlopen(_request(url), timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise ArtifactResolutionError(f"GitHub API {exc.code} for {url}: {body[:600]}") from exc
    except urllib.error.URLError as exc:
        raise ArtifactResolutionError(f"GitHub API request failed for {url}: {exc}") from exc


def _artifact_score(component: str, platform_name: str, artifact: dict[str, Any]) -> int:
    name = str(artifact.get("name", "")).lower()
    if not name or bool(artifact.get("expired")):
        return -10_000

    score = 0
    if component in name:
        score += 30
    else:
        return -10_000

    if platform_name == "linux":
        if "linux" in name or "manylinux" in name:
            score += 40
        if "windows" in name or "win_amd64" in name:
            score -= 80
    elif platform_name == "windows":
        if "windows" in name or "win_amd64" in name:
            score += 40
        if "manylinux" in name or ("linux" in name and "windows" not in name):
            score -= 80
    else:
        return -10_000

    if component == "endstone":
        if name.endswith(".zip"):
            score += 30
        if "cp313" in name:
            score += 8
        if "x86_64" in name or "amd64" in name:
            score += 4

    if component == "spark" and platform_name in name:
        score += 15

    return score


def _select_from_run(
    component: str, platform_name: str, repo: str, run: dict[str, Any]
) -> dict[str, Any] | None:
    data = _get_json(f"/repos/{repo}/actions/runs/{run['id']}/artifacts?per_page=100")
    artifacts = data.get("artifacts") or []
    ranked = sorted(
        (
            (_artifact_score(component, platform_name, artifact), artifact)
            for artifact in artifacts
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    if not ranked or ranked[0][0] < 40:
        return None
    return ranked[0][1]


def discover(
    component: str,
    platform_name: str,
    expected_sha: str | None = None,
    expected_run_id: str | int | None = None,
    expected_artifact_id: str | int | None = None,
    expected_artifact_digest: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    config = COMPONENTS[component]
    repo = config["repo"]
    branch = config["branch"]
    exact_sha = (expected_sha or "").strip()

    if expected_run_id is not None:
        run = _get_json(f"/repos/{repo}/actions/runs/{expected_run_id}")
        if (
            str(run.get("id")) != str(expected_run_id)
            or run.get("conclusion") != "success"
            or (run.get("repository") or {}).get("full_name", "").lower() != repo.lower()
            or not exact_sha
            or run.get("head_sha") != exact_sha
        ):
            raise ArtifactResolutionError("Pinned workflow run does not match repository, success, or expected SHA")
        if expected_artifact_id is not None:
            artifact = _get_json(f"/repos/{repo}/actions/artifacts/{expected_artifact_id}")
            if (
                str(artifact.get("id")) != str(expected_artifact_id)
                or str((artifact.get("workflow_run") or {}).get("id")) != str(expected_run_id)
                or _artifact_score(component, platform_name, artifact) < 40
            ):
                raise ArtifactResolutionError("Pinned artifact does not match run or platform, or has expired")
        else:
            artifact = _select_from_run(component, platform_name, repo, run)
        if artifact is None:
            raise ArtifactResolutionError("Pinned run has no matching artifact")
        _verify_digest_metadata(artifact, expected_artifact_digest, required=True)
        return run, artifact
    if expected_artifact_id is not None or expected_artifact_digest:
        raise ArtifactResolutionError("Artifact pin requires an expected run ID")

    query_fields: dict[str, Any] = {"status": "success", "per_page": 100}
    if exact_sha:
        query_fields["head_sha"] = exact_sha
    else:
        query_fields["branch"] = branch
    query = urllib.parse.urlencode(query_fields)
    runs = _get_json(f"/repos/{repo}/actions/runs?{query}").get("workflow_runs") or []

    for run in runs:
        if run.get("conclusion") != "success":
            continue
        if exact_sha:
            if run.get("head_sha") != exact_sha:
                continue
        elif run.get("head_branch") != branch:
            continue
        artifact = _select_from_run(component, platform_name, repo, run)
        if artifact is not None:
            return run, artifact

    if exact_sha:
        raise ArtifactResolutionError(
            f"No successful {repo}@{exact_sha} run with a {platform_name} {component} artifact was found"
        )
    raise ArtifactResolutionError(
        f"No successful {repo}@{branch} run with a {platform_name} {component} artifact was found"
    )


def _digest(value: str) -> str:
    value = value.removeprefix("sha256:").lower()
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ArtifactResolutionError("Invalid SHA256 artifact digest")
    return value


def _verify_digest_metadata(artifact: dict[str, Any], expected: str | None, *, required: bool) -> None:
    official = artifact.get("digest")
    if not official:
        if required or expected:
            raise ArtifactResolutionError("Pinned artifact is missing its official digest")
        return
    official_hash = _digest(str(official))
    if expected and _digest(expected) != official_hash:
        raise ArtifactResolutionError("Expected artifact digest does not match official digest")


def _download_artifact(repo: str, artifact: dict[str, Any], destination: pathlib.Path) -> pathlib.Path:
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / "artifact.zip"
    url = f"{API}/repos/{repo}/actions/artifacts/{artifact['id']}/zip"
    opener = urllib.request.build_opener(_SafeRedirect())
    digest = hashlib.sha256()
    try:
        with (
            opener.open(_request(url, accept="application/vnd.github+json"), timeout=120) as response,
            archive.open("wb") as out,
        ):
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                out.write(chunk)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise ArtifactResolutionError(
            f"Artifact download failed ({exc.code}) for {repo}/{artifact.get('name')}: {body[:600]}"
        ) from exc

    artifact["downloaded_sha256"] = digest.hexdigest()
    if artifact.get("digest") and _digest(str(artifact["digest"])) != digest.hexdigest():
        raise ArtifactResolutionError("Downloaded artifact bytes do not match official digest")
    extract_dir = destination / "payload"
    extract_dir.mkdir(exist_ok=True)
    try:
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(extract_dir)
    except zipfile.BadZipFile as exc:
        raise ArtifactResolutionError(
            f"Downloaded artifact {artifact.get('name')} is not a ZIP archive"
        ) from exc
    archive.unlink(missing_ok=True)

    for nested in list(extract_dir.rglob("*.zip")):
        nested_dir = nested.with_suffix("")
        nested_dir.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(nested) as zf:
                zf.extractall(nested_dir)
        except zipfile.BadZipFile:
            continue

    return extract_dir


def _metadata(component: str, repo: str, run: dict[str, Any], artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "component": component,
        "repository": repo,
        "branch": run.get("head_branch"),
        "sha": run.get("head_sha"),
        "run_id": run.get("id"),
        "run_url": run.get("html_url"),
        "workflow": run.get("name"),
        "event": run.get("event"),
        "created_at": run.get("created_at"),
        "artifact": {
            "id": artifact.get("id"),
            "name": artifact.get("name"),
            "size_in_bytes": artifact.get("size_in_bytes"),
            "expires_at": artifact.get("expires_at"),
            "digest": artifact.get("digest"),
            "downloaded_sha256": artifact.get("downloaded_sha256"),
        },
    }


def save_metadata(data: dict[str, Any], path: pathlib.Path | str = "metadata.json") -> None:
    pathlib.Path(path).write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def resolve_artifacts(
    platform_name: str,
    output_dir: pathlib.Path | str = "downloads",
    metadata_path: pathlib.Path | str = "metadata.json",
    spark_sha: str | None = None,
    endstone_sha: str | None = None,
    spark_run_id: str | int | None = None,
    spark_artifact_id: str | int | None = None,
    spark_artifact_digest: str | None = None,
) -> dict[str, Any]:
    if platform_name not in {"linux", "windows"}:
        raise ValueError(f"Unsupported platform: {platform_name}")

    exact_spark_sha = (spark_sha or os.environ.get("EXPECTED_SPARK_SHA", "")).strip() or None
    exact_endstone_sha = (endstone_sha or os.environ.get("EXPECTED_ENDSTONE_SHA", "")).strip() or None
    pins = {
        "expected_run_id": spark_run_id if spark_run_id is not None else os.environ.get("EXPECTED_SPARK_RUN_ID") or None,
        "expected_artifact_id": spark_artifact_id if spark_artifact_id is not None else os.environ.get("EXPECTED_SPARK_ARTIFACT_ID") or None,
        "expected_artifact_digest": spark_artifact_digest if spark_artifact_digest is not None else os.environ.get("EXPECTED_SPARK_ARTIFACT_DIGEST") or None,
    }
    root = pathlib.Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"platform": platform_name, "components": {}}

    expected_shas = {
        "endstone": exact_endstone_sha,
        "spark": exact_spark_sha,
    }
    for component, config in COMPONENTS.items():
        run, artifact = discover(
            component,
            platform_name,
            expected_sha=expected_shas[component],
            **({key: value for key, value in pins.items() if value is not None} if component == "spark" else {}),
        )
        info = _metadata(component, config["repo"], run, artifact)
        result["components"][component] = info
        save_metadata(result, metadata_path)
        payload = _download_artifact(config["repo"], artifact, root / component)
        info["artifact"]["downloaded_sha256"] = artifact.get("downloaded_sha256")
        info["payload_dir"] = str(payload)
        save_metadata(result, metadata_path)
        print(
            f"[artifact] {component}: {info['sha']} run={info['run_id']} "
            f"artifact={info['artifact']['name']}"
        )

    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=["linux", "windows"])
    parser.add_argument("--output-dir", default="downloads")
    parser.add_argument("--spark-sha", default=None)
    parser.add_argument("--endstone-sha", default=None)
    args = parser.parse_args()
    print(
        json.dumps(
            resolve_artifacts(
                args.platform,
                args.output_dir,
                spark_sha=args.spark_sha,
                endstone_sha=args.endstone_sha,
            ),
            indent=2,
        )
    )
