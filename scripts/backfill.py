#!/usr/bin/env python3
"""Backfill — processa todos os emails LinkLei desde START_DATE."""

import os
import json
import imaplib
import email as email_lib
import requests
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from collections import defaultdict

ASAAS_API_KEY      = os.environ['ASAAS_API_KEY']
LINKLEI_EMAIL      = os.environ['LINKLEI_EMAIL']
LINKLEI_PASSWORD   = os.environ['LINKLEI_PASSWORD']
GMAIL_USER         = os.environ['GMAIL_USER']
GMAIL_APP_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD', '')
SPREADSHEET_ID     = os.environ.get('SPREADSHEET_ID', '')
GOOGLE_SA_JSON     = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON', '')
START_DATE         = os.environ.get('START_DATE', '2026-06-19')

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

# ── LinkLei — Playwright para capturar Bearer token ───────────────────────────
def get_linklei_token():
    """Faz login real via browser headless e intercepta o Bearer token do SPA."""
    from playwright.sync_api import sync_playwright

    captured = {'token': None}

    def on_request(req):
        if captured['token']:
            return
        auth = req.headers.get('authorization', '')
        if auth.startswith('Bearer ') and '/api/v1/' in req.url:
            t = auth[7:]
            if len(t) > 20:
                captured['token'] = t
                print(f'  [Playwright] token capturado ({req.url.split("/api/v1/")[-1][:40]}) ✓')

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage'],
        )
        ctx = browser.new_context()
        page = ctx.new_page()
        page.on('request', on_request)

        print('  [Playwright] abrindo página de login...')
        page.goto('https://app.linklei.com.br/login', timeout=30000)
        page.wait_for_load_state('networkidle', timeout=15000)

        page.fill('input[type="email"]', LINKLEI_EMAIL)
        page.fill('input[type="password"]', LINKLEI_PASSWORD)
        page.click('button[type="submit"]')

        page.wait_for_load_state('networkidle', timeout=20000)
        page.wait_for_timeout(3000)
        print(f'  [Playwright] URL pós-login: {page.url}')

        if not captured['token']:
            print('  [Playwright] token não encontrado após login — navegando para /tarefas...')
            page.goto('https://app.linklei.com.br/tarefas', timeout=20000)
            page.wait_for_load_state('networkidle', timeout=15000)
            page.wait_for_timeout(3000)

        if not captured['token']:
            print('  [Playwright] tentando /dashboard...')
            page.goto('https://app.linklei.com.br/dashboard', timeout=20000)
            page.wait_for_load_state('networkidle', timeout=15000)
            page.wait_for_timeout(2000)

        browser.close()

    if captured['token']:
        print(f'  [Playwright] token: {captured["token"][:16]}...')
    else:
        print('  [Playwright] AVISO: token não capturado')

    return captured['token']

# ── LinkLei — criar tarefa via API ────────────────────────────────────────────
def create_task(bearer_token, title, deadline):
    if not bearer_token:
        return 0

    headers = {
        'Accept': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
        'Authorization': f'Bearer {bearer_token}',
        'Referer': 'https://app.linklei.com.br/tarefas',
        'Origin': 'https://app.linklei.com.br',
    }

    for url in [
        'https://app.linklei.com.br/api/v1/workspace/user-task/new',
        'https://app.linklei.com.br/api/v1/user-task/new',
    ]:
        resp = requests.post(url, json={'title': title, 'deadline': deadline},
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
    criadas = 0
    if emails:
        try:
            bearer_token = get_linklei_token()
            if bearer_token:
                for e in emails:
                    deadline = (datetime.combine(e['date'], datetime.min.time()) + timedelta(days=4)).strftime('%Y-%m-%d')
                    status = create_task(bearer_token, e['subject'], deadline)
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

    print(f'\n✓ Backfill concluído. {criadas} tarefa(s) criada(s).')

if __name__ == '__main__':
    main()
