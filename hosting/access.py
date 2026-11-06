"""Who can use the verifier, and which clients each person can see.

Admins see everything and manage people. Members see only the clients assigned to them: every report, search,
download, weekly/monthly page and overview is filtered on the server. People sign in with their PPG Microsoft
account or a one-time email link. Until an admin turns on "Require sign-in", the app stays behind Vercel's sign-in
and treats whoever gets through as the owner, so setting this up can never lock anyone out.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ROLES = ('admin', 'member')
COOKIE = 'ppg_session'
STATE_COOKIE = 'ppg_signin_state'
SESSION_DAYS = 30
LINK_MINUTES = 30            # emailed sign-in links
INVITE_HOURS = 72            # links an admin copies and sends
EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
OWNER = dict(id='vercel', email=None, name='You (through Vercel sign-in)', role='admin', clients=[], virtual=True)


class AccessError(Exception):
    pass


def _now():
    return datetime.now(timezone.utc)


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _clean_email(email):
    email = str(email or '').strip().lower()
    if not EMAIL.match(email) or len(email) > 200:
        raise AccessError('Enter a valid email address.')
    return email


def _user(row):
    if not row:
        return None
    return dict(id=row['id'], email=row['email'], name=row['name'] or '', role=row['role'],
                clients=json.loads(row['clients_json'] or '[]'), created_at=row['created_at'],
                last_login=row['last_login'], disabled=bool(row['disabled_at']))


# ---------------------------------------------------------------- settings
def required(inbox):
    return bool(inbox.setting('access_required', False))


def set_required(inbox, on, actor):
    if on and (not actor or actor.get('virtual') or actor.get('role') != 'admin'):
        raise AccessError('Sign in yourself first (with Microsoft or your own sign-in link), then turn this on, so you can never be locked out.')
    inbox.set_setting('access_required', bool(on))
    return bool(on)


# ---------------------------------------------------------------- people
def list_users(inbox):
    with inbox.connect() as db:
        return [_user(r) for r in db.execute('SELECT * FROM users WHERE disabled_at IS NULL ORDER BY role, lower(name), email').fetchall()]


def get_user(inbox, user_id=None, email=None):
    with inbox.connect() as db:
        if user_id:
            row = db.execute('SELECT * FROM users WHERE id=? AND disabled_at IS NULL', (str(user_id),)).fetchone()
        else:
            row = db.execute('SELECT * FROM users WHERE email=? AND disabled_at IS NULL', (_clean_email(email),)).fetchone()
    return _user(row)


def save_user(inbox, data, valid_clients=None):
    email = _clean_email(data.get('email'))
    role = data.get('role') if data.get('role') in ROLES else 'member'
    name = ' '.join(str(data.get('name') or '').split())[:80]
    clients = sorted({str(c) for c in (data.get('clients') or []) if isinstance(c, str) and c.strip()})[:200]
    if valid_clients is not None:
        clients = [c for c in clients if c in valid_clients or c == '_none']
    with inbox.connect() as db:
        existing = db.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()
        if data.get('id') and (not existing or existing['id'] != data['id']):
            other = db.execute('SELECT id FROM users WHERE id=?', (str(data['id']),)).fetchone()
            if not other:
                raise AccessError('That person was removed. Reload the page.')
            if existing:
                raise AccessError(f'{email} is already on the list.')
            db.execute('UPDATE users SET email=?, name=?, role=?, clients_json=?, disabled_at=NULL WHERE id=?',
                       (email, name, role, json.dumps(clients), str(data['id'])))
            uid = str(data['id'])
        elif existing:
            db.execute('UPDATE users SET name=?, role=?, clients_json=?, disabled_at=NULL WHERE id=?', (name, role, json.dumps(clients), existing['id']))
            uid = existing['id']
        else:
            uid = secrets.token_hex(8)
            db.execute('INSERT INTO users (id, email, name, role, clients_json, created_at) VALUES (?,?,?,?,?,?)',
                       (uid, email, name, role, json.dumps(clients), _now().isoformat()))
    return get_user(inbox, uid)


def remove_user(inbox, user_id, actor):
    if actor and actor.get('id') == user_id:
        raise AccessError("You can't remove yourself.")
    with inbox.connect() as db:
        admins = db.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND disabled_at IS NULL AND id<>?", (str(user_id),)).fetchone()[0]
        if admins == 0 and required(inbox):
            raise AccessError('Keep at least one admin while sign-in is required.')
        db.execute('UPDATE users SET disabled_at=? WHERE id=?', (_now().isoformat(), str(user_id)))
        db.execute('DELETE FROM sessions WHERE user_id=?', (str(user_id),))
        db.execute('DELETE FROM login_tokens WHERE user_id=?', (str(user_id),))


# ---------------------------------------------------------------- sessions and sign-in links
def start_session(inbox, user):
    token = secrets.token_urlsafe(32)
    now = _now()
    with inbox.connect() as db:
        db.execute('INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)',
                   (_hash(token), user['id'], now.isoformat(), (now + timedelta(days=SESSION_DAYS)).isoformat()))
        db.execute('UPDATE users SET last_login=? WHERE id=?', (now.isoformat(), user['id']))
    return token


def session_user(inbox, token):
    if not token or len(token) > 200:
        return None
    with inbox.connect() as db:
        row = db.execute('SELECT user_id, expires_at FROM sessions WHERE token_hash=?', (_hash(token),)).fetchone()
        if not row or row['expires_at'] < _now().isoformat():
            return None
        u = db.execute('SELECT * FROM users WHERE id=? AND disabled_at IS NULL', (row['user_id'],)).fetchone()
    return _user(u)


def end_session(inbox, token):
    if token:
        with inbox.connect() as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (_hash(token),))


def sign_in_link(inbox, user, minutes):
    token = secrets.token_urlsafe(32)
    now = _now()
    with inbox.connect() as db:
        db.execute('INSERT INTO login_tokens (token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)',
                   (_hash(token), user['id'], now.isoformat(), (now + timedelta(minutes=minutes)).isoformat()))
    return token


def use_link(inbox, token):
    if not token or len(token) > 200:
        raise AccessError('That sign-in link is not valid.')
    now = _now().isoformat()
    with inbox.connect() as db:
        row = db.execute('SELECT user_id, expires_at, used_at FROM login_tokens WHERE token_hash=?', (_hash(token),)).fetchone()
        if not row:
            raise AccessError('That sign-in link is not valid.')
        if row['used_at']:
            raise AccessError('That sign-in link was already used. Ask for a new one.')
        if row['expires_at'] < now:
            raise AccessError('That sign-in link has expired. Ask for a new one.')
        db.execute('UPDATE login_tokens SET used_at=? WHERE token_hash=?', (now, _hash(token)))
    user = get_user(inbox, row['user_id'])
    if not user:
        raise AccessError('Your access was removed. Ask an admin to add you again.')
    return user


def cookie(name, value, max_age, secure):
    return f'{name}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}' + ('; Secure' if secure else '')


def read_cookie(header, name):
    for part in (header or '').split(';'):
        k, _, v = part.strip().partition('=')
        if k == name:
            return v
    return None


# ---------------------------------------------------------------- which clients a person can see
def client_of(job, clients):
    """The client key a job belongs to, by job number (as the app's client tabs do)."""
    if not job:
        return None
    up = str(job).upper()
    nums = set(re.findall(r'\d{3,}', up))
    for c in clients or []:
        for j in c.get('jobs', []):
            if j.get('job', '').upper() == up or nums & set(j.get('numbers') or []):
                return c['key']
    return None


def can_see_job(user, job, clients):
    if not user:
        return False
    if user.get('role') == 'admin':
        return True
    key = client_of(job, clients)
    return (key or '_none') in set(user.get('clients') or [])


# ---------------------------------------------------------------- Microsoft sign-in (PPG's Entra app)
def microsoft_ready():
    return all(os.environ.get(k) for k in ('MS_TENANT_ID', 'MS_CLIENT_ID', 'MS_CLIENT_SECRET'))


def microsoft_url(redirect_uri, state):
    q = dict(client_id=os.environ['MS_CLIENT_ID'], response_type='code', redirect_uri=redirect_uri, response_mode='query',
             scope='openid email profile', state=state, prompt='select_account')
    return f'https://login.microsoftonline.com/{os.environ["MS_TENANT_ID"]}/oauth2/v2.0/authorize?' + urllib.parse.urlencode(q)


def microsoft_email(code, redirect_uri, opener=None):
    """Trade the code for an ID token (directly with Microsoft over TLS) and return the signed-in email."""
    body = urllib.parse.urlencode(dict(client_id=os.environ['MS_CLIENT_ID'], client_secret=os.environ['MS_CLIENT_SECRET'], grant_type='authorization_code',
                                       code=code, redirect_uri=redirect_uri, scope='openid email profile')).encode()
    req = urllib.request.Request(f'https://login.microsoftonline.com/{os.environ["MS_TENANT_ID"]}/oauth2/v2.0/token', data=body,
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with (opener or urllib.request.urlopen)(req, timeout=20) as r:
            out = json.loads(r.read())
    except Exception as exc:
        raise AccessError('Microsoft sign-in did not finish. Try again.') from exc
    token = out.get('id_token')
    if not token:
        raise AccessError(out.get('error_description') or 'Microsoft sign-in did not finish. Try again.')
    payload = token.split('.')[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
    if claims.get('aud') != os.environ['MS_CLIENT_ID'] or claims.get('tid') != os.environ['MS_TENANT_ID']:
        raise AccessError('That Microsoft account is not a PPG account.')
    email = claims.get('email') or claims.get('preferred_username') or claims.get('upn')
    return _clean_email(email), claims.get('name')
