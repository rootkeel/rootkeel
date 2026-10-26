#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Print the download part of a release's notes, as Markdown.

Usage: scripts/release-notes.py <version> <images.txt> <assets-dir>

images.txt holds build-images.sh --push's output: one line per image, its tag and its digest
reference. The release workflow appends the list of changes after these notes.
"""
import os
import sys
from pathlib import Path

RT = [("macos-arm64", "macOS on Apple silicon"), ("macos-x86_64", "macOS on Intel"),
      ("linux-x86_64", "Linux on x86-64"), ("linux-arm64", "Linux on ARM64")]


def main(version, images_file, assets_dir):
    repository = os.environ.get("GITHUB_SERVER_URL", "https://github.com") + "/" + os.environ.get(
        "GITHUB_REPOSITORY", "rootkeel/rootkeel")
    images = dict(line.split() for line in Path(images_file).read_text().splitlines() if line.strip())
    assets = {path.name for path in Path(assets_dir).iterdir()}
    agent = f"rootkeel-agent-{version}-linux.tar.gz"
    evidence = f"rootkeel-{version}-images-sbom-provenance.tar.gz"
    wanted = [f"rt-{version}-{platform}.tar.gz" for platform, _ in RT] + [agent, evidence, "SHA256SUMS"]
    missing = [name for name in wanted if name not in assets]
    if missing or len(images) != 2:
        sys.exit("Release assets or images missing: %s" % ", ".join(missing or ["images"]))
    out = ["## Server images", "",
           "For `linux/amd64` and `linux/arm64`, with SBOM and build provenance attestations. Pin them by "
           "digest in your environment file:", "", "```sh"]
    for tag, digest in sorted(images.items()):
        name = tag.rsplit("/", 1)[1].split(":")[0]
        out.append(f"RT_{name.upper()}_IMAGE={digest}")
    out += ["```", "", "Tags: " + ", ".join(f"`{tag}`" for tag in sorted(images)) + ".", "",
            "## Downloads", "", "| File | What it is | Runs on |", "| --- | --- | --- |"]
    for platform, where in RT:
        out.append(f"| `rt-{version}-{platform}.tar.gz` | `rt`, standalone (no Python needed) | {where} |")
    out += [f"| `{agent}` | The agent installer: `sudo agent/install.sh <user> machine.json` | "
            "Debian 13 and Ubuntu 24.04, x86-64 and ARM64 |",
            f"| `{evidence}` | SPDX SBOMs and SLSA provenance of each image, per platform | |",
            "| `SHA256SUMS` | Checksums of the files above: `sha256sum -c SHA256SUMS` | |", "",
            "Windows is not supported natively: run the Linux `rt` inside WSL. `rt attach` needs "
            "[Zellij 0.45.1](https://github.com/zellij-org/zellij/releases/tag/v0.45.1) on the same computer; "
            "everything else in `rt` works without it. See [docs/install.md](%s/blob/v%s/docs/install.md)." % (repository, version)]
    print("\n".join(out))


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(*sys.argv[1:])
