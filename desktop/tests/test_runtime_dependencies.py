"""Runtime dependency smoke tests for the desktop sidecar."""


def test_websockets_runtime_dependency_is_importable():
    import websockets  # noqa: F401
