"""Runnable synthetic end-to-end example of the one-hour draft runner. Reads no live source and sends nothing.

    PYTHONPATH=.:tests python scripts/last_scan_synthetic_demo.py /path/to/empty/output/folder

A synthetic SharePoint-shaped capture (tests/source_first_fixture.py) is fed through the real runner, the real
durable store (SQLite here) and the real native-template publisher, with a simulated clock:

  21:00  one scan at 20:30                -> waiting (hour not over)
  21:20  scanning resumed (scan at 21:18) -> waiting, hour restarted from 21:20
  22:10  nothing new                      -> still waiting (this is the "lunch" case: no draft yet)
  22:20  quiet for a full hour            -> DRAFT + checklist written, held, internal outbox entry
  22:25  same firing delivered twice      -> replayed from the run receipt, nothing read or built again
  22:40  a late edit arrives              -> the draft is superseded and the hour starts again
"""
import json
import sys
from pathlib import Path
from hosting import source_first as sf
from hosting import last_scan_runner as runner
from hosting.last_scan_local import StateStore, NativeDraftRouter
from hosting.last_scan_service import source_adapter
from source_first_fixture import capture, MAP, SCOPE


def main(folder):
    out = Path(folder); out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise SystemExit('Choose an empty folder; nothing is ever overwritten.')
    now = ['']; scan = ['2026-11-10T20:30:00Z']; comment = ['Two scratched parts']
    def cap(scope):
        c = capture(); raw = c['records'][0]['raw']
        raw['ActualScan'] = scan[0]; raw['Rejected'] = None; raw['Comments'] = comment[0]   # NOK count unknown at source
        c['captured_at'] = now[0]
        return sf.seal({k: v for k, v in c.items() if k not in ('snapshot_hash', 'envelope_hash')}), {}
    store = StateStore(out / 'state.db')
    entry = dict(client_key='synthetic', site_id=SCOPE['site_id'], list_id=SCOPE['list_id'], job=SCOPE['job'], verified=True)
    config = dict(enabled=True, assignments=[dict(
        verified=True, active=True, timestamp_semantics_verified=True, client_id='synthetic', job=SCOPE['job'],
        site_id=SCOPE['site_id'], list_id=SCOPE['list_id'], zone='America/Chicago', scan_field='ActualScan',
        shift_field='Shift', recorded_shifts=['1'], business_dates=['2026-11-10'],
        mapping=MAP, header={})])       # this assignment's own report mapping and header
    publisher = NativeDraftRouter(out / 'drafts', config['assignments'])
    drafts = publisher.for_scope(config['assignments'][0]).output
    def fire(at, run_id=None):
        now[0] = at
        result = runner.run_once(store, config, fetch=lambda s, settings: source_adapter(s, settings, capture_fn=cap),
                                 render=publisher, run_id=run_id or 'firing-' + at, entries=[entry], clock=lambda: now[0])
        row = result['results'][0]
        print(at, '->', row['status'], '| due', row.get('due_at'), '| replayed' if result.get('replayed') else '')
        return result
    fire('2026-11-10T21:00:00Z')
    scan[0] = '2026-11-10T21:18:00Z'
    fire('2026-11-10T21:20:00Z')
    fire('2026-11-10T22:10:00Z')
    done = fire('2026-11-10T22:20:00Z')
    fire('2026-11-10T22:25:00Z', run_id='firing-2026-11-10T22:20:00Z')
    comment[0] = 'Two scratched parts (count corrected later)'
    fire('2026-11-10T22:40:00Z')
    op = done['results'][0]['operation_id']
    checklist = json.loads((drafts / op / 'checklist.json').read_text())
    with store.transaction('outbox:' + done['results'][0]['scope']) as box:
        entries = {k[:12]: v['status'] for k, v in box['entries'].items()}
    print(json.dumps(dict(draft=str(drafts / op / 'draft.xlsx'), checklist_status=checklist['status'],
                          client_release=checklist['client_release'], day_completion=checklist['day_completion'],
                          unknown_values=[f['field'] for f in checklist['findings'] if f.get('code') == 'missing_value'],
                          outbox=entries, notifications_sent=done['notifications_sent']), indent=2))


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else '')
