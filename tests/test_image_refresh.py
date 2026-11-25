"""SwarmForge's own sandbox images rebuild themselves when their recipe changed."""
import subprocess
from types import SimpleNamespace

from swarmforge import preflight
from swarmforge.sandbox import RECIPE_LABEL, recipe_hash


def fake_docker(monkeypatch, labels):
    builds = []

    def run(args, **kw):
        if args[1:3] == ["image", "inspect"]:
            img = args[-1]
            if img not in labels:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout=labels[img] + "\n", stderr="")
        if args[1] == "build":
            tag = args[args.index("-t") + 1]
            label = args[args.index("--label") + 1].split("=", 1)[1]
            builds.append(tag)
            labels[tag] = label
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(args)
    monkeypatch.setattr(subprocess, "run", run)
    preflight._REBUILD_TRIED.clear()
    return builds


def test_outdated_dev_image_is_rebuilt_once_and_then_trusted(cfg, monkeypatch):
    base = recipe_hash("Dockerfile.sandbox", {"BASE_IMAGE": cfg.sandbox.base_image})
    labels = {cfg.sandbox.image: base, preflight.DEV_IMAGE: ""}  # dev image predates the label: stale
    builds = fake_docker(monkeypatch, labels)
    assert preflight._image_problems(cfg, preflight.DEV_IMAGE) == []
    assert builds == [preflight.DEV_IMAGE]  # base was current; only the dev image was rebuilt
    assert preflight._image_problems(cfg, preflight.DEV_IMAGE) == []
    assert builds == [preflight.DEV_IMAGE]  # current now: no second build


def test_missing_image_is_built_and_failed_rebuild_of_an_old_image_does_not_block(cfg, monkeypatch):
    labels = {}
    builds = fake_docker(monkeypatch, labels)
    assert preflight._image_problems(cfg, None) == []
    assert builds == [cfg.sandbox.image]
    labels[cfg.sandbox.image] = "old-recipe"
    preflight._REBUILD_TRIED.clear()
    def failing(args, **kw):
        if args[1] == "build":
            return SimpleNamespace(returncode=1, stdout="", stderr="network down")
        return SimpleNamespace(returncode=0, stdout="old-recipe", stderr="")
    monkeypatch.setattr(subprocess, "run", failing)
    assert preflight._image_problems(cfg, None) == []  # the old image still runs tasks


def test_other_images_are_never_rebuilt(cfg, monkeypatch):
    builds = fake_docker(monkeypatch, {"someone/else:1": ""})
    assert preflight._image_problems(cfg, "someone/else:1") == []
    assert builds == []
    assert RECIPE_LABEL == "swarmforge.recipe"
