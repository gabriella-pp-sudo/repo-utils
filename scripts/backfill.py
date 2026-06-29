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
def _find_selector(page, selectors, state='visible', timeout=5000):
    """Retorna o primeiro seletor encontrado no estado especificado."""
    from playwright.sync_api import TimeoutError as PWTimeout
    for sel in selectors:
        try:
            page.wait_for_selector(sel, state=state, timeout=timeout)
            return sel
        except PWTimeout:
            continue
    return None

def _type_and_trigger(page, selector, text):
    """Clica, digita e dispara eventos React/Vue no campo."""
    page.click(selector)
    page.evaluate(f"document.querySelector('{selector}').value = ''")
    page.type(selector, text, delay=60)
    page.evaluate(
        "sel => { const el = document.querySelector(sel);"
        " el.dispatchEvent(new Event('input', {bubbles:true}));"
        " el.dispatchEvent(new Event('change', {bubbles:true})); }",
        selector
    )

def get_linklei_token():
    """Faz login real via browser headless e intercepta o Bearer token do SPA."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

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
        print(f'  [Playwright] título: {page.title()} | URL: {page.url}')

        # Imprimir todos os inputs da página logo após carregar (diagnóstico)
        all_inputs = page.eval_on_selector_all('input', 'els => els.map(e => e.outerHTML)')
        print(f'  [Playwright] inputs na página: {all_inputs}')

        # ── Preencher email com type() para disparar eventos React ───────────
        email_sel = _find_selector(page, [
            'input[type="email"]',
            'input[name="email"]',
            'input[placeholder*="email" i]',
            'input[placeholder*="e-mail" i]',
        ], timeout=15000)

        if not email_sel:
            print('  [Playwright] campo email não encontrado')
            browser.close()
            return None

        print(f'  [Playwright] campo email: {email_sel}')
        _type_and_trigger(page, email_sel, LINKLEI_EMAIL)
        page.wait_for_timeout(1000)

        # ── Campo de senha ───────────────────────────────────────────────────
        pwd_selectors = [
            'input[type="password"]',
            'input[name="password"]',
            'input[placeholder*="senha" i]',
            'input[placeholder*="password" i]',
        ]

        # Verificar se a senha existe no DOM (mesmo que hidden)
        pwd_in_dom = _find_selector(page, pwd_selectors, state='attached', timeout=2000)
        pwd_sel = _find_selector(page, pwd_selectors, state='visible', timeout=2000)

        if pwd_in_dom and not pwd_sel:
            print(f'  [Playwright] senha no DOM mas não visível ({pwd_in_dom}) — Tab para desfocar...')
            page.press(email_sel, 'Tab')
            page.wait_for_timeout(800)
            pwd_sel = _find_selector(page, pwd_selectors, state='visible', timeout=3000)

        if not pwd_sel:
            # Aguardar o botão habilitar (React validou o email) e clicar
            print('  [Playwright] aguardando botão submit habilitar...')
            try:
                page.wait_for_selector('button[type="submit"]:not([disabled])', timeout=6000)
                page.click('button[type="submit"]')
                page.wait_for_timeout(2000)
            except PWTimeout:
                # Botão permanece desabilitado — forçar Enter
                print('  [Playwright] Enter no campo email...')
                page.press(email_sel, 'Enter')
                page.wait_for_timeout(2000)
            pwd_sel = _find_selector(page, pwd_selectors, state='visible', timeout=6000)

        if not pwd_sel:
            after_inputs = page.eval_on_selector_all('input', 'els => els.map(e => e.outerHTML)')
            print(f'  [Playwright] inputs após tentativas: {after_inputs}')
            browser.close()
            return None

        print(f'  [Playwright] campo senha: {pwd_sel}')
        _type_and_trigger(page, pwd_sel, LINKLEI_PASSWORD)
        page.wait_for_timeout(500)

        # ── Aguardar submit habilitar e enviar formulário ────────────────────
        print('  [Playwright] aguardando submit...')
        try:
            page.wait_for_selector('button[type="submit"]:not([disabled])', timeout=8000)
            page.click('button[type="submit"]')
        except PWTimeout:
            print('  [Playwright] Enter no campo senha')
            page.press(pwd_sel, 'Enter')

        page.wait_for_load_state('networkidle', timeout=20000)
        page.wait_for_timeout(3000)
        print(f'  [Playwright] URL pós-login: {page.url}')

        if not captured['token']:
            print('  [Playwright] navegando para /tarefas...')
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

# ── LinkLei — buscar team_member_id do usuário logado ─────────────────────────
def get_team_member_id(token):
    """Busca o ID de membro da equipe do usuário logado via API."""
    headers = {
        'Accept': 'application/json',
        'Authorization': f'Bearer {token}',
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': 'https://app.linklei.com.br/tarefas',
    }

    def _search(obj, keys=('team_member_id', 'id_team_member', 'member_id'), depth=0):
        if depth > 5 or not isinstance(obj, (dict, list)):
            return None
        if isinstance(obj, dict):
            for k in keys:
                if k in obj and obj[k]:
                    return obj[k]
            for v in obj.values():
                r = _search(v, keys, depth + 1)
                if r:
                    return r
        elif isinstance(obj, list):
            for item in obj[:10]:
                r = _search(item, keys, depth + 1)
                if r:
                    return r
        return None

    for url in [
        'https://app.linklei.com.br/api/v1/workspace/user',
        'https://app.linklei.com.br/api/v1/workspace/user/me',
        'https://app.linklei.com.br/api/v1/workspace/team-member',
        'https://app.linklei.com.br/api/v1/me',
    ]:
        try:
            r = requests.get(url, headers=headers, timeout=15)
            path = url.split('/api/v1/')[-1]
            print(f'  [API] {path}: HTTP {r.status_code}')
            if r.status_code == 200:
                data = r.json()
                tid = _search(data)
                if tid:
                    print(f'  [API] team_member_id: {tid}')
                    return tid
                top = list(data.keys())[:8] if isinstance(data, dict) else str(data)[:80]
                print(f'  [API] keys: {top}')
        except Exception as ex:
            print(f'  [API] erro: {ex}')

    return None

# ── LinkLei — criar tarefa via API ────────────────────────────────────────────
def create_task(bearer_token, title, start_date, end_date, team_member_ids=None):
    if not bearer_token:
        return 0

    headers = {
        'Accept': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
        'Authorization': f'Bearer {bearer_token}',
        'Referer': 'https://app.linklei.com.br/tarefas',
        'Origin': 'https://app.linklei.com.br',
    }
    payload = {'title': title, 'date': start_date, 'end_date': end_date}
    if team_member_ids:
        payload['team_member_id_list'] = (
            team_member_ids if isinstance(team_member_ids, list) else [team_member_ids]
        )

    for url in [
        'https://app.linklei.com.br/api/v1/workspace/user-task/new',
        'https://app.linklei.com.br/api/v1/user-task/new',
    ]:
        resp = requests.post(url, json=payload, headers=headers, timeout=20)
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

    # 2. Tarefas LinkLei — início = data do email, prazo = data + 4 dias
    print('\n[2/3] Criando tarefas no LinkLei...')
    criadas = 0
    if emails:
        try:
            bearer_token = get_linklei_token()
            if bearer_token:
                team_member_id = get_team_member_id(bearer_token)
                team_ids = [team_member_id] if team_member_id else None
                for e in emails:
                    start_date = e['date'].strftime('%Y-%m-%d')
                    end_date = (datetime.combine(e['date'], datetime.min.time()) + timedelta(days=4)).strftime('%Y-%m-%d')
                    status = create_task(bearer_token, e['subject'], start_date, end_date, team_ids)
                    print(f'  HTTP {status} [{e["date"]}] prazo {end_date}: {e["subject"][:60]}')
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
