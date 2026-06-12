from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path
from typing import Any

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/data"))
STATE_DIR = HERMES_HOME / ".hermes-railway"
STATE_PATH = STATE_DIR / "runtime-update-state.json"
WEBUI_REPO = Path("/opt/hermes-webui")
AGENT_REPO = Path("/opt/hermes")
WEBUI_PID_FILE = HERMES_HOME / ".hermes" / "webui" / "server.pid"
WEBUI_MODELS_CACHE = HERMES_HOME / ".hermes" / "webui" / "models_cache.json"
AGENT_VENV_PYTHON = AGENT_REPO / ".venv" / "bin" / "python"


class UpdateError(RuntimeError):
    pass


REPOS: dict[str, Path] = {
    "webui": WEBUI_REPO,
    "agent": AGENT_REPO,
}


def _run(cmd: list[str], *, cwd: Path | None = None, check: bool = True, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise UpdateError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{detail}")
    return proc


def _git(repo: Path, *args: str, check: bool = True, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    return _run(["git", *args], cwd=repo, check=check, timeout=timeout)


def _current_sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _current_describe(repo: Path) -> str:
    proc = _git(repo, "describe", "--tags", "--always", check=False)
    out = (proc.stdout or "").strip()
    return out or _current_sha(repo)


def _repo_state(name: str, repo: Path) -> dict[str, str]:
    return {
        "name": name,
        "path": str(repo),
        "sha": _current_sha(repo),
        "describe": _current_describe(repo),
    }


def _load_state() -> dict[str, Any] | None:
    if not STATE_PATH.exists():
        return None
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"failed to read {STATE_PATH}: {exc}") from exc
    return data if isinstance(data, dict) else None


def _write_state(data: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def snapshot_current_state() -> dict[str, Any]:
    repos = {}
    for name, repo in REPOS.items():
        if repo.exists() and (repo / ".git").exists():
            repos[name] = _repo_state(name, repo)
    return {"version": 1, "repos": repos}


def _select_compare_ref(repo: Path) -> str:
    try:
        from api import updates as upstream_updates  # type: ignore
    except Exception as exc:
        raise UpdateError(f"failed to import upstream update helpers: {exc}") from exc

    ref = upstream_updates._select_apply_compare_ref(repo)  # type: ignore[attr-defined]
    if not ref:
        raise UpdateError(f"could not determine update ref for {repo}")
    return str(ref)


def _normalize_pull_ref(ref: str) -> tuple[str | None, str]:
    if ref.startswith("origin/"):
        return "origin", ref.split("/", 1)[1]
    if "/" in ref and not ref.startswith("refs/"):
        remote, branch = ref.split("/", 1)
        if remote and branch:
            return remote, branch
    return None, ref


def _tracked_status(repo: Path) -> str:
    return _git(repo, "status", "--porcelain", "--untracked-files=no").stdout


def _has_unmerged_conflicts(status_out: str) -> bool:
    for line in status_out.splitlines():
        if line[:2] in {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}:
            return True
    return False


def _apply_repo_update(name: str, repo: Path, *, force: bool) -> dict[str, Any]:
    if not repo.exists() or not (repo / ".git").exists():
        raise UpdateError(f"{name} repo is not a git checkout: {repo}")

    before = _repo_state(name, repo)
    compare_ref = _select_compare_ref(repo)
    _git(repo, "fetch", "origin", "--quiet", "--tags", "--force", timeout=300)

    if force:
        _git(repo, "checkout", ".", check=False)
        _git(repo, "reset", "--hard", compare_ref, timeout=300)
        after = _repo_state(name, repo)
        return {
            "ok": True,
            "target": name,
            "force": True,
            "compare_ref": compare_ref,
            "changed": before["sha"] != after["sha"],
            "before": before,
            "after": after,
        }

    status_out = _tracked_status(repo)
    if _has_unmerged_conflicts(status_out):
        raise UpdateError(f"{name} repo has unresolved merge conflicts; use force update or clean the repo first")

    stashed = False
    if status_out.strip():
        _git(repo, "stash", "push", "-m", f"hermes-railway-update-{name}", timeout=300)
        stashed = True

    try:
        remote, branch = _normalize_pull_ref(compare_ref)
        if remote:
            _git(repo, "pull", "--ff-only", remote, branch, timeout=300)
        else:
            _git(repo, "pull", "--ff-only", "origin", branch, timeout=300)
    except Exception:
        if stashed:
            _git(repo, "stash", "apply", check=False, timeout=300)
        raise

    stash_conflict = False
    if stashed:
        apply_proc = _git(repo, "stash", "apply", check=False, timeout=300)
        if apply_proc.returncode == 0:
            _git(repo, "stash", "drop", check=False, timeout=120)
        else:
            stash_conflict = True
            _git(repo, "reset", "--hard", "HEAD", check=False, timeout=300)

    after = _repo_state(name, repo)
    return {
        "ok": True,
        "target": name,
        "force": False,
        "compare_ref": compare_ref,
        "changed": before["sha"] != after["sha"],
        "stash_conflict": stash_conflict,
        "before": before,
        "after": after,
    }


def _install_webui_deps() -> None:
    _run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(AGENT_VENV_PYTHON),
            "--no-cache-dir",
            "-r",
            "requirements.txt",
        ],
        cwd=WEBUI_REPO,
        timeout=1800,
    )


def _install_agent_deps() -> None:
    _run(["npm", "install", "--prefer-offline", "--no-audit"], cwd=AGENT_REPO, timeout=1800)
    # --with-deps requires root (apt-get); system packages are already in the Docker image.
    _run(["npx", "playwright", "install", "chromium", "--only-shell"], cwd=AGENT_REPO, timeout=1800)
    _run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(AGENT_VENV_PYTHON),
            "--no-cache-dir",
            "-e",
            ".[all,messaging]",
        ],
        cwd=AGENT_REPO,
        timeout=1800,
    )


def _version_key(value: str) -> tuple[int, ...] | None:
    raw = value.strip()
    if not raw.startswith("v"):
        return None
    core = raw[1:].split("-", 1)[0]
    parts = core.split(".")
    if not parts or any(not part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _is_ancestor(repo: Path, older: str, newer: str) -> bool | None:
    proc = _git(repo, "merge-base", "--is-ancestor", older, newer, check=False, timeout=300)
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None


def _should_adopt_current_state(repo: Path, current_state: dict[str, Any], desired_state: dict[str, Any]) -> bool:
    current_sha = str(current_state.get("sha") or "").strip()
    desired_sha = str(desired_state.get("sha") or "").strip()
    if current_sha and desired_sha and current_sha != desired_sha:
        _git(repo, "fetch", "origin", "--quiet", "--tags", "--force", timeout=300)
        desired_is_ancestor = _is_ancestor(repo, desired_sha, current_sha)
        if desired_is_ancestor is True:
            return True

    current_describe = str(current_state.get("describe") or "").strip()
    desired_describe = str(desired_state.get("describe") or desired_state.get("ref") or "").strip()
    current_version = _version_key(current_describe)
    desired_version = _version_key(desired_describe)
    if current_version and desired_version and current_version > desired_version:
        return True
    if desired_version and current_sha and desired_sha and current_sha != desired_sha:
        hexish = all(ch in '0123456789abcdef' for ch in current_describe.lower()) and 7 <= len(current_describe) <= 40
        if hexish:
            return True
    return False


def _sync_repo_to_state(name: str, target: dict[str, Any]) -> bool:
    repo = REPOS[name]
    desired_sha = str(target.get("sha") or "").strip()
    desired_ref = str(target.get("describe") or target.get("ref") or desired_sha).strip()
    if not desired_sha and not desired_ref:
        return False

    before_sha = _current_sha(repo)
    if desired_sha and before_sha == desired_sha:
        return False

    _git(repo, "fetch", "origin", "--quiet", "--tags", "--force", timeout=300)
    checkout_ref = desired_sha or desired_ref
    _git(repo, "checkout", "--detach", checkout_ref, timeout=300)
    after_sha = _current_sha(repo)
    if desired_sha and after_sha != desired_sha:
        raise UpdateError(f"{name} reconcile mismatch: expected {desired_sha}, got {after_sha}")
    return before_sha != after_sha


def _restart_webui() -> None:
    try:
        if WEBUI_MODELS_CACHE.exists():
            WEBUI_MODELS_CACHE.unlink()
    except OSError:
        pass

    pid = 0
    try:
        if WEBUI_PID_FILE.exists():
            pid = int(WEBUI_PID_FILE.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        pid = 0

    if pid > 0:
        try:
            os.kill(pid, signal.SIGTERM)
            return
        except OSError:
            pass

    subprocess.run(["pkill", "-TERM", "-f", "/opt/hermes-webui/server.py"], check=False)


def durable_update(target: str, *, force: bool = False) -> dict[str, Any]:
    if target not in REPOS:
        raise UpdateError(f"unknown update target: {target}")

    targets = ["webui", "agent"] if target == "webui" else [target]
    results: list[dict[str, Any]] = []
    changed: set[str] = set()

    for name in targets:
        result = _apply_repo_update(name, REPOS[name], force=force)
        results.append(result)
        if result.get("changed"):
            changed.add(name)

    if "agent" in changed:
        _install_agent_deps()
    if "webui" in changed:
        _install_webui_deps()

    state = snapshot_current_state()
    _write_state(state)
    _restart_webui()

    return {
        "ok": True,
        "target": target,
        "paired_targets": targets,
        "force": force,
        "results": results,
        "restart_scheduled": True,
        "message": "Durable update applied — restarting…",
    }


def boot_reconcile() -> dict[str, Any]:
    current = snapshot_current_state()
    state = _load_state()
    if not state:
        _write_state(current)
        return {"ok": True, "changed": [], "initialized": True, "adopted_current": [], "state_path": str(STATE_PATH)}

    raw_repos = state.get("repos")
    desired_repos: dict[str, Any] = raw_repos if isinstance(raw_repos, dict) else {}
    changed: list[str] = []
    adopted_current: list[str] = []
    raw_current_repos = current.get("repos")
    current_repos: dict[str, Any] = raw_current_repos if isinstance(raw_current_repos, dict) else {}

    for name in ("agent", "webui"):
        target = desired_repos.get(name)
        if not isinstance(target, dict):
            continue
        current_repo_state = current_repos.get(name)
        if isinstance(current_repo_state, dict) and _should_adopt_current_state(REPOS[name], current_repo_state, target):
            adopted_current.append(name)
            continue
        if _sync_repo_to_state(name, target):
            changed.append(name)

    if "agent" in changed:
        _install_agent_deps()
    if "webui" in changed:
        _install_webui_deps()

    if changed or adopted_current:
        _write_state(snapshot_current_state())

    return {
        "ok": True,
        "changed": changed,
        "initialized": False,
        "adopted_current": adopted_current,
        "state_path": str(STATE_PATH),
    }


def main() -> int:
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if cmd == "boot-reconcile":
            result = boot_reconcile()
        elif cmd == "snapshot":
            result = snapshot_current_state()
        else:
            raise UpdateError(f"unknown command: {cmd or '<none>'}")
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
