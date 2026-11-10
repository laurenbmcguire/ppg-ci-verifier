"""Admin-only manual source-review routes behind existing session and CSRF gates."""
import json
from urllib.parse import parse_qs, urlsplit
from hosting import source_first as sf, source_first_service as service
from hosting.files import files_for
from hosting.spreadsheet_inbox import SpreadsheetInbox
from hosting.storage import StorageUnavailable


def response_json(result):
    raw = json.dumps(result, allow_nan=False).encode()
    if len(raw) > sf.MAX_RESPONSE_BYTES:
        raise sf.ReviewError('This review exceeds the private pilot response limit. The operation may already be saved; reload saved receipts before retrying. No data was truncated or report released.')
    return raw


def handle(h, method, route, payload=None):
    try:
        sf.actor(h.current_user())
        query = parse_qs(urlsplit(h.path).query)
        key = query.get('id', [None])[0] if method == 'GET' else (payload or {}).get('id')
        action = route.rsplit('/', 1)[-1]
        if method == 'GET' and action == 'download':
            with h.store().session() as directory:
                inbox = SpreadsheetInbox(directory)
                service._principal(h, inbox)
                with inbox.connect() as db:
                    state = service._get(db, key)
                report = (state or {}).get('report')
                if not report or report['file_hash'] != query.get('sha256', [''])[0]:
                    raise sf.ReviewError('This held report is not the current saved artifact. Reload its review receipt.')
                raw = files_for(directory).get(report['artifact_path'])
                if sf.digest(raw) != report['file_hash']:
                    raise sf.ReviewError('Saved report failed its integrity check.')
                if len(raw) > sf.MAX_RESPONSE_BYTES:
                    raise sf.ReviewError('This saved report exceeds the private pilot download limit. Its saved bytes are unchanged. No report was released.')
            return h.send(200, raw, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                          {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                           'Content-Disposition': 'attachment; filename="' + report['filename'] + '"'})
        if method == 'GET' and action == 'photo':
            sha = query.get('sha256', [''])[0]
            with h.store().session() as directory:
                inbox = SpreadsheetInbox(directory)
                service._principal(h, inbox)
                with inbox.connect() as db:
                    state = service._get(db, key)
                if not state:
                    raise sf.ReviewError('Review not found.')
                cap = service.load_capture(directory, state)
                photo = next((p for p in cap['photos'] if p.get('sha256') == sha and p.get('status') == 'supported'), None)
                if not photo:
                    raise sf.ReviewError('Photo is not part of this source snapshot.')
                raw = files_for(directory).get('uploads/source-first-photo-' + sha + '.bin')
                if sf.digest(raw) != sha:
                    raise sf.ReviewError('Photo bytes failed their integrity check.')
            return h.send(200, raw, photo['media_type'], {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
        if method == 'GET' and route == '/api/source-review':
            result = service.view(h, key)
        elif method == 'GET':
            return h.fail(404, 'Unknown source-review view.')
        else:
            if not isinstance(payload, dict):
                raise sf.ReviewError('Send a JSON object.')
            if action == 'capture':
                result = service.capture(h, payload)
            elif action == 'refresh':
                result = service.refresh(h, key, payload)
            elif action == 'generate':
                result = service.generate(h, key, payload)
            elif action in ('mapping', 'correct', 'header', 'approve'):
                result = service.mutate(h, key, action, payload)
            else:
                return h.fail(404, 'Unknown source-review operation.')
        return h.send(200, response_json(result))
    except PermissionError as exc:
        return h.fail(403, str(exc))
    except (sf.ReviewError, ValueError, KeyError, TypeError) as exc:
        return h.fail(409, str(exc))
    except StorageUnavailable:
        return h.fail(503, 'The durable outcome is unconfirmed. Reload the source review and its request receipt before retrying.')
    except Exception:
        return h.fail(503, 'The capture or save outcome is unconfirmed. Reload its receipt. No source write or report release was requested.')
