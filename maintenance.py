"""Bounded private backups and terminal-source retention for OCR lead research."""
from contextlib import closing, nullcontext
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tarfile
import tempfile

from worker.store import Store


def maintain(research, mailbox_paths, output, *, clock=None, reserve=512 * 1024**2):
    clock = clock or datetime.now(timezone.utc)
    research, output = Path(research), Path(output)
    paths = [research, *(Path(p) for p in mailbox_paths)]
    for path in [*paths, output]:
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("symlink_storage_refused")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not any(p.is_file() for p in paths):
        return {"state": "no_research_database", "models_called": 0}
    paths = [p for p in paths if p.is_file()]
    size = sum(p.stat().st_size + (p.with_name(p.name + '-wal').stat().st_size
               if p.with_name(p.name + '-wal').is_file() else 0) for p in paths)
    if shutil.disk_usage(output).free < reserve + 3 * size:
        raise RuntimeError("insufficient_backup_space")
    store = Store(research) if research.is_file() else None
    try:
        with store.exclusive() if store else nullcontext():
            target = output / ('lead-flow-' + clock.strftime('%Y%m%d-%H%M%S') + '.tar.gz')
            if target.exists():
                raise RuntimeError("backup_timestamp_exists")
            with tempfile.TemporaryDirectory(prefix='.snapshot-', dir=output) as temporary:
                private = Path(temporary)
                for index, source in enumerate(paths):
                    dest = private / (str(index) + '-' + source.name)
                    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as original, \
                         closing(sqlite3.connect(dest)) as copied:
                        original.backup(copied)
                        if copied.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                            raise RuntimeError('backup_integrity_failed')
                    os.chmod(dest, 0o600)
                archive = private / 'snapshot.tar.gz'
                with tarfile.open(archive, 'w:gz') as tar:
                    for source in private.glob('*.sqlite3'):
                        tar.add(source, arcname=source.name)
                os.chmod(archive, 0o600)
                with archive.open('r+b') as stream:
                    os.fsync(stream.fileno())
                os.link(archive, target)  # atomic publication, never replace
            cutoff = (clock - timedelta(days=14)).isoformat(timespec='seconds')
            removed = 0
            # Quotes, candidates, decisions, draft/outbox acknowledgements and
            # run identity survive. Active/retryable runs retain their bodies.
            rows = store.rows("SELECT pages.run_id,pages.url,pages.data FROM pages JOIN runs ON runs.id=pages.run_id "
                              "WHERE runs.state IN ('completed','completed_with_gaps') AND runs.updated<?", (cutoff,)) if store else []
            with store.db if store else nullcontext():
                for row in rows:
                    value = json.loads(row['data'])
                    if value.get('text'):
                        value['text'] = ''
                        value['body_compacted_at'] = clock.isoformat(timespec='seconds')
                        store.db.execute('UPDATE pages SET data=? WHERE run_id=? AND url=?',
                                         (json.dumps(value, ensure_ascii=False), row['run_id'], row['url']))
                        removed += 1
            expiry = (clock - timedelta(days=14)).timestamp()
            pruned = 0
            for path in output.iterdir():
                if (re.fullmatch(r'lead-flow-\d{8}-\d{6}\.tar\.gz', path.name)
                        and path.is_file() and not path.is_symlink() and path.stat().st_mtime < expiry):
                    path.unlink(); pruned += 1
            return {'state': 'completed', 'backup': target.name, 'databases': len(paths),
                    'source_bodies_compacted': removed, 'backups_pruned': pruned,
                    'models_called': 0, 'knowledge_preserved': True}
    finally:
        if store:
            store.close()


if __name__ == '__main__':
    try:
        result = maintain('/opt/lead-flow/data/research.sqlite3',
            ['/opt/iva/data/lead-flow/mail-dispatch.sqlite3', '/opt/iva/data/lead-flow/mail-inbox.sqlite3'],
            '/opt/iva/data/lead-flow/backups')
        print(json.dumps(result))
    except Exception as exc:
        print(json.dumps({'state': 'failed', 'error_type': type(exc).__name__}))
        raise SystemExit(1)
