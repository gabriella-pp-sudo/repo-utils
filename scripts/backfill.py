#!/usr/bin/env python3
"""Backfill — processa todos os emails LinkLei desde START_DATE."""

import os
import json
import imaplib
import email as email_lib
import requests
from datetime import datetime, timedelta, date
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from collections import defaultdict

ASAAS_API_KEY     = os.environ['ASAAS_API_KEY']
LINKLEI_EMAIL     = os.environ['LINKLEI_EMAIL']
LINKLEI_PASSWORD  = os.environ['LINKLEI_PASSWORD']
GMAIL_USER        = os.environ['GMAIL_USER']
GMAIL_APP_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD', '')
SPREADSHEET_ID    = os.environ.get('SPREADSHEET_ID', '')
GOOGLE_SA_JSON    = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON', '')
START_DATE        = os.environ.get('START_DATE', '2026-06-19')

# ── IMAP — todos os emails desde START_DATE ────────────────────────────────────
def get_emails_since(start_date_str):
    dt = datetime.strptime(start_date_str, '%Y-%m-%d')
    since_imap = dt.strftime('%d-%b-%Y')
    mail = imaplib.IMAP4_SSL('imap.gmail.com')
    mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    mail.select('inbox')
    _, msgs = mail.search(None, 'FROM', '"no-reply@app.linklei.com.br"', 'SINCE', since_imap)
    emails = []
    ids = msgs[0].split()
    print(f'  [IMAP] {len(ids)} mensagem(s) encontrada(s) desde {start_date_str}')
    for num in ids:
        _, data = mail.fetch(num, '(RFC822)')
        msg = email_lib.message_from_bytes(data[0][1])
        subject = str(make_header(decode_header(msg.get('Subject', '') or '')))
        try:
            msg_date = parsedate_to_datetime(msg.get('Date', '')).date()
        except Exception:
            msg_date = datetime.utcnow().date()
        emails.append({'subject': subject, 'date': msg_date})
    mail.logout()
    return emails

# ── LinkLei ────────────────────────────────────────────────────────────────────
class _CSRFParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.csrf = None
    def handle_starttag(self, tag, attrs):
        if tag == 'meta':
            d = dict(attrs)
            if d.get('name') == 'csrf-token':
                self.csrf = d.get('content')

def linklei_login():
    sess = requests.Session()
    r = sess.get('https://app.linklei.com.br/login', timeout=20)
    p = _CSRFParser()
    p.feed(r.text)
    if not p.csrf:
        raise RuntimeError('CSRF token não encontrado')

    raw_token = p.csrf
    print(f'  [LinkLei] cookies={[c.name for c in sess.cookies]} raw_token={raw_token[:12]}...')

    xhr_headers = {
        'Accept': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': 'https://app.linklei.com.br/login',
        'Origin': 'https://app.linklei.com.br',
    }

    resp = sess.post(
        'https://app.linklei.com.br/login',
        json={'email': LINKLEI_EMAIL, 'password': LINKLEI_PASSWORD},
        headers={**xhr_headers, 'X-CSRF-TOKEN': raw_token},
        timeout=20,
    )
    print(f'  [LinkLei] login HTTP {resp.status_code} | ct={resp.headers.get("content-type","?")}')

    if resp.status_code == 419:
        resp = sess.post(
            'https://app.linklei.com.br/login',
            data={'email': LINKLEI_EMAIL, 'password': LINKLEI_PASSWORD, '_token': raw_token},
            headers=xhr_headers, timeout=20,
        )
        print(f'  [LinkLei] fallback form+_token: HTTP {resp.status_code}')

    if resp.status_code not in (200, 201):
        print(f'  [LinkLei] erro: {resp.text[:500]}')
        return sess, None

    ct = resp.headers.get('content-type', '')
    data = resp.json() if ct.startswith('application/json') else {}
    if data:
        print(f'  [LinkLei] resposta JSON keys: {list(data.keys())}')
    user_data = data.get('user_data', {}) if isinstance(data, dict) else {}
    if user_data and isinstance(user_data, dict):
        print(f'  [LinkLei] user_data keys: {list(user_data.keys())}')
    api_token = (
        data.get('api-token') or data.get('token') or data.get('access_token')
        or (user_data.get('api_token') if isinstance(user_data, dict) else None)
        or (user_data.get('api-token') if isinstance(user_data, dict) else None)
        or (user_data.get('token') if isinstance(user_data, dict) else None)
        or (user_data.get('access_token') if isinstance(user_data, dict) else None)
        or sess.cookies.get('api-token')
    )

    import re as _re
    from urllib.parse import unquote as _unquote

    # 2) seguir redirect para capturar mais cookies/token
    redirect_url = data.get('redirect', '') if isinstance(data, dict) else ''
    if not api_token and redirect_url:
        try:
            dash = sess.get(redirect_url if redirect_url.startswith('http')
                            else f'https://app.linklei.com.br{redirect_url}', timeout=20)
            print(f'  [LinkLei] redirect: HTTP {dash.status_code} | cookies={[c.name for c in sess.cookies]}')
            m = _re.search(r'["\']api[_-]token["\']\s*:\s*["\']([A-Za-z0-9|_\-\.]{20,})["\']', dash.text)
            if m:
                api_token = m.group(1)
                print('  [LinkLei] token encontrado no HTML ✓')
            api_token = api_token or sess.cookies.get('api-token')
        except Exception as ex:
            print(f'  [LinkLei] erro no redirect: {ex}')

    # 3) XSRF-TOKEN URL-decoded como Bearer
    if not api_token:
        xsrf_val = sess.cookies.get('XSRF-TOKEN', '')
        if xsrf_val:
            api_token = _unquote(xsrf_val)
            print(f'  [LinkLei] usando XSRF-TOKEN URL-decoded como bearer: {api_token[:16]}...')

    ud = user_data if isinstance(user_data, dict) else {}
    print(f'  [LinkLei] plan_is_free={ud.get("plan_is_free")} slug={ud.get("link_slug") or ud.get("slug")}')
    print(f'  [LinkLei] modo auth: {"bearer" if api_token else "sem token"}')
    return sess, api_token or 'SESSION', ud

def create_task(sess, api_token, title, deadline, user_data=None):
    ud = user_data or {}
    slug = ud.get('link_slug') or ud.get('slug', '')

    headers = {
        'Accept': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': 'https://app.linklei.com.br/tarefas',
        'Origin': 'https://app.linklei.com.br',
    }
    if api_token and api_token != 'SESSION':
        headers['Authorization'] = f'Bearer {api_token}'
    csrf_raw = sess.cookies.get('csrf_cookie_name', '')
    if csrf_raw:
        headers['X-CSRF-TOKEN'] = csrf_raw
    xsrf = sess.cookies.get('XSRF-TOKEN', '')
    if xsrf:
        headers['X-XSRF-TOKEN'] = xsrf

    for url in [
        'https://app.linklei.com.br/api/v1/workspace/user-task/new',
        *([ f'https://app.linklei.com.br/api/v1/{slug}/user-task/new' ] if slug else []),
        'https://app.linklei.com.br/api/v1/user-task/new',
    ]:
        resp = sess.post(url, json={'title': title, 'deadline': deadline},
                         headers=headers, timeout=20)
        if resp.status_code in (200, 201):
            return resp.status_code
        ct = resp.headers.get('content-type', '')
        err = resp.json() if ct.startswith('application/json') else resp.text[:200]
        print(f'    {url.split("/api/")[1]}: HTTP {resp.status_code} → {err}')
        if resp.status_code not in (401, 403, 404):
            break

    return resp.status_code

# ── Asaas (snapshot atual) ─────────────────────────────────────────────────────
def get_asaas_snapshot():
    h = {'access_token': ASAAS_API_KEY}
    today = datetime.utcnow().strftime('%Y-%m-%d')
    r = requests.get('https://api.asaas.com/v3/payments',
                     params={'status': 'OVERDUE', 'limit': 50}, headers=h, timeout=20)
    overdue = r.json().get('totalCount', 0)
    r = requests.get('https://api.asaas.com/v3/payments',
                     params={'status': 'RECEIVED', 'paymentDate': today, 'limit': 50},
                     headers=h, timeout=20)
    received = r.json().get('totalCount', 0)
    r = requests.get('https://api.asaas.com/v3/finance/balance', headers=h, timeout=20)
    bal = r.json()
    balance = bal.get('balance', bal.get('totalBalance', 0))
    return overdue, received, balance

# ── Google Sheets ──────────────────────────────────────────────────────────────
def update_spreadsheet(rows):
    if not GOOGLE_SA_JSON or not SPREADSHEET_ID:
        print('  [Sheets] GOOGLE_SERVICE_ACCOUNT_JSON não configurado — pulando')
        return
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_info(
            json.loads(GOOGLE_SA_JSON),
            scopes=['https://www.googleapis.com/auth/spreadsheets',
                    'https://www.googleapis.com/auth/drive'],
        )
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = sh.worksheet('Relatório Diário')
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title='Relatório Diário', rows=1000, cols=6)
            ws.append_row(['Data', 'Atraso', 'Recebido Hoje', 'Saldo Asaas', 'Movimentações', 'Tarefas Criadas'])
        for row in rows:
            ws.append_row(row)
            print(f'  [Sheets] {row[0]} adicionado ✓')
    except Exception as e:
        print(f'  [Sheets] Erro: {e}')

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print(f'\n=== BACKFILL desde {START_DATE} até hoje ===\n')

    # 1. Emails
    print('[1/3] Lendo emails LinkLei...')
    emails = get_emails_since(START_DATE)
    by_date = defaultdict(list)
    for e in emails:
        by_date[e['date']].append(e)
    datas = sorted(by_date.keys())
    print(f'  Datas com emails: {[str(d) for d in datas]}')

    # 2. Tarefas LinkLei — prazo = data do email + 4 dias
    print('\n[2/3] Criando tarefas no LinkLei...')
    if emails:
        try:
            sess, api_token, ud = linklei_login()
            if api_token:
                criadas = 0
                for e in emails:
                    deadline = (datetime.combine(e['date'], datetime.min.time()) + timedelta(days=4)).strftime('%Y-%m-%d')
                    status = create_task(sess, api_token, e['subject'], deadline, ud)
                    print(f'  HTTP {status} [{e["date"]}] prazo {deadline}: {e["subject"][:60]}')
                    if status in (200, 201):
                        criadas += 1
                print(f'  {criadas}/{len(emails)} tarefas criadas ✓')
            else:
                print('  Sem token — tarefas não criadas')
        except Exception as ex:
            print(f'  Erro: {ex}')
    else:
        print('  Nenhum email encontrado no período')

    # 3. Planilha — uma linha por dia do período
    print('\n[3/3] Atualizando planilha Google Sheets...')
    overdue, received, balance = get_asaas_snapshot()
    sheets_rows = []
    start_dt = datetime.strptime(START_DATE, '%Y-%m-%d').date()
    today_dt = datetime.utcnow().date()
    cur = start_dt
    while cur <= today_dt:
        day_emails = by_date.get(cur, [])
        date_br = cur.strftime('%d/%m/%Y')
        mov = ' | '.join(e['subject'] for e in day_emails) if day_emails else '—'
        sheets_rows.append([date_br, overdue, received, f'R$ {balance}', mov, len(day_emails)])
        cur += timedelta(days=1)
    update_spreadsheet(sheets_rows)

    print('\n✓ Backfill concluído.')

if __name__ == '__main__':
    main()
