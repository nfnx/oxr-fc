"""Run once on a NAS-mounted preparation environment, never during cold start."""
import argparse
import hashlib
import json
from pathlib import Path
import re


def verify(root):
    expected = ["config.json", "model.safetensors", "tokenizer.json", "chat_template.jinja",
                "plugins/vllm/pyproject.toml", "layout_analysis/last.pth", "layout_analysis/metadata.json"]
    manifest = {}
    for name in expected:
        path = root / name
        if not path.is_file() or not path.stat().st_size:
            raise ValueError("Missing or empty model file: " + name)
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            first = handle.read(128)
            if first.startswith(b"version https://git-lfs.github.com/spec/v1"):
                raise ValueError("Unresolved LFS pointer: " + name)
            digest.update(first)
            for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
                digest.update(chunk)
        manifest[name] = {"bytes": path.stat().st_size, "sha256": digest.hexdigest()}
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", required=True, help="Full Hugging Face model commit SHA")
    parser.add_argument("--target", required=True, type=Path, help="New directory on mounted NAS")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.revision):
        parser.error("revision must be a full 40-character commit SHA")
    if args.target.exists():
        parser.error("target already exists; use a new versioned directory")
    from huggingface_hub import snapshot_download
    # A partial directory is never a valid deployment target until receipt exists.
    snapshot_download(repo_id="SoMarkAI/OXR-1.0", revision=args.revision, local_dir=args.target)
    manifest = verify(args.target)
    receipt = {"repo": "SoMarkAI/OXR-1.0", "revision": args.revision, "files": manifest}
    (args.target / "model-manifest.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print("Model verified. Set MODEL_PATH to:", args.target)


if __name__ == "__main__":
    main()
