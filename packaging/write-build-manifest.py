"""Record a local package build; this developer tool is never installed."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

project, source, runtime_package, runtime_source, work = map(Path, sys.argv[1:])
def git(*args, cwd):
    return subprocess.check_output(['git', *args], cwd=cwd, text=True).strip()
def digest(path):
    checksum = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(block)
    return checksum.hexdigest()
files = {str(path.relative_to(source)): digest(path) for path in sorted(source.rglob('*'))
         if path.is_file() and '.build' not in path.relative_to(source).parts}
signing_file = work/'signing.json'
signing_details = json.loads(signing_file.read_text()) if signing_file.exists() else dict(mode='ad-hoc', notarized=False)
signing = ('Developer ID; notarized and stapled' if signing_details['notarized'] else 'Developer ID; not notarized') \
    if signing_details['mode'] == 'developer-id' else 'local ad-hoc executables; unsigned and unnotarized installer'
manifest = dict(built_at=datetime.now(timezone.utc).isoformat(), version='0.1.0', architecture='arm64',
                minimum_macos='26.0', signing=signing, signing_details=signing_details,
                mybox_commit=git('rev-parse', 'HEAD', cwd=project),
                mybox_changes=git('status', '--short', '--untracked-files=no', cwd=project),
                runtime_commit=git('rev-parse', 'HEAD', cwd=runtime_source),
                runtime_package_sha256=digest(runtime_package),
                package_sha256=digest(project/'dist/MyBox-0.1.0-arm64.pkg'), source_files=files,
                build_directory=str(work), installation_performed=False)
(project/'dist/MyBox-0.1.0-build.json').write_text(json.dumps(manifest, indent=2)+'\n')
