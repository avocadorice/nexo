#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 - <<'PY'
import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

os.umask(0o077)
root = Path.cwd()
stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
destination = root / '.local' / 'recovery' / stamp
destination.mkdir(parents=True)
database = 'nexo_restore_' + stamp.lower()
kubectl = ['kubectl', '--kubeconfig', str(root/'.local/kubeconfig'),
           '--context', 'kind-nexo', '-n', 'nexo', 'exec', '-i', 'postgres-0', '--']
tables = ['customers', 'tokens', 'transactions', 'slots', 'batches', 'chargebacks',
          'delivery_attempts', 'audit_events', 'schema_migrations']
count_sql = 'SELECT json_object_agg(name,total) FROM (' + ' UNION ALL '.join(
    f"SELECT '{table}' name, count(*) total FROM {table}" for table in tables
) + ') AS counts'
integrity_sql = '''
SELECT json_build_object(
 'orphaned_assignments', (SELECT count(*) FROM chargebacks c LEFT JOIN batches b
     ON b.id=c.batch_id WHERE c.batch_id IS NOT NULL AND b.id IS NULL),
 'invalid_money_or_owner', (SELECT count(*) FROM chargebacks c JOIN transactions t
     ON t.id=c.transaction_id WHERE c.customer_id<>t.customer_id OR c.amount_minor>t.amount_minor),
 'invalid_batch_membership', (SELECT count(*) FROM chargebacks c JOIN batches b ON b.id=c.batch_id
     JOIN transactions t ON t.id=c.transaction_id WHERE b.network<>t.network
     OR b.partition<>c.partition OR c.received_at>=b.slot),
 'wrong_file_row_count', (SELECT count(*) FROM batches b WHERE b.state<>'building'
     AND b.row_count<>(SELECT count(*) FROM chargebacks c WHERE c.batch_id=b.id)),
 'reused_object_keys', (SELECT count(*)-count(DISTINCT object_key) FROM batches
     WHERE object_key IS NOT NULL),
 'orphaned_attempts', (SELECT count(*) FROM delivery_attempts a LEFT JOIN batches b
     ON b.id=a.batch_id WHERE b.id IS NULL)
)
'''

def query(db, sql):
    result = subprocess.run(kubectl + ['psql', '-X', '-U', 'nexo', '-d', db,
                            '-v', 'ON_ERROR_STOP=1', '-At', '-c', sql],
                            check=True, capture_output=True, text=True)
    return json.loads(result.stdout)

started = time.monotonic()
before = query('nexo', count_sql)
archive = destination/'nexo.dump'
with archive.open('wb') as output:
    subprocess.run(kubectl + ['pg_dump', '-U', 'nexo', '-d', 'nexo', '--format=custom',
                              '--no-owner', '--no-acl'], stdout=output, check=True)
dump_seconds = time.monotonic() - started
created = False
report = {'checked_at_utc': stamp, 'source_database': 'nexo', 'restore_database': database,
          'source_counts_before': before, 'dump_seconds': round(dump_seconds, 3),
          'archive_bytes': archive.stat().st_size}
with archive.open('rb') as source:
    digest = hashlib.sha256()
    for chunk in iter(lambda: source.read(1024*1024), b''):
        digest.update(chunk)
report['archive_sha256'] = digest.hexdigest()
try:
    subprocess.run(kubectl + ['createdb', '-U', 'nexo', database], check=True)
    created = True
    restore_started = time.monotonic()
    with archive.open('rb') as source:
        subprocess.run(kubectl + ['pg_restore', '-U', 'nexo', '-d', database,
                                  '--no-owner', '--no-acl', '--exit-on-error'],
                       stdin=source, check=True)
    report['restore_seconds'] = round(time.monotonic()-restore_started, 3)
    report['restored_counts'] = query(database, count_sql)
    report['source_counts_after'] = query('nexo', count_sql)
    report['integrity_violations'] = query(database, integrity_sql)
    if report['source_counts_before'] != report['source_counts_after']:
        raise RuntimeError('Live row counts changed during the drill; rerun at a quiet moment.')
    if report['restored_counts'] != report['source_counts_after']:
        raise RuntimeError('Restored row counts do not match the stable source counts.')
    if any(report['integrity_violations'].values()):
        raise RuntimeError('Restored relationship or financial checks failed.')
    report['result'] = 'passed'
finally:
    if created:
        subprocess.run(kubectl + ['dropdb', '-U', 'nexo', database], check=True)
    report['total_seconds'] = round(time.monotonic()-started, 3)
    (destination/'report.json').write_text(json.dumps(report, indent=2)+'\n')
print(json.dumps(report, indent=2))
print(f'Private archive and report: {destination.relative_to(root)}')
PY
