"""Approve a report as final and send it to the client. Shared by the review screen and the API,
so both follow the same rules: sending needs an approval on that exact version, and a second real
send needs again=True. Network calls to Microsoft 365 happen outside any database session."""
import re
from datetime import datetime, timezone

from hosting import mailer
from hosting.review_summary import summarize
from hosting.spreadsheet_inbox import SpreadsheetInbox


def _load(store, file_hash):
    with store.session() as directory:
        inbox = SpreadsheetInbox(directory)
        item = inbox.get(file_hash)
        _, data = inbox.original_bytes(file_hash)
        rv = summarize(item, with_issues=False)
        saved = inbox.contacts_for(rv.get('job'))
    return item, data, rv, saved


def client_filename(filename):
    """The attachment name the client sees: no '(fixed)' marker."""
    return re.sub(r'\s*\(fixed( \d+)?\)(?=\.[^.]+$)', '', filename)


def approve(store, file_hash, approver, note='', override=False, withdraw=False):
    with store.session() as directory:
        return SpreadsheetInbox(directory).approve(file_hash, approver, note, override is True, withdraw is True)


def info(store, file_hash):
    item, data, rv, saved = _load(store, file_hash)
    previous, lookup = None, None
    if mailer.configured():
        try:
            previous = mailer.last_sent(rv.get('job'))
        except mailer.MailError as exc:
            lookup = str(exc)
    shift = mailer.shift_of(item)
    subject, body = mailer.compose(rv.get('job'), rv.get('day'), shift, previous)
    to = previous['to'] if previous else (saved.get('to') or [])
    cc = previous['cc'] if previous else (saved.get('cc') or [])
    cc = cc + [a for a in mailer.always_cc() if a.lower() not in {x.lower() for x in to + cc}]
    return dict(enabled=mailer.configured(), sender=mailer.sender(), job=rv.get('job'), day=rv.get('day'), shift=shift,
                previous=previous and {k: previous[k] for k in ('id', 'subject', 'sent_at', 'web_link', 'to', 'cc')}, lookup_error=lookup,
                recipients_from='previous' if previous else ('saved' if saved.get('to') else 'none'),
                to=to, cc=cc, subject=subject, body=body,
                attachment=dict(name=client_filename(item['filename']), size=len(data), too_big=len(data) > mailer.MAX_ATTACHMENT),
                approval=item.get('approval'), sent=item.get('sent') or [])


def send(store, file_hash, payload, defaults=None):
    """payload: mode ('send'|'draft'), to, cc, subject, body, attachment_name, reply_to, again, by.
    defaults: an info() result used for anything the payload leaves out (the API's 'send with the usual wording')."""
    item, data, rv, _ = _load(store, file_hash)
    approval = item.get('approval')
    if not approval:
        raise ValueError('Approve the report before sending it to the client.')
    from hosting.review_summary import same_run
    if item.get('checker_version') != _checker_version() or not same_run(approval.get('run'), item):
        raise ValueError('The checks changed after this report was approved (re-checked, reconciled again, or approved before approvals were tied to a check run). Approve it again before sending.')
    mode = 'draft' if payload.get('mode') == 'draft' else 'send'
    done = [x for x in item.get('sent') or [] if x.get('sent')]
    if done and mode == 'send' and payload.get('again') is not True:
        raise ValueError(f'This report was already sent to the client on {done[-1].get("at", "")[:10]}. Tick "Send again" (again: true) to send it anyway.')
    d = defaults or {}
    pick = lambda key, fallback=None: payload[key] if payload.get(key) not in (None, '', []) else d.get(key, fallback)
    to, cc = pick('to', []), pick('cc', [])
    name = payload.get('attachment_name') or client_filename(item['filename'])
    reply_to = payload.get('reply_to') if isinstance(payload.get('reply_to'), str) else (
        (d.get('previous') or {}).get('id') if defaults is not None and payload.get('reply_all', True) else None)
    try:
        out = mailer.deliver(to, cc, pick('subject'), pick('body'), name, data, reply_to=reply_to, send_now=mode == 'send')
    except mailer.MailError as exc:
        raise ValueError(str(exc)) from None
    record = dict(at=datetime.now(timezone.utc).isoformat(), sent=out['sent'], sender=out['sender'],
                  web_link=None if out['sent'] else out.get('web_link'),
                  to=mailer.clean_addresses(to, 'To'), cc=mailer.clean_addresses(cc, 'Cc'),
                  subject=' '.join(str(pick('subject') or '').split()), attachment=name, reply_all=bool(reply_to),
                  by=' '.join(str(payload.get('by') or approval.get('by') or '').split())[:80], version=item.get('version'),
                  via=payload.get('via') or 'app')
    with store.session() as directory:
        inbox = SpreadsheetInbox(directory)
        item = inbox.record_send(file_hash, record)
        inbox.save_contacts(rv.get('job'), record['to'], record['cc'])
    return item, record


def _checker_version():
    from hosting.spreadsheet_inbox import CHECKER_VERSION
    return CHECKER_VERSION
