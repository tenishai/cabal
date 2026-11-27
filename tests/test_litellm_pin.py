"""The litellm Python pin and the LiteLLM Proxy image name one version (issues 383, 481).

A bump has to move all three lines together, and each image is pinned by digest, so a
reviewed line is the only way a new LiteLLM build gets in.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_python_pin_and_both_proxy_images_name_one_version():
    pins = re.findall(r'"litellm==([0-9.]+)"', (ROOT / "pyproject.toml").read_text())
    assert len(pins) == 1, pins
    images = []
    for f in ("docker-compose.yml", "k8s/litellm.yaml"):
        found = re.findall(r"image:\s*(ghcr\.io/berriai/litellm\S*)", (ROOT / f).read_text())
        assert len(found) == 1, (f, found)
        images += found
    for image in images:
        m = re.fullmatch(r"ghcr\.io/berriai/litellm:v([0-9.]+)@sha256:[0-9a-f]{64}", image)
        assert m, f"{image} is not pinned as :v<version>@sha256:<digest>"
        assert m.group(1) == pins[0], (image, pins[0])
    assert images[0] == images[1]
