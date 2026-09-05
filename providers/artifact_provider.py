"""Resolve GitHub Actions artifacts for Endstone and Spark.

Discovery deliberately runs inside the GitHub Actions runner and uses GH_TOKEN.
No artifact name is hard-coded: artifacts are ranked for the current platform.
By default, development artifacts come from the configured branch. Release and
pre-merge validation can pin either component to an exact successful workflow
head SHA. Research validation can additionally constrain the Spark workflow and
artifact-name prefix so two different builds at one SHA cannot be confused.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
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
    component: str,
    platform_name: str,
    repo: str,
    run: dict[str, Any],
    artifact_name_prefix: str | None = None,
) -> dict[str, Any] | None:
    data = _get_json(f"/repos/{repo}/actions/runs/{run['id']}/artifacts?per_page=100")
    artifacts = data.get("artifacts") or []
    prefix = (artifact_name_prefix or "").strip().lower()
    if prefix:
        artifacts = [
            artifact
            for artifact in artifacts
            if str(artifact.get("name", "")).lower().startswith(prefix)
        ]
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
    expected_workflow: str | None = None,
    artifact_name_prefix: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    config = COMPONENTS[component]
    repo = config["repo"]
    branch = config["branch"]
    exact_sha = (expected_sha or "").strip()
    exact_workflow = (expected_workflow or "").strip()
    exact_artifact_prefix = (artifact_name_prefix or "").strip()

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
        if exact_workflow and str(run.get("name") or "") != exact_workflow:
            continue
        artifact = _select_from_run(
            component,
            platform_name,
            repo,
            run,
            artifact_name_prefix=exact_artifact_prefix or None,
        )
        if artifact is not None:
            return run, artifact

    constraints = []
    if exact_workflow:
        constraints.append(f"workflow={exact_workflow!r}")
    if exact_artifact_prefix:
        constraints.append(f"artifact_prefix={exact_artifact_prefix!r}")
    suffix = f" ({', '.join(constraints)})" if constraints else ""
    if exact_sha:
        raise ArtifactResolutionError(
            f"No successful {repo}@{exact_sha} run with a {platform_name} {component} artifact was found{suffix}"
        )
    raise ArtifactResolutionError(
        f"No successful {repo}@{branch} run with a {platform_name} {component} artifact was found{suffix}"
    )


def _download_artifact(repo: str, artifact: dict[str, Any], destination: pathlib.Path) -> pathlib.Path:
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / "artifact.zip"
    url = f"{API}/repos/{repo}/actions/artifacts/{artifact['id']}/zip"
    opener = urllib.request.build_opener(_SafeRedirect())
    try:
        with (
            opener.open(_request(url, accept="application/vnd.github+json"), timeout=120) as response,
            archive.open("wb") as out,
        ):
            shutil.copyfileobj(response, out, length=1024 * 1024)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise ArtifactResolutionError(
            f"Artifact download failed ({exc.code}) for {repo}/{artifact.get('name')}: {body[:600]}"
        ) from exc

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
    spark_workflow: str | None = None,
    spark_artifact_prefix: str | None = None,
) -> dict[str, Any]:
    if platform_name not in {"linux", "windows"}:
        raise ValueError(f"Unsupported platform: {platform_name}")

    exact_spark_sha = (spark_sha or os.environ.get("EXPECTED_SPARK_SHA", "")).strip() or None
    exact_endstone_sha = (endstone_sha or os.environ.get("EXPECTED_ENDSTONE_SHA", "")).strip() or None
    exact_spark_workflow = (
        spark_workflow or os.environ.get("EXPECTED_SPARK_WORKFLOW", "")
    ).strip() or None
    exact_spark_artifact_prefix = (
        spark_artifact_prefix or os.environ.get("EXPECTED_SPARK_ARTIFACT_PREFIX", "")
    ).strip() or None
    root = pathlib.Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"platform": platform_name, "components": {}}

    expected_shas = {
        "endstone": exact_endstone_sha,
        "spark": exact_spark_sha,
    }
    for component, config in COMPONENTS.items():
        discover_kwargs: dict[str, Any] = {"expected_sha": expected_shas[component]}
        if component == "spark" and exact_spark_workflow is not None:
            discover_kwargs["expected_workflow"] = exact_spark_workflow
        if component == "spark" and exact_spark_artifact_prefix is not None:
            discover_kwargs["artifact_name_prefix"] = exact_spark_artifact_prefix
        run, artifact = discover(component, platform_name, **discover_kwargs)
        info = _metadata(component, config["repo"], run, artifact)
        result["components"][component] = info
        save_metadata(result, metadata_path)
        payload = _download_artifact(config["repo"], artifact, root / component)
        info["payload_dir"] = str(payload)
        save_metadata(result, metadata_path)
        print(
            f"[artifact] {component}: {info['sha']} run={info['run_id']} "
            f"workflow={info['workflow']} artifact={info['artifact']['name']}"
        )

    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=["linux", "windows"])
    parser.add_argument("--output-dir", default="downloads")
    parser.add_argument("--spark-sha", default=None)
    parser.add_argument("--endstone-sha", default=None)
    parser.add_argument("--spark-workflow", default=None)
    parser.add_argument("--spark-artifact-prefix", default=None)
    args = parser.parse_args()
    print(
        json.dumps(
            resolve_artifacts(
                args.platform,
                args.output_dir,
                spark_sha=args.spark_sha,
                endstone_sha=args.endstone_sha,
                spark_workflow=args.spark_workflow,
                spark_artifact_prefix=args.spark_artifact_prefix,
            ),
            indent=2,
        )
    )