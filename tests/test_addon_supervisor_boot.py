"""Add-on container must boot the way the Supervisor actually starts it.

Regression tests for GitHub issues #9 (naigy81, HA OS on a Raspberry
Pi 4) and #10 (JBrocx). Both users reported that the ``FoxESS Control``
add-on never starts, with the add-on log showing only::

    [FATAL tini (7)] exec /init failed: Permission denied

The add-on's whole job is to copy ``custom_components/foxess_control``
into ``/config/custom_components/``, so an add-on that never reaches
``run.sh`` means the integration is never installed.

Root cause
----------
``foxess-control/build.yaml`` bases the image on Home Assistant's
Alpine base images (``ghcr.io/home-assistant/<arch>-base:3.21``).
Those images bundle **s6-overlay v3** (verified: the image ships
``/package/admin/s6-overlay/s6-overlay-3.2.2.0``) and declare
``ENTRYPOINT ["/init"]``. s6-overlay v3's ``/init`` *must* run as
PID 1 — ``s6-overlay-suexec`` aborts with
``fatal: can only run as pid 1`` otherwise.

The Supervisor decides whether to interpose Docker's own init process
(tini) in front of the entrypoint from the add-on's ``init:`` option,
and that option **defaults to true when the key is omitted**. From the
Supervisor source:

* ``supervisor/apps/validate.py``::

      vol.Optional(ATTR_INIT, default=True): vol.Boolean(),

* ``supervisor/apps/model.py``::

      @property
      def default_init(self) -> bool:
          \"\"\"Return True if the app have no own init.\"\"\"
          return self.data[ATTR_INIT]

* ``supervisor/docker/app.py``::

      init=self.app.default_init,

This is stated outright in the add-on documentation's ``init`` entry:

    Set this to ``false`` to disable the Docker default system init. Use
    this if the image has its own init system (Like s6-overlay). *Note:
    Starting in V3 of S6 setting this to ``false`` is required or the
    app won't start.*

-- https://developers.home-assistant.io/docs/add-ons/configuration, and
https://developers.home-assistant.io/blog/2022/05/12/s6-overlay-base-images

``foxess-control/config.yaml`` has no ``init:`` key, so the Supervisor
starts the container with ``init=True``. Docker then runs tini as PID 1,
tini execs ``/init``, and s6-overlay v3 refuses to run.

On the reported error string
---------------------------
Be precise about this: ``init: true`` on an s6-v3 base produces
``s6-overlay-suexec: fatal: can only run as pid 1`` — tini execs
``/init`` *successfully* and s6 then rejects its own PID. The users'
``exec /init failed: Permission denied`` is tini's own message for
``execve("/init")`` returning ``EACCES``, i.e. exec was *denied*
outright. That was the custom AppArmor profile which omitted ``/init``
(added in 7cf864d, dropped again in 0c21701).

So these are two stacked defects at the same choke point, not one:
the AppArmor denial is already fixed, and this test covers the one that
remains. Note that ``init: true`` is a necessary precondition for tini
to be in the exec chain at all — with ``init: false`` there is no tini,
so neither failure mode can occur.

Test strategy
-------------
``test_addon_boots_the_way_supervisor_starts_it`` is the strong test: it
builds the real ``foxess-control/Dockerfile`` and runs it exactly as the
Supervisor would, deriving the init-wrapper decision from
``config.yaml`` using the Supervisor's own default. It then asserts the
add-on actually did its job (the integration landed in ``/config``).
That is a genuine reproduction of the reported symptom rather than a
restatement of the fix.

``test_ha_base_image_addon_must_disable_docker_init`` and its
parametrised neighbourhood are a **deliberately weaker** static guard,
kept so the invariant is still checked on machines without a container
runtime. They derive the requirement from ``build.yaml``'s
``build_from`` rather than hardcoding it, but they can only encode what
we believe about those base images -- they cannot observe a real boot.
Treat the container test as the authority.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
ADDON_DIR = REPO_ROOT / "foxess-control"
ADDON_CONFIG = ADDON_DIR / "config.yaml"
ADDON_BUILD = ADDON_DIR / "build.yaml"
ADDON_DOCKERFILE = ADDON_DIR / "Dockerfile"

# Home Assistant base images are published as
# ghcr.io/home-assistant/<arch>-base[-<flavour>]:<tag>. Every one of them
# bundles s6-overlay and sets ENTRYPOINT ["/init"].
_HA_BASE_IMAGE_RE = re.compile(
    r"^ghcr\.io/home-assistant/[a-z0-9]+-base(?:-[a-z0-9.]+)?:(?P<tag>[A-Za-z0-9._-]+)$"
)

# s6-overlay v3 landed in home-assistant/docker-base on 2022-05-10
# ("Update s6-overlay to 3.1.0.1", e4ee5e6e). Alpine 3.16 was released
# 2022-05-23, so every HA Alpine base image tagged 3.16 or later carries
# s6-overlay v3; 3.14 and earlier carry v2, which tolerated not being
# PID 1. Only v3 hard-fails, so only v3 forces `init: false`.
_S6_V3_MIN_ALPINE = (3, 16)


def _load_yaml(path: Path) -> dict[str, Any]:
    """Parse a YAML mapping from ``path``."""
    with path.open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    assert isinstance(loaded, dict), f"{path} must contain a YAML mapping"
    return loaded


def _alpine_tag_tuple(tag: str) -> tuple[int, ...] | None:
    """Return ``(3, 21)`` for an Alpine version tag, else ``None``."""
    if not re.fullmatch(r"\d+(?:\.\d+)*", tag):
        return None
    return tuple(int(part) for part in tag.split("."))


def base_image_needs_pid1(image: str) -> bool:
    """Return True if ``image`` runs s6-overlay v3 and so must be PID 1.

    Derived from the image reference alone so the check keeps working if
    ``build.yaml`` is repointed at a different tag or a non-Home
    Assistant base.
    """
    match = _HA_BASE_IMAGE_RE.match(image.strip())
    if match is None:
        # Not a Home Assistant base image: no s6-overlay expectation.
        return False
    version = _alpine_tag_tuple(match.group("tag"))
    if version is None:
        # Non-numeric tag (e.g. a date-stamped or flavour tag) — current
        # Home Assistant base images are all s6-overlay v3.
        return True
    return version >= _S6_V3_MIN_ALPINE


def supervisor_uses_docker_init(addon_config: dict[str, Any]) -> bool:
    """Return whether the Supervisor would interpose tini as PID 1.

    Mirrors ``vol.Optional(ATTR_INIT, default=True)`` from
    ``supervisor/apps/validate.py`` — an absent ``init:`` key means
    Docker's init *is* used.
    """
    return bool(addon_config.get("init", True))


def addon_config_is_bootable(
    addon_config: dict[str, Any], build_config: dict[str, Any]
) -> bool:
    """Return True if this add-on config can boot on its own base images.

    False when any base image needs to be PID 1 but the Supervisor would
    put Docker's init there instead.
    """
    build_from = build_config.get("build_from", {})
    assert isinstance(build_from, dict), "build.yaml build_from must be a mapping"
    needs_pid1 = any(base_image_needs_pid1(image) for image in build_from.values())
    return not (needs_pid1 and supervisor_uses_docker_init(addon_config))


# --------------------------------------------------------------------------
# Strong test: build the real image and boot it the way the Supervisor does.
# --------------------------------------------------------------------------


def _container_runtime() -> str | None:
    """Return an available container runtime binary name, or None."""
    for candidate in ("podman", "docker"):
        if shutil.which(candidate):
            return candidate
    return None


@pytest.mark.slow
def test_addon_boots_the_way_supervisor_starts_it() -> None:
    """Add-on reaches run.sh and installs the integration into /config.

    Reproduces issues #9/#10 end to end: builds
    ``foxess-control/Dockerfile`` and runs it with the init-wrapper
    setting the Supervisor would derive from ``config.yaml``. With no
    ``init: false`` the container dies inside ``/init`` and never copies
    anything, so ``manifest.json`` is missing from ``/config``.
    """
    runtime = _container_runtime()
    if runtime is None:
        pytest.skip("no podman/docker available to boot the add-on image")

    addon_config = _load_yaml(ADDON_CONFIG)
    build_config = _load_yaml(ADDON_BUILD)
    build_from = build_config["build_from"]

    # The runtime's own arch is the only one we can execute natively.
    base_image = build_from["amd64"]

    # Pull the base image up front so a network outage is reported as a
    # skip rather than masquerading as the add-on failing to boot.
    pull = subprocess.run(
        [runtime, "pull", base_image],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if pull.returncode != 0:
        pytest.skip(f"cannot pull {base_image}: {pull.stderr.strip()[:200]}")

    unique = uuid.uuid4().hex[:10]
    image_tag = f"localhost/foxess-addon-boot-test:{unique}"
    volume = f"foxess-addon-boot-{unique}"

    build = subprocess.run(
        [
            runtime,
            "build",
            "--build-arg",
            f"BUILD_FROM={base_image}",
            "-f",
            str(ADDON_DOCKERFILE),
            "-t",
            image_tag,
            str(REPO_ROOT),
        ],
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert build.returncode == 0, (
        f"add-on image build failed:\n{build.stdout[-3000:]}\n{build.stderr[-3000:]}"
    )

    # A named volume (root-owned inside the container) avoids the
    # rootless-bind-mount UID mismatch that would make run.sh's mkdir
    # fail for reasons unrelated to the bug under test.
    subprocess.run(
        [runtime, "volume", "create", volume],
        check=True,
        capture_output=True,
        timeout=60,
    )

    try:
        # This is the line under test: the Supervisor passes
        # `init=<config.yaml init, default True>` straight to Docker.
        run_cmd = [runtime, "run", "--rm"]
        if supervisor_uses_docker_init(addon_config):
            run_cmd.append("--init")
        run_cmd += ["-v", f"{volume}:/config:rw", image_tag]

        started = subprocess.run(
            run_cmd,
            capture_output=True,
            text=True,
            timeout=600,
        )

        # The observable contract: run.sh installed the integration.
        probe = subprocess.run(
            [
                runtime,
                "run",
                "--rm",
                "-v",
                f"{volume}:/config",
                "--entrypoint",
                "",
                base_image,
                "test",
                "-f",
                "/config/custom_components/foxess_control/manifest.json",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )

        combined = f"{started.stdout}\n{started.stderr}"
        assert probe.returncode == 0, (
            "add-on did not install the integration into /config "
            f"(exit={started.returncode}). Container output:\n{combined[-3000:]}"
        )
        assert started.returncode == 0, (
            "add-on container did not exit cleanly "
            f"(exit={started.returncode}). Container output:\n{combined[-3000:]}"
        )
    finally:
        subprocess.run(
            [runtime, "volume", "rm", "-f", volume],
            capture_output=True,
            timeout=120,
        )
        subprocess.run(
            [runtime, "rmi", "-f", image_tag],
            capture_output=True,
            timeout=300,
        )


# --------------------------------------------------------------------------
# Weaker static guard, derived from build.yaml (runs without a runtime).
# --------------------------------------------------------------------------


def test_ha_base_image_addon_must_disable_docker_init() -> None:
    """Shipped add-on config can boot on the base images it declares.

    Weaker than the container test above: it reasons from
    ``build.yaml``'s ``build_from`` instead of observing a real boot.
    """
    addon_config = _load_yaml(ADDON_CONFIG)
    build_config = _load_yaml(ADDON_BUILD)

    assert any(
        base_image_needs_pid1(image) for image in build_config["build_from"].values()
    ), "expected build.yaml to target s6-overlay-v3 Home Assistant base images"

    assert addon_config_is_bootable(addon_config, build_config), (
        "foxess-control/config.yaml bases on s6-overlay-v3 Home Assistant "
        "base images, which must run as PID 1, but the Supervisor would "
        "start the container with Docker's init (tini) as PID 1 because "
        f"init resolves to {supervisor_uses_docker_init(addon_config)!r}. "
        "Set 'init: false' in config.yaml."
    )


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("ghcr.io/home-assistant/amd64-base:3.21", True),
        ("ghcr.io/home-assistant/aarch64-base:3.21", True),
        ("ghcr.io/home-assistant/armv7-base:3.16", True),
        # s6-overlay v2 vintage: tolerated not being PID 1.
        ("ghcr.io/home-assistant/amd64-base:3.14", False),
        # Not a Home Assistant base image: no s6-overlay expectation.
        ("docker.io/library/alpine:3.21", False),
        ("docker.io/library/python:3.13-slim", False),
        ("ghcr.io/some-other-org/amd64-base:3.21", False),
    ],
)
def test_base_image_pid1_requirement_detection(image: str, expected: bool) -> None:
    """Only s6-overlay-v3 Home Assistant base images demand PID 1."""
    assert base_image_needs_pid1(image) is expected


@pytest.mark.parametrize(
    ("init_value", "expected"),
    [
        # The bug: key omitted, so the Supervisor default (True) applies.
        ({}, False),
        ({"init": True}, False),
        ({"init": False}, True),
    ],
)
def test_s6_v3_base_requires_init_false(
    init_value: dict[str, Any], expected: bool
) -> None:
    """An s6-v3 base is only bootable when init is explicitly false."""
    build_config = {"build_from": {"amd64": "ghcr.io/home-assistant/amd64-base:3.21"}}
    assert addon_config_is_bootable(init_value, build_config) is expected


@pytest.mark.parametrize("init_value", [{}, {"init": True}, {"init": False}])
def test_non_s6_base_is_not_required_to_disable_init(
    init_value: dict[str, Any],
) -> None:
    """A non-s6 base image imposes no ``init: false`` requirement."""
    build_config = {"build_from": {"amd64": "docker.io/library/alpine:3.21"}}
    assert addon_config_is_bootable(init_value, build_config) is True


def test_mixed_bases_flag_the_s6_v3_member() -> None:
    """One s6-v3 base among several is enough to require ``init: false``."""
    build_config = {
        "build_from": {
            "amd64": "docker.io/library/alpine:3.21",
            "aarch64": "ghcr.io/home-assistant/aarch64-base:3.21",
        }
    }
    assert addon_config_is_bootable({}, build_config) is False
    assert addon_config_is_bootable({"init": False}, build_config) is True


def test_supervisor_init_defaults_to_true_when_key_absent() -> None:
    """Absent ``init:`` means Docker's init is used (Supervisor default)."""
    assert supervisor_uses_docker_init({}) is True
    assert supervisor_uses_docker_init({"init": True}) is True
    assert supervisor_uses_docker_init({"init": False}) is False
