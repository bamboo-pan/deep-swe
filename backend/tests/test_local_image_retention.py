from app.pier_retry_patch.local_image_retention import (
    preserve_local_prebuilt_image,
)


def test_preserves_local_prebuilt_image_during_compose_cleanup():
    command = ["down", "--rmi", "all", "--volumes", "--remove-orphans"]

    assert preserve_local_prebuilt_image(
        command,
        image="deepswe-test-verifier:local",
        use_prebuilt=True,
    ) == ["down", "--volumes", "--remove-orphans"]


def test_keeps_cleanup_for_trial_build_images():
    command = ["down", "--rmi", "all", "--volumes", "--remove-orphans"]

    assert preserve_local_prebuilt_image(
        command,
        image="deepswe-test-base:local",
        use_prebuilt=False,
    ) == command


def test_keeps_cleanup_for_registry_images_and_non_down_commands():
    cleanup = ["down", "--rmi", "all", "--volumes", "--remove-orphans"]
    start = ["up", "--detach", "--wait"]

    assert preserve_local_prebuilt_image(
        cleanup,
        image="registry.example/test-verifier:v1",
        use_prebuilt=True,
    ) == cleanup
    assert preserve_local_prebuilt_image(
        start,
        image="deepswe-test-verifier:local",
        use_prebuilt=True,
    ) == start
