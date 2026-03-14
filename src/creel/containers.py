"""Container management — Docker image building, caching, and executor containers.

Extracted from orchestrator.py to keep container/Docker concerns separate from
the core task execution and inline executor logic.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import threading
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from creel.models import BridgeConfig, ExecutorConfig, ToolConfig
from creel.secrets import decrypt_env_file

if TYPE_CHECKING:
    from creel.models import AgentDefinition

logger = logging.getLogger(__name__)

_HASH_GLOBS = ("Dockerfile", "**/*.py", "**/*.txt")

_BASE_IMAGE_NAME = "creel-executor-base"
_BASE_DOCKERFILE = Path("src/executors/base/Dockerfile")

# Default GHCR registry prefix for pre-built executor images
GHCR_PREFIX = "ghcr.io/creel-ai"


class ImageBuildCache:
    """Coordinates Docker image builds, deduplicating concurrent requests.

    The first thread to request a given image "claims" the build; other
    threads wait on a ``threading.Event`` until the build finishes.  On
    failure the error is stored but cleared on the next access so that
    retries are possible (handles transient Docker daemon failures).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # key -> (event, result_or_none, error_or_none)
        self._builds: dict[str, tuple[threading.Event, str | None, Exception | None]] = {}

    def ensure_image(self, image: str) -> str:
        """Build (or wait for) a Docker image.  Returns the usable image ref."""
        key = self._cache_key(image)
        event: threading.Event | None = None
        claimed = False

        with self._lock:
            if key in self._builds:
                ev, result, error = self._builds[key]
                if ev.is_set():
                    if error is None:
                        return result  # type: ignore[return-value]
                    # Previous build failed — clear so we can retry
                    del self._builds[key]
                else:
                    # Another thread is building; we'll wait outside the lock
                    event = ev

            if event is None and key not in self._builds:
                # Claim the build
                event = threading.Event()
                self._builds[key] = (event, None, None)
                claimed = True

        if claimed:
            assert event is not None
            try:
                result = _ensure_image_uncached(image)
                with self._lock:
                    self._builds[key] = (event, result, None)
                event.set()
                return result
            except Exception as exc:
                with self._lock:
                    self._builds[key] = (event, None, exc)
                event.set()
                raise

        # Wait for another thread's in-progress build
        assert event is not None
        event.wait()
        with self._lock:
            entry = self._builds.get(key)
            if entry is None or entry[0] is not event:
                # Our build was superseded by a retry; re-enter from the top
                superseded = True
            else:
                superseded = False
                _, result, error = entry
        if superseded:
            return self.ensure_image(image)
        if error is not None:
            raise RuntimeError(str(error)) from error
        return result  # type: ignore[return-value]

    def start_prebuild(self, images: list[str]) -> list[threading.Thread]:
        """Spawn daemon threads to pre-build each image."""
        threads = []
        for img in images:
            t = threading.Thread(
                target=self._prebuild_one,
                args=(img,),
                daemon=True,
                name=f"prebuild-{img}",
            )
            t.start()
            threads.append(t)
        return threads

    def _prebuild_one(self, image: str) -> None:
        try:
            self.ensure_image(image)
        except Exception:
            logger.warning("Pre-build failed for %s (will retry on demand)", image, exc_info=True)

    def clear(self) -> None:
        """Reset the cache (primarily for testing)."""
        with self._lock:
            self._builds.clear()

    @staticmethod
    def _cache_key(image: str) -> str:
        """Derive the cache key.

        Executor images share a key by base name (``executor-weather``)
        since the content-hash tag is computed inside the build.  Other
        images use their full name.
        """
        base = image.split(":")[0]
        if base.startswith("executor-"):
            # Content hash is computed inside the build, so different tags
            # (e.g. :latest vs :abc123) map to the same build work.
            return base
        return image


_image_cache = ImageBuildCache()


class _HostAuthEntry(TypedDict):
    host_path: str
    container_path: str


_HOST_AUTH_REGISTRY: dict[str, _HostAuthEntry] = {
    "github": {
        "host_path": "~/.config/gh",
        "container_path": "/home/executor/.config/gh",
    },
}


def _compute_base_image_hash() -> str:
    """Hash the base Dockerfile contents for cache-busting.

    Returns the first 12 hex chars of the SHA-256 digest.
    """
    h = sha256()
    if _BASE_DOCKERFILE.exists():
        h.update(_BASE_DOCKERFILE.read_bytes())
    return h.hexdigest()[:12]


def _ensure_base_image() -> str:
    """Build the shared executor base image if it doesn't already exist.

    Returns the tagged image reference (e.g. ``creel-executor-base:abc123``).
    """
    content_hash = _compute_base_image_hash()
    tagged = f"{_BASE_IMAGE_NAME}:{content_hash}"

    inspect = subprocess.run(
        ["docker", "image", "inspect", tagged],
        capture_output=True,
    )
    if inspect.returncode == 0:
        return tagged

    if not _BASE_DOCKERFILE.exists():
        raise FileNotFoundError(f"Base Dockerfile not found at {_BASE_DOCKERFILE}")

    _build_image(
        tags=[tagged, f"{_BASE_IMAGE_NAME}:latest"],
        dockerfile=_BASE_DOCKERFILE,
        context=_BASE_DOCKERFILE.parent,
    )
    return tagged


def _compute_executor_hash(executor_dir: Path) -> str:
    """Hash all source files in an executor directory and shared context files.

    Returns the first 12 hex chars of the SHA-256 digest computed over
    sorted (relative-path, file-contents) pairs.  Shared files in the
    parent build-context directory (e.g. ``google_creds.py``) are also
    included so that changes to shared modules trigger a rebuild.
    The base Dockerfile is included so base image changes trigger child rebuilds.
    """
    h = sha256()
    # Include base Dockerfile so changes to it invalidate executor caches
    if _BASE_DOCKERFILE.exists():
        h.update(b"base:")
        h.update(_BASE_DOCKERFILE.read_bytes())
    # Executor-specific files
    paths = sorted(p for pattern in _HASH_GLOBS for p in executor_dir.glob(pattern) if p.is_file())
    # Shared files in the build context (src/executors/)
    context_dir = executor_dir.parent
    shared = sorted(p for pattern in _HASH_GLOBS for p in context_dir.glob(pattern) if p.is_file())
    for p in paths:
        h.update(p.relative_to(executor_dir).as_posix().encode())
        h.update(p.read_bytes())
    for p in shared:
        h.update(("../" + p.relative_to(context_dir).as_posix()).encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:12]


def _ensure_image(image: str) -> str:
    """Build the Docker image if needed, with build deduplication.

    Delegates to :class:`ImageBuildCache` so concurrent and repeated
    calls only trigger a single ``docker build``.
    """
    return _image_cache.ensure_image(image)


def _is_remote_image(image: str) -> bool:
    """Return True if *image* looks like a remote registry reference.

    Heuristic: contains a ``/`` (e.g. ``ghcr.io/org/name:tag``).
    Local images like ``executor-weather:latest`` never contain a slash.
    """
    return "/" in image.split(":")[0]


def _pull_image(image: str, *, timeout: int = 300) -> str:
    """Pull a remote image.  Returns the image reference on success.

    Args:
        image: Full image reference (e.g. ``ghcr.io/org/name:tag``).
        timeout: Maximum seconds to wait for the pull (default 300).
    """
    logger.info("Pulling image %s", image)
    try:
        result = subprocess.run(
            ["docker", "pull", image],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Timed out pulling image {image} after {timeout}s") from exc
    if result.returncode != 0:
        err = result.stderr.strip() if result.stderr else "unknown error"
        raise RuntimeError(f"Failed to pull image {image}: {err[:500]}")
    return image


def _ensure_image_uncached(image: str) -> str:
    """Build or pull the Docker image if it doesn't already exist (no caching).

    For remote images (containing ``/``), pulls from the registry.
    For executor images the tag is derived from a content hash of the
    executor source directory so that code changes automatically trigger
    a rebuild.  Returns the image reference that should be used to run
    the container (may differ from *image* when a hash tag is applied).

    Derives Dockerfile/build context from image name:
      executor-gmail-modify:latest -> -f src/executors/gmail_modify/Dockerfile src/executors/
      llm-runner:latest            -> src/llm/
    """
    # Remote registry images — pull instead of build
    if _is_remote_image(image):
        # Check if already present locally
        inspect = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
        )
        if inspect.returncode == 0:
            return image
        return _pull_image(image)

    base = image.split(":")[0]

    if base.startswith("executor-"):
        name = base.removeprefix("executor-").replace("-", "_")
        executor_dir = Path("src/executors") / name
        content_hash = _compute_executor_hash(executor_dir)
        hashed_image = f"{base}:{content_hash}"

        # Already built with this hash – nothing to do.
        inspect = subprocess.run(
            ["docker", "image", "inspect", hashed_image],
            capture_output=True,
        )
        if inspect.returncode == 0:
            return hashed_image

        # Ensure the shared base image is built first (child Dockerfiles
        # use FROM creel-executor-base:latest).
        if _BASE_DOCKERFILE.exists():
            _ensure_base_image()

        context = Path("src/executors")
        dockerfile = executor_dir / "Dockerfile"
        if not dockerfile.exists():
            raise FileNotFoundError(f"No Dockerfile at {dockerfile} for image {image}")

        _build_image(
            tags=[hashed_image, f"{base}:latest"],
            dockerfile=dockerfile,
            context=context,
        )
        return hashed_image

    # Non-executor images: use existing tag-based check.
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
    )
    if result.returncode == 0:
        return image

    if base == "llm-runner":
        context = Path("src/llm")
        dockerfile = context / "Dockerfile"
    else:
        context = Path("src") / base.replace("-", "_")
        if not context.exists():
            context = Path("src") / base
        dockerfile = context / "Dockerfile"

    if not dockerfile.exists():
        raise FileNotFoundError(f"No Dockerfile at {dockerfile} for image {image}")

    _build_image(tags=[image], dockerfile=dockerfile, context=context)
    return image


def _ensure_image_from_dockerfile(dockerfile_path: str, image_tag: str) -> str:
    """Build a Docker image from a custom Dockerfile path.

    Used when a tool specifies ``dockerfile:`` instead of ``image:``.
    Returns the image reference.
    """
    dockerfile = Path(dockerfile_path)
    if not dockerfile.exists():
        raise FileNotFoundError(f"Custom Dockerfile not found: {dockerfile}")

    # Check if image already exists
    inspect = subprocess.run(
        ["docker", "image", "inspect", image_tag],
        capture_output=True,
    )
    if inspect.returncode == 0:
        return image_tag

    _build_image(
        tags=[image_tag],
        dockerfile=dockerfile,
        context=dockerfile.parent,
    )
    return image_tag


def _build_image(
    tags: list[str],
    dockerfile: Path,
    context: Path,
) -> None:
    """Run ``docker build`` with one or more ``-t`` tags."""
    build_cmd: list[str] = ["docker", "build"]
    for t in tags:
        build_cmd.extend(["-t", t])
    build_cmd.extend(["-f", str(dockerfile), str(context)])

    logger.info("Building image %s from %s (Dockerfile: %s)", tags[0], context, dockerfile)
    build_result = subprocess.run(
        build_cmd,
        capture_output=True,
        text=True,
    )
    if build_result.returncode != 0:
        build_err = build_result.stderr.strip() if build_result.stderr else "unknown error"
        logger.error("Docker build failed for %s:\n%s", tags[0], build_err)
        raise RuntimeError(f"Docker build failed for {tags[0]}: {build_err[:500]}")


def collect_required_images(agent_def: AgentDefinition) -> list[str]:
    """Derive the set of Docker images needed by an agent's tools.

    Uses the same naming convention as :pyattr:`ExecutorConfig.image`:
    ``executor-{name}:latest`` (underscores → hyphens).
    ``ToolConfig.image`` overrides the derived name when set.
    ``ToolConfig.dockerfile`` means the image will be built locally from
    a custom Dockerfile — it is included with a ``custom-`` prefix.
    The ``llm-runner:latest`` image is included when tools are present
    (agent mode requires the containerised LLM runner).
    The shared base image (``creel-executor-base:latest``) is included
    when any local executor images are present (not needed for pre-built
    registry images).
    """
    images: set[str] = set()
    has_local_executor_images = False
    for _tool_name, tool_config in agent_def.tools.items():
        if tool_config.dockerfile:
            # Custom Dockerfile — tag derived from tool name
            image = f"custom-{tool_config.executor.replace('_', '-')}:latest"
            images.add(image)
        elif tool_config.image:
            images.add(tool_config.image)
            # Custom image overrides (local or remote) don't need the base
        else:
            image = f"executor-{tool_config.executor.replace('_', '-')}:latest"
            images.add(image)
            has_local_executor_images = True
    if agent_def.tools:
        images.add("llm-runner:latest")
    if has_local_executor_images and _BASE_DOCKERFILE.exists():
        images.add(f"{_BASE_IMAGE_NAME}:latest")
    return sorted(images)


def pull_required_images(agent_def: AgentDefinition) -> list[str]:
    """Pull all pre-built images required by an agent definition.

    Only pulls remote/registry images (those containing ``/``).
    Returns a list of status messages for display.

    Used by ``creel init`` to pre-fetch GHCR images so executors are
    ready to run without a first-use download delay.
    """
    images = collect_required_images(agent_def)
    remote = [img for img in images if _is_remote_image(img)]

    if not remote:
        return ["  No remote images to pull."]

    messages: list[str] = []
    for img in remote:
        try:
            _pull_image(img)
            messages.append(f"  pulled: {img}")
        except RuntimeError as exc:
            messages.append(f"  warning: failed to pull {img} ({exc})")

    return messages


def prebuild_images(agent_def: AgentDefinition) -> list[threading.Thread]:
    """Kick off background image builds for all tools in the agent definition.

    Builds the shared base image synchronously first (all executor images
    depend on it), then kicks off parallel builds for the rest.

    Returns the list of spawned threads (callers are not expected to join).
    """
    images = collect_required_images(agent_def)

    # Build base image synchronously first if it's in the list
    base_tag = f"{_BASE_IMAGE_NAME}:latest"
    if base_tag in images:
        logger.info("Building shared base image before executor images")
        try:
            _ensure_base_image()
        except Exception:
            logger.warning("Base image pre-build failed (will retry on demand)", exc_info=True)
        images = [img for img in images if img != base_tag]

    logger.info("Pre-building %d Docker image(s): %s", len(images), images)
    return _image_cache.start_prebuild(images)


def _run_executor_container(
    config: ExecutorConfig,
    tool_config: ToolConfig | None = None,
    bridge_config: BridgeConfig | None = None,
) -> str:
    """Run an executor in an isolated Docker container.

    Captures both stdout (data) and stderr (logs/errors). Stderr is
    always logged at DEBUG on success and ERROR on failure. The
    request_id is passed into the container as ``CREEL_REQUEST_ID``
    for log correlation.

    Args:
        config: Executor configuration
        tool_config: Optional tool configuration with mount/network/image overrides
        bridge_config: Optional bridge configuration for macOS host tools
    """
    from creel.log import request_id_var
    from creel.orchestrator import (
        _EXECUTOR_TO_BRIDGE_SCOPE,
        _replace_google_credentials_with_access_token,
    )

    # Validate host_auth early — before image build — so misconfigurations fail fast
    _host_auth_entry: _HostAuthEntry | None = None
    if tool_config and tool_config.host_auth:
        if tool_config.secrets:
            raise ValueError(
                "host_auth and secrets are mutually exclusive — use one or the other, not both"
            )
        executor_name = config.name
        if not executor_name:
            raise ValueError("host_auth requires the executor to have a name")
        _host_auth_entry = _HOST_AUTH_REGISTRY.get(executor_name)
        if _host_auth_entry is None:
            raise ValueError(
                f"host_auth is not supported for executor '{executor_name}' "
                f"(supported: {sorted(_HOST_AUTH_REGISTRY)})"
            )
        auth_host_path = Path(os.path.expanduser(_host_auth_entry["host_path"]))
        if not auth_host_path.is_dir():
            raise RuntimeError(
                f"Host auth directory not found: {auth_host_path} — "
                f"run `gh auth login` to authenticate first"
            )

    # Determine image to use - dockerfile > image override > executor default
    if tool_config and tool_config.dockerfile:
        custom_tag = f"custom-{config.name.replace('_', '-')}:latest"
        image = _ensure_image_from_dockerfile(tool_config.dockerfile, custom_tag)
    else:
        image = tool_config.image if (tool_config and tool_config.image) else config.image
        image = _ensure_image(image)

    env_vars: dict[str, str] = {}

    # Decrypt and inject secrets
    if config.secrets:
        env_vars.update(decrypt_env_file(config.secrets))

    # Pass args as env vars
    for key, value in config.args.items():
        env_vars[key.upper()] = value

    _replace_google_credentials_with_access_token(env_vars)

    # Pass request ID for correlation
    rid = request_id_var.get(None)
    if rid:
        env_vars["CREEL_REQUEST_ID"] = rid

    # Add bridge configuration if enabled
    if bridge_config and bridge_config.enabled:
        # Rewrite localhost to host.docker.internal for container access
        bridge_url = bridge_config.url
        bridge_url = bridge_url.replace("://localhost", "://host.docker.internal")
        bridge_url = bridge_url.replace("://127.0.0.1", "://host.docker.internal")
        env_vars["BRIDGE_URL"] = bridge_url
        # Look up scoped token by executor name (e.g. browser → BRIDGE_TOKEN_BROWSER)
        executor_name = config.name or ""
        scope_name = _EXECUTOR_TO_BRIDGE_SCOPE.get(executor_name, executor_name.upper())
        scoped_token = os.environ.get(f"BRIDGE_TOKEN_{scope_name}", "")
        if scoped_token:
            env_vars["BRIDGE_TOKEN"] = scoped_token
        elif bridge_config.token:
            env_vars["BRIDGE_TOKEN"] = bridge_config.token

    # Handle workspace mount for file_ops (must be before env_file write
    # so WORKSPACE=/workspace ends up in the env file, not the host path)
    _workspace_mount: tuple[str, str] | None = None
    workspace_path = config.args.get("workspace")
    if workspace_path and config.name in ("file_ops",):
        resolved_ws = os.path.realpath(workspace_path)
        if not os.path.isdir(resolved_ws):
            raise RuntimeError("Workspace directory no longer exists")
        # Use read-only mount for read/list operations
        action = config.args.get("action", "")
        mount_mode = "ro" if action in ("read", "list") else "rw"
        _workspace_mount = (resolved_ws, mount_mode)
        env_vars["WORKSPACE"] = "/workspace"

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".env", delete=True, prefix="creel-"
    ) as env_file:
        for key, value in env_vars.items():
            # Sanitize values to prevent env-file newline injection
            sanitized = value.replace("\n", "").replace("\r", "")
            env_file.write(f"{key}={sanitized}\n")
        env_file.flush()

        # Build docker run command with per-tool resource overrides
        writable = tool_config.writable if tool_config else False
        tmpfs_size = tool_config.tmpfs_size if tool_config else "16M"
        memory = tool_config.memory if tool_config else "256m"
        cpus = tool_config.cpus if tool_config else "0.5"

        docker_cmd = [
            "docker",
            "run",
            "--rm",
        ]
        if not writable:
            docker_cmd.append("--read-only")
        docker_cmd.extend(
            [
                "--tmpfs",
                f"/tmp:rw,noexec,nosuid,size={tmpfs_size}",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                f"--memory={memory}",
                f"--cpus={cpus}",
                "--env-file",
                env_file.name,
            ]
        )

        # Add mount options from tool config
        if tool_config and tool_config.mounts:
            for mount in tool_config.mounts:
                # Expand ~ to home directory
                host_path = os.path.expanduser(mount.path)
                docker_cmd.extend(["-v", f"{host_path}:/mnt{host_path}:{mount.mode}"])

        # Mount host CLI auth directory (validated above, before image build)
        if _host_auth_entry is not None:
            auth_host_path = Path(os.path.expanduser(_host_auth_entry["host_path"]))
            docker_cmd.extend(["-v", f"{auth_host_path}:{_host_auth_entry['container_path']}:ro"])

        # Mount dynamic workspace for file_ops executor
        if _workspace_mount:
            docker_cmd.extend(["-v", f"{_workspace_mount[0]}:/workspace:{_workspace_mount[1]}"])

        # Add network isolation if disabled
        if tool_config and not tool_config.network:
            docker_cmd.extend(["--network=none"])

        # Add image name
        docker_cmd.append(image)

        try:
            result = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                timeout=config.timeout,
            )
        except subprocess.TimeoutExpired as e:
            stderr = (e.stderr or "").strip() if isinstance(e.stderr, str) else ""
            if stderr:
                logger.error(
                    "Executor %s stderr (timeout after %ds):\n%s",
                    config.name,
                    config.timeout,
                    stderr,
                )
            raise RuntimeError(f"Executor '{config.name}' timed out after {config.timeout}s") from e

    # Log stderr regardless of exit code
    stderr = result.stderr.strip() if result.stderr else ""
    if stderr:
        if result.returncode == 0:
            logger.debug("Executor %s stderr (success):\n%s", config.name, stderr)
        else:
            logger.error(
                "Executor %s stderr (exit %d):\n%s", config.name, result.returncode, stderr
            )

    if result.returncode != 0:
        # Include stderr in the error so it propagates to the LLM
        error_detail = stderr[:500] if stderr else f"exit code {result.returncode}"
        raise RuntimeError(f"Executor '{config.name}' failed: {error_detail}")

    return result.stdout.strip()
