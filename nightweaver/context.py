"""Small immutable context packs anchored to the run's Git baseline."""

import hashlib
import json

from .safety import redact, write_json


def capture(spec, base):
    from .engine import git, git_bytes
    repository = spec["repository"]
    files, remaining = [], 48000
    for name in spec.get("context_files", []):
        reference = f"{base}:{name}"
        size = int(git(repository, "cat-file", "-s", reference))
        if size > 16000 or size > remaining:
            raise ValueError(f"Context file exceeds the 16KB per-file / 48KB pack budget: {name}")
        mode = git(repository, "--literal-pathspecs", "ls-tree", base, "--", name).split(" ", 1)[0]
        if mode not in ("100644", "100755"):
            raise ValueError(f"Context must be a regular committed file: {name}")
        raw = git_bytes(repository, "cat-file", "blob", reference)
        if b"\x00" in raw:
            raise ValueError(f"Context must be text: {name}")
        try:
            content = raw.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValueError(f"Context must be UTF-8 text: {name}") from error
        digest = hashlib.sha256(raw).hexdigest()
        files.append({"path": name, "sha256": digest, "content": redact(content)})
        remaining -= size
    names = git(repository, "ls-tree", "-r", "--name-only", "-z", base).rstrip("\0").split("\0") if spec.get("repository_map") else []
    names = [name for name in names if name]
    return {"base": base, "files": files, "repository_map": names[:240], "map_total_files": len(names)}


def save(store, run_id, pack):
    write_json(store.directory(run_id) / "context.json", pack)


def brief(store, run_id):
    path = store.directory(run_id) / "context.json"
    if not path.is_file():
        return ""
    pack = json.loads(path.read_text(encoding="utf-8"))
    parts = [f"Pinned project context at {pack['base']}"]
    for file in pack["files"]:
        parts.append(f"File {file['path']} (sha256 {file['sha256']})\n{file['content']}")
    if pack["repository_map"]:
        parts.append(f"Tracked-file map (first {len(pack['repository_map'])}/{pack['map_total_files']} files)\n"
                     + "\n".join(pack["repository_map"]))
    return "\n\n".join(parts) if len(parts) > 1 else ""
