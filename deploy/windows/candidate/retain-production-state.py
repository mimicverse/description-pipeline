"""Retain immutable candidate evidence while BOTH endpoint owners are stopped."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import uuid
from description_pipeline.io import acquire_process_lock
from description_pipeline.orchestration.windows import read_config

base = Path(__file__).resolve().parent
root = base.parent
original = json.loads((root / 'endpoint.json').read_text(encoding='utf-8-sig'))
candidate = json.loads((base / 'endpoint-candidate.json').read_text(encoding='utf-8-sig'))
roots = {'package_root', 'output_root', 'state_root'}
allowed = roots | {'schema_version', 'token_file', 'host', 'port', 'targets'}
# These two retired input interfaces are intentionally absent in the current API.
retired = {'discovery', 'handoff_roots'}
assert set(candidate) == allowed and not (set(original) - allowed - retired)
for key in allowed - roots:
    assert candidate[key] == original.get(key, {'host': '127.0.0.1', 'port': 8765}.get(key)), key


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def reject_redirect(path):
    if path.is_symlink() or path.is_junction():
        raise RuntimeError(f'Unexpected redirected evidence path: {path}')


def walk_files(source):
    """Depth-first walk that refuses redirected paths before descending into them."""
    pending = [source]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                path = Path(entry.path)
                reject_redirect(path)
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    yield path


def check_target(target, destination):
    """Refuse redirected parents and file-vs-directory conflicts before any copy."""
    reject_redirect(target)
    node = target.parent
    while True:
        reject_redirect(node)
        if node == destination:
            return
        if node.exists() and not node.is_dir():
            raise RuntimeError(f'Production path is not a directory: {node}')
        node = node.parent


def existing_ancestor(path):
    node = path
    while not node.exists() and node.parent != node:
        node = node.parent
    return node


def scan():
    result = {}
    for key in sorted(roots):
        source, destination = Path(candidate[key]), Path(original[key])
        reject_redirect(source)
        reject_redirect(destination)
        if destination.exists() and not destination.is_dir():
            raise RuntimeError(f'Production root is not a directory: {destination}')
        if source == destination or source in destination.parents or destination in source.parents:
            raise RuntimeError('Runtime data roots overlap')
        for path in walk_files(source):
            if key == 'state_root' and path.name == '.endpoint.lock':
                continue
            relative = path.relative_to(source)
            target = destination / relative
            check_target(target, destination)
            result[(key, relative.as_posix())] = (path, target, digest(path), path.stat().st_size)
    return dict(sorted(result.items()))


handles = []
try:
    for config in (original, candidate):
        state = Path(config['state_root'])
        handles.append(acquire_process_lock(state / '.endpoint.lock'))
        for path in (state / 'jobs').glob('*.json'):
            job = json.loads(path.read_text(encoding='utf-8-sig'))
            if job.get('status') in {'queued', 'selecting', 'running', 'awaiting_definition'}:
                raise RuntimeError('Refusing retention with an active native job')
    planned = scan()
    needed = {}
    probes = {}
    for _path, target, sha, size in planned.values():
        if target.exists():
            if not target.is_file() or digest(target) != sha:
                raise RuntimeError(f'Preserve differing production file: {target}')
            continue
        probe = existing_ancestor(target)
        anchor = str(probe.anchor).upper()
        needed[anchor] = needed.get(anchor, 0) + size
        probes.setdefault(anchor, probe)
    for anchor, size in sorted(needed.items()):
        if not size:
            continue
        if shutil.disk_usage(probes[anchor]).free <= size:
            raise RuntimeError(f'Insufficient space on {anchor} for retained evidence')
    receipt = []
    for (key, relative), (path, target, sha, _) in planned.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = target.with_name(target.name + '.retaining-' + uuid.uuid4().hex)
            try:
                with path.open('rb') as source, temporary.open('xb') as destination:
                    shutil.copyfileobj(source, destination)
                    destination.flush()
                    os.fsync(destination.fileno())
                assert digest(temporary) == sha, 'Copy differs from source'
                temporary.rename(target)  # Windows refuses an existing destination.
            finally:
                temporary.unlink(missing_ok=True)
        assert digest(target) == sha and digest(path) == sha, 'Evidence changed during retention'
        receipt.append({'root': key, 'relative': relative, 'sha256': sha})
    assert scan() == planned, 'Candidate inventory changed during retention'
    promoted = dict(candidate)
    for key in roots:
        promoted[key] = original[key]
    config_file = base / 'endpoint-promoted.json'
    with config_file.open('w', encoding='utf-8') as stream:
        stream.write(json.dumps(promoted, indent=2) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    read_config(config_file)  # Validate current schema, paths and token without printing secrets.
    result = {'complete': True, 'config_sha256': digest(config_file), 'files': receipt}
    with (base / 'retained-production-state.json').open('w', encoding='utf-8') as stream:
        stream.write(json.dumps(result, indent=2) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps({'retained_files': len(receipt), 'existing_production_roots_preserved': True}))
finally:
    for handle in handles:
        os.close(handle)
