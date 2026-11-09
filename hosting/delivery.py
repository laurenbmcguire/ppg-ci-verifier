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


def approve(store, file_hash, approver, note='', override=False, withdraw=False, *, expected_check_hash=None, expected_run=None):
    with store.session() as directory:
        return SpreadsheetInbox(directory).approve(file_hash, approver, note, override is True, withdraw is True,
                                                 expected_check_hash=expected_check_hash, expected_run=expected_run)


def info(store, file_hash):
    item, data, rv, saved = _load(store, file_hash)
    previous, lookup = None, None
    # Delivery is paused: opening approval must not query a mailbox.
    shift = mailer.shift_of(item)
    subject, body = mailer.compose(rv.get('job'), rv.get('day'), shift, previous)
    to = previous['to'] if previous else (saved.get('to') or [])
    cc = previous['cc'] if previous else (saved.get('cc') or [])
    cc = cc + [a for a in mailer.always_cc() if a.lower() not in {x.lower() for x in to + cc}]
    return dict(enabled=False, delivery_paused=True, sender=mailer.sender(), job=rv.get('job'), day=rv.get('day'), shift=shift,
                previous=previous and {k: previous[k] for k in ('id', 'subject', 'sent_at', 'web_link', 'to', 'cc')}, lookup_error=lookup,
                recipients_from='previous' if previous else ('saved' if saved.get('to') else 'none'),
                to=to, cc=cc, subject=subject, body=body,
                attachment=dict(name=client_filename(item['filename']), size=len(data), too_big=len(data) > mailer.MAX_ATTACHMENT),
                approval=item.get('approval'), sent=item.get('sent') or [])


def send(store, file_hash, payload, defaults=None):
    """payload: mode ('send'|'draft'), to, cc, subject, body, attachment_name, reply_to, again, by.
    defaults: an info() result used for anything the payload leaves out (the API's 'send with the usual wording')."""
    # A provider timeout may mean the email was sent. Until a durable send reservation,
    # immutable approved snapshot and reconciliation protocol exist, retries are unsafe.
    # Fail before loading state or contacting Microsoft, including draft creation.
    raise ValueError('Sending and draft creation are paused pending delivery safety checks. No email was sent. Use the approved download for manual delivery.')


def _checker_version():
    from hosting.spreadsheet_inbox import CHECKER_VERSION
    return CHECKER_VERSION
