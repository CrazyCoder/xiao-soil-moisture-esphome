#!/usr/bin/env python3
"""Check single-file Builder installs against an unpublished Git snapshot.

Run with the Python environment containing ESPHome:
  python tools/check-builder.py [--compile]

Only example credentials enter the fixture. Git URL rewrites apply to child
processes only; the working repository and global Git configuration are untouched.
"""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compile", action="store_true", help="also build both hardware variants")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]

    with tempfile.TemporaryDirectory(prefix="soil-builder-") as temp:
        fixture = Path(temp)
        repository = fixture / "repository"
        repository.mkdir()
        shutil.copytree(root / "components", repository / "components",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(root / "esphome/packages", repository / "esphome/packages")
        run("git", "init", "-q", "-b", "main", str(repository))
        run("git", "-C", str(repository), "add", ".")
        run("git", "-C", str(repository), "-c", "user.name=Builder check",
            "-c", "user.email=builder@example.invalid", "-c", "commit.gpgsign=false",
            "commit", "-qm", "Builder fixture")
        commit = run("git", "-C", str(repository), "rev-parse", "HEAD",
                     capture_output=True).stdout.strip()

        # Exercise the actual GitHub URLs in the sample, using unpublished
        # source files. Both the package loader and component loader clone Git.
        template = (root / "esphome/sample-builder.yaml").read_text()
        upstream = "CrazyCoder/xiao-soil-moisture-esphome"
        fork = "example-owner/soil-monitor-fork"
        cases = [(upstream, True), (upstream, False), (fork, True), (fork, False)]
        for selected_repository, with_button in cases:
            # Rewrite ONLY the selected source. Any accidentally hard-coded
            # HTTPS source is rejected instead of reaching the real network.
            env = os.environ.copy()
            index = int(env.get("GIT_CONFIG_COUNT", "0"))
            for suffix in (".git", ""):
                env[f"GIT_CONFIG_KEY_{index}"] = f"url.{repository.as_uri()}.insteadOf"
                env[f"GIT_CONFIG_VALUE_{index}"] = (
                    "https://github.com/" + selected_repository + suffix
                )
                index += 1
            env[f"GIT_CONFIG_KEY_{index}"] = "protocol.https.allow"
            env[f"GIT_CONFIG_VALUE_{index}"] = "never"
            env["GIT_CONFIG_COUNT"] = str(index + 1)

            label = ("upstream-" if selected_repository == upstream else "fork-")
            label += "button-main" if with_button else "timer-pinned"
            config_dir = fixture / label
            config_dir.mkdir()
            device = config_dir / "plant.yaml"
            config = template.replace(
                f'soil_monitor_repository: "{upstream}"',
                f'soil_monitor_repository: "{selected_repository}"',
            )
            if not with_button:
                config = config.replace('soil_monitor_ref: "main"',
                                        f'soil_monitor_ref: "{commit}"')
                config = config.replace(
                    "      - esphome/packages/xiao-soil-moisture-button.yaml\n", "")
            device.write_text(config)
            shutil.copyfile(root / "esphome/secrets.yaml.example",
                            config_dir / "secrets.yaml")
            command = [sys.executable, "-m", "esphome", "compile", str(device)]
            # Compile each hardware variant once; the fork cases verify source
            # selection/codegen without rebuilding identical C++ a second time.
            if not args.compile or selected_repository == fork:
                command.append("--only-generate")
            run(*command, env=env, cwd=config_dir)

            source = config_dir / ".esphome/build/plant-1/src"
            for header in (root / "components/soil_monitor").glob("*.h"):
                copied = source / "esphome/components/soil_monitor" / header.name
                assert copied.read_bytes() == header.read_bytes(), header.name
            includes = (source / "esphome.h").read_text()
            assert 'esphome/components/soil_monitor/soil_monitor_helpers.h' in includes
            main_cpp = (source / "main.cpp").read_text()
            assert ("my_button" in main_cpp) == with_button
            assert not (config_dir / "packages").exists()
            assert not (config_dir / "components").exists()
            for domain in ("packages", "external_components"):
                checkouts = list((config_dir / ".esphome" / domain).glob("*/.git"))
                assert len(checkouts) == 1, (label, domain)
                origin = run("git", "-C", str(checkouts[0].parent), "remote", "get-url",
                             "origin", capture_output=True).stdout.strip()
                assert origin.removesuffix(".git") == "https://github.com/" + selected_repository
            print(f"PASS: {label}: standalone YAML + secrets, Git packages and headers",
                  flush=True)


if __name__ == "__main__":
    main()
