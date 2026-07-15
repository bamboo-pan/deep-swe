"""Keep shared local-only images out of Pier's Compose image cleanup."""

from __future__ import annotations

from collections.abc import Sequence


LOCAL_IMAGE_SUFFIX = ":local"


def preserve_local_prebuilt_image(
    command: Sequence[str],
    *,
    image: object,
    use_prebuilt: bool,
) -> list[str]:
    """Remove ``down --rmi all`` only for a prebuilt ``:local`` image.

    Pier normally removes every image used by a deleted Compose environment.
    That is correct for per-trial build images, but a separate verifier can use
    a shared local-only image directly. Removing that tag makes concurrent and
    later trials try (and fail) to pull it from a registry.
    """
    values = list(command)
    if not (
        use_prebuilt
        and isinstance(image, str)
        and image.strip().endswith(LOCAL_IMAGE_SUFFIX)
        and values
        and values[0] == "down"
    ):
        return values

    preserved: list[str] = []
    index = 0
    while index < len(values):
        if values[index] == "--rmi" and index + 1 < len(values):
            index += 2
            continue
        preserved.append(values[index])
        index += 1
    return preserved
