"""Send an approved report to the client from the data@ppgquality.com mailbox (Microsoft Graph).

Follows the end-of-shift routine: Reply All on the most recent report sent for the SAME job number
(so the recipient list is never typed from memory), subject "<JOB>| PPG Quality End-of-Shift Report -
M.DD.YY <shift> Shift" with "Re:" removed, body = last time's greeting and sentence with only the date
and shift changed, Lauren on Cc, today's file attached. "Save as draft" leaves it in Drafts for review;
"Send" sends it. Nothing internal (check results, scan times, inspector names) goes in the email.

Uses the same app registration as the SharePoint lookup. Needs the Microsoft Graph application
permissions Mail.ReadWrite (find the last report, create the draft) and Mail.Send, with admin consent.
"""
import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

from hosting import sharepoint

DEFAULT_SENDER = 'data@ppgquality.com'
DEFAULT_CC = 'lauren.mcguire@ppgquality.com'
MAX_ATTACHMENT = 3 * 1024 * 1024  # Graph takes up to 4 MB per request; base64 adds a third
MAX_RECIPIENTS = 30
EMAIL = re.compile(r'^[A-Za-z0-9._%+\'-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$')
DATE_TOKEN = re.compile(r'\b\d{1,2}[./-]\d{1,2}[./-](?:\d{4}|\d{2})\b')
SHIFT_TOKEN = re.compile(r'\b(1st|2nd|3rd|first|second|third)(\s+shift)\b', re.I)
SHIFT_WORDS = {'first': '1st', 'second': '2nd', 'third': '3rd'}
QUOTE_START = re.compile(r'^\s*(From:\s|-----\s*Original Message|_{8,}|On .+ wrote:)', re.M)


class MailError(RuntimeError):
    pass


def sender():
    return (os.environ.get('MAIL_SENDER') or DEFAULT_SENDER).strip()


def always_cc():
    raw = os.environ.get('MAIL_ALWAYS_CC')
    return [a for a in re.split(r'[;,\s]+', DEFAULT_CC if raw is None else raw) if EMAIL.match(a)]


def configured():
    return sharepoint.configured() and bool(EMAIL.match(sender()))


def clean_addresses(values, label):
    if isinstance(values, str):
        values = re.split(r'[;,\s]+', values)
    if not isinstance(values, list):
        raise MailError(f'{label}: send a list of email addresses.')
    out, seen = [], set()
    for v in values:
        v = str(v or '').strip().strip('<>').strip()
        if not v:
            continue
        if not EMAIL.match(v) or len(v) > 254:
            raise MailError(f'"{v}" is not a valid email address ({label}).')
        if v.lower() not in seen:
            seen.add(v.lower()); out.append(v)
    return out


# ---------------------------------------------------------------- wording
def mdy(day):
    """2026-09-18 -> 9.18.26 (the date style used in report emails)."""
    try:
        d = date.fromisoformat(str(day)[:10])
    except ValueError:
        return None
    return f'{d.month}.{d.day:02d}.{d.year % 100:02d}'


def shift_of(result):
    """'1st' / '2nd' / '3rd' when the report names one shift, else None."""
    identity = result.get('report_identity') or {}
    texts = [str(identity.get('shift') or ''), result.get('filename') or '']
    found = set()
    for t in texts:
        for m in SHIFT_TOKEN.finditer(t):
            found.add(SHIFT_WORDS.get(m.group(1).lower(), m.group(1).lower()))
        m = re.fullmatch(r'\s*(1st|2nd|3rd|1|2|3)\s*', t)
        if m:
            found.add({'1': '1st', '2': '2nd', '3': '3rd'}.get(m.group(1), m.group(1)))
    return found.pop() if len(found) == 1 else None


def _swap(text, new_date, new_shift):
    if new_date:
        text = DATE_TOKEN.sub(new_date, text)
    if new_shift:
        text = SHIFT_TOKEN.sub(lambda m: new_shift + m.group(2), text)
    return text


def compose(job, day, shift, previous=None):
    """Subject and body for this report, from the last sent report when there is one."""
    when = mdy(day) or ''
    pattern = f'{job or "JOB"}| PPG Quality End-of-Shift Report - {when}' + (f' {shift} Shift' if shift else '')
    if previous and previous.get('subject'):
        subject = re.sub(r'^\s*((re|fw|fwd)\s*:\s*)+', '', previous['subject'], flags=re.I)
        subject = _swap(subject, when, shift) if DATE_TOKEN.search(subject) else pattern
    else:
        subject = pattern
    body = ''
    if previous and previous.get('body'):
        top = QUOTE_START.split(previous['body'], maxsplit=1)[0].strip()
        body = _swap(top, when, shift)
    if not body:
        body = ('Good Afternoon,\n\nPlease see the attached PPG Quality End of Shift Data and Billing Report'
                + (f' completed on {shift} shift {when}.' if shift else f' for {when}.'))
    return ' '.join(subject.split()), body


# ---------------------------------------------------------------- Graph calls
def _call(method, path, body=None, opener=None, prefer=None, want=True):
    try:
        token = sharepoint._token_value(opener)
    except (sharepoint.SharePointError, sharepoint.SharePointNotConfigured) as exc:
        raise MailError(str(exc).replace('SharePoint lookup', 'mail sender')) from None
    headers = {'Authorization': 'Bearer ' + token}
    if body is not None:
        headers['Content-Type'] = 'application/json'
    if prefer:
        headers['Prefer'] = prefer
    request = urllib.request.Request(path if path.startswith('https://') else sharepoint.GRAPH + path,
                                     data=(b'' if method in ('POST', 'PATCH') else None) if body is None else json.dumps(body).encode(), method=method, headers=headers)
    try:
        with sharepoint._open(request, opener) as response:
            data = response.read()
            return json.loads(data) if want and data else None
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise MailError(f'Microsoft 365 would not let the app use the {sender()} mailbox (HTTP {exc.code}). '
                            'An admin needs to give the app registration the Microsoft Graph application permissions '
                            '"Mail.ReadWrite" and "Mail.Send" and grant admin consent.') from None
        if exc.code == 404:
            raise MailError(f'The mailbox {sender()} (or the earlier email) was not found in Microsoft 365.') from None
        if exc.code == 413:
            raise MailError('The report is too large to email from here. Download it and send it from Outlook.') from None
        raise MailError(f'Microsoft 365 answered HTTP {exc.code}. Nothing was sent; try again.') from None
    except (urllib.error.URLError, TimeoutError):
        raise MailError('Could not reach Microsoft 365. Check Drafts and Sent Items in the mailbox before trying again.') from None


def _box():
    return '/users/' + urllib.parse.quote(sender())


def _people(items):
    return [((x or {}).get('emailAddress') or {}).get('address') for x in items or [] if ((x or {}).get('emailAddress') or {}).get('address')]


def last_sent(job, opener=None):
    """The most recent email in Sent Items whose subject has this job number, or None."""
    if not job:
        return None
    query = urllib.parse.urlencode({'$search': f'"{job}"', '$top': '25',
                                    '$select': 'id,subject,sentDateTime,toRecipients,ccRecipients,body,webLink'})
    data = _call('GET', f'{_box()}/mailFolders/sentitems/messages?{query}', opener=opener, prefer='outlook.body-content-type="text"') or {}
    key = re.sub(r'[^A-Z0-9]', '', job.upper())
    hits = [m for m in data.get('value') or [] if key and key in re.sub(r'[^A-Z0-9]', '', (m.get('subject') or '').upper())]
    if not hits:
        return None
    m = max(hits, key=lambda x: x.get('sentDateTime') or '')
    return dict(id=m['id'], subject=m.get('subject') or '', sent_at=m.get('sentDateTime'), web_link=m.get('webLink'),
                to=_people(m.get('toRecipients')), cc=_people(m.get('ccRecipients')),
                body=((m.get('body') or {}).get('content') or '')[:20000])


def deliver(to, cc, subject, text, attachment_name, attachment_bytes, reply_to=None, send_now=True, opener=None):
    """Create the email in the sender mailbox (as Reply All on reply_to when given), attach the report,
    then send it or leave it in Drafts. Returns dict(sender, draft_id, web_link, sent)."""
    to, cc = clean_addresses(to, 'To'), clean_addresses(cc or [], 'Cc')
    if not to:
        raise MailError('Add at least one client email address.')
    if len(to) + len(cc) > MAX_RECIPIENTS:
        raise MailError(f'Send to {MAX_RECIPIENTS} people or fewer at a time.')
    subject = ' '.join(str(subject or '').split())[:250]
    if not subject:
        raise MailError('Add a subject line.')
    name = ' '.join(str(attachment_name or '').split())
    if not name or any(c in name for c in '/\\:*?"<>|'):
        raise MailError('Give the attachment a plain file name.')
    if len(attachment_bytes) > MAX_ATTACHMENT:
        raise MailError(f'The report is {len(attachment_bytes) / 1048576:.1f} MB; attachments from here are limited to 3 MB. Download it and send it from Outlook.')
    if not configured():
        raise MailError('Sending is not set up yet (the Microsoft app registration or the sender mailbox is missing).')
    text = str(text or '')[:20000]
    people = {'toRecipients': [{'emailAddress': {'address': a}} for a in to],
              'ccRecipients': [{'emailAddress': {'address': a}} for a in cc]}
    if reply_to:
        draft = _call('POST', f'{_box()}/messages/{urllib.parse.quote(reply_to)}/createReplyAll', {'comment': text}, opener=opener) or {}
        _call('PATCH', f'{_box()}/messages/{urllib.parse.quote(draft["id"])}', dict(people, subject=subject), opener=opener)
    else:
        draft = _call('POST', f'{_box()}/messages', dict(people, subject=subject, body={'contentType': 'Text', 'content': text}), opener=opener) or {}
    if not draft.get('id'):
        raise MailError('Microsoft 365 did not create the email. Nothing was sent; try again.')
    did = urllib.parse.quote(draft['id'])
    ctype = ('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' if name.lower().endswith('.xlsx') else 'text/csv')
    _call('POST', f'{_box()}/messages/{did}/attachments', {'@odata.type': '#microsoft.graph.fileAttachment', 'name': name,
          'contentType': ctype, 'contentBytes': base64.b64encode(attachment_bytes).decode()}, opener=opener)
    if send_now:
        _call('POST', f'{_box()}/messages/{did}/send', opener=opener, want=False)
    return dict(sender=sender(), draft_id=draft['id'], web_link=draft.get('webLink'), sent=bool(send_now))


def send_plain(to, subject, text, opener=None):
    """A short plain-text email from the sender mailbox, no attachment (sign-in links)."""
    to = clean_addresses(to, 'To')
    if not to:
        raise MailError('No email address.')
    if not configured():
        raise MailError('Sending is not set up yet (the Microsoft app registration or the sender mailbox is missing).')
    message = {'subject': ' '.join(str(subject).split())[:250], 'body': {'contentType': 'Text', 'content': str(text)[:5000]},
               'toRecipients': [{'emailAddress': {'address': a}} for a in to]}
    _call('POST', f'{_box()}/sendMail', {'message': message, 'saveToSentItems': True}, opener=opener)
    return dict(sender=sender(), sent=True)
