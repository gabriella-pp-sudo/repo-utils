#!/usr/bin/env python3
"""
Pereira & Ricci — Automação Diária
Relatório financeiro (Asaas), tarefas no LinkLei e atualização de planilha.
"""

import os
import json
import imaplib
import smtplib
import email as email_lib
import requests
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header, make_header
from html.parser import HTMLParser

# ── Configurações ──────────────────────────────────────────────────────────────
ASAAS_API_KEY     = os.environ['ASAAS_API_KEY']
LINKLEI_EMAIL     = os.environ['LINKLEI_EMAIL']
LINKLEI_PASSWORD  = os.environ['LINKLEI_PASSWORD']
GMAIL_USER        = os.environ['GMAIL_USER']
GMAIL_APP_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD', '')
SPREADSHEET_ID    = os.environ.get('SPREADSHEET_ID', '')
GOOGLE_SA_JSON    = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON', '')

TODAY    = datetime.utcnow().strftime('%Y-%m-%d')
TODAY_BR = datetime.utcnow().strftime('%d/%m/%Y')

# ── Asaas ──────────────────────────────────────────────────────────────────────
def get_asaas_data():
    h = {'access_token': ASAAS_API_KEY}
    r = requests.get('https://api.asaas.com/v3/payments',
                     params={'status': 'OVERDUE', 'limit': 50}, headers=h, timeout=20)
    overdue = r.json().get('totalCount', 0)

    r = requests.get('https://api.asaas.com/v3/payments',
                     params={'status': 'RECEIVED', 'paymentDate': TODAY, 'limit': 50},
                     headers=h, timeout=20)
    received = r.json().get('totalCount', 0)

    r = requests.get('https://api.asaas.com/v3/finance/balance', headers=h, timeout=20)
    bal_data = r.json()
    balance = bal_data.get('balance', bal_data.get('totalBalance', 0))

    return overdue, received, balance

# ── Gmail IMAP — ler emails do LinkLei ────────────────────────────────────────
def get_linklei_emails():
    if not GMAIL_APP_PASSWORD:
        print('  [IMAP] GMAIL_APP_PASSWORD não configurado — pulando leitura de emails')
        return []
    try:
        mail = imaplib.IMAP4_SSL('imap.gmail.com')
        mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        mail.select('inbox')
        since = (datetime.utcnow() - timedelta(days=1)).strftime('%d-%b-%Y')
        _, msgs = mail.search(None, 'FROM', '"no-reply@app.linklei.com.br"', 'SINCE', since)
        movements = []
        for num in msgs[0].split()[-20:]:
            _, data = mail.fetch(num, '(RFC822)')
            msg = email_lib.message_from_bytes(data[0][1])
            subject = str(make_header(decode_header(msg.get('Subject', '') or '')))
            date_str = msg.get('Date', '')
            movements.append({'subject': subject, 'date': date_str})
        mail.logout()
        print(f'  [IMAP] {len(movements)} email(s) encontrado(s)')
        return movements
    except Exception as e:
        print(f'  [IMAP] Erro: {e}')
        return []

# ── LinkLei — autenticação ────────────────────────────────────────────────────
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
        raise RuntimeError('CSRF token não encontrado em /login')

    raw_token = p.csrf
    xsrf_cookie = next(
        (sess.cookies.get(c.name) for c in sess.cookies if 'csrf' in c.name.lower()),
        raw_token
    )
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

    # Login OK — extrair token
    import re as _re
    from urllib.parse import unquote as _unquote
    ct = resp.headers.get('content-type', '')
    data = resp.json() if ct.startswith('application/json') else {}
    if data:
        print(f'  [LinkLei] login JSON keys: {list(data.keys())}')
    user_data = data.get('user_data', {}) if isinstance(data, dict) else {}
    redirect_url = data.get('redirect', '') if isinstance(data, dict) else ''

    # 1) token direto no JSON de login
    api_token = (
        data.get('api-token') or data.get('token') or data.get('access_token')
        or (user_data.get('api_token') if isinstance(user_data, dict) else None)
        or (user_data.get('api-token') if isinstance(user_data, dict) else None)
        or sess.cookies.get('api-token')
    )

    # 2) seguir redirect do login — pode setar cookies ou retornar token no HTML
    if not api_token and redirect_url:
        try:
            dash = sess.get(redirect_url if redirect_url.startswith('http')
                            else f'https://app.linklei.com.br{redirect_url}',
                            timeout=20)
            print(f'  [LinkLei] redirect {redirect_url}: HTTP {dash.status_code} '
                  f'| novos cookies={[c.name for c in sess.cookies]}')
            # Procurar token em variáveis JS do dashboard
            m = _re.search(r'["\']api[_-]token["\']\s*:\s*["\']([A-Za-z0-9|_\-\.]{20,})["\']', dash.text)
            if m:
                api_token = m.group(1)
                print(f'  [LinkLei] token encontrado no HTML do dashboard ✓')
            api_token = api_token or sess.cookies.get('api-token')
        except Exception as ex:
            print(f'  [LinkLei] erro no redirect: {ex}')

    # 3) XSRF-TOKEN URL-decoded como Bearer (SPA pattern)
    if not api_token:
        xsrf_val = sess.cookies.get('XSRF-TOKEN', '')
        if xsrf_val:
            api_token = _unquote(xsrf_val)
            print(f'  [LinkLei] usando XSRF-TOKEN URL-decoded como bearer: {api_token[:16]}...')

    ud = user_data if isinstance(user_data, dict) else {}
    print(f'  [LinkLei] plan_is_free={ud.get("plan_is_free")} slug={ud.get("link_slug") or ud.get("slug")}')
    print(f'  [LinkLei] modo auth: {"bearer" if api_token else "sem token"}')
    return sess, api_token or 'SESSION', ud

# ── LinkLei — criar tarefa ────────────────────────────────────────────────────
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

# ── Google Sheets ──────────────────────────────────────────────────────────────
def update_spreadsheet(overdue, received, balance, movements):
    if not GOOGLE_SA_JSON or not SPREADSHEET_ID:
        print('  [Sheets] credenciais não configuradas — pulando planilha')
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

        mov_text = ' | '.join(m['subject'] for m in movements) if movements else '—'
        ws.append_row([TODAY_BR, overdue, received, f'R$ {balance}', mov_text, len(movements)])
        print('  [Sheets] linha adicionada ✓')
    except Exception as e:
        print(f'  [Sheets] Erro: {e}')

# ── Email — relatório diário ───────────────────────────────────────────────────
def send_report(overdue, received, balance, movements, created_tasks=None):
    if created_tasks is None:
        created_tasks = []
    mov_label = movements[0]['subject'] if movements else 'Sem novas movimentações'
    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="font-family:Arial,sans-serif;max-width:620px;margin:0 auto;padding:20px">
<h2 style="color:#1a3a5c;border-bottom:2px solid #1a3a5c;padding-bottom:8px">
  Pereira &amp; Ricci — Relatório Diário<br>
  <span style="font-size:14px;font-weight:normal;color:#666">{TODAY_BR}</span>
</h2>
<table style="width:100%;border-collapse:collapse;margin:16px 0">
  <tr style="background:#fdf2f2">
    <td style="padding:10px 14px;border:1px solid #f0c0c0"><strong style="color:#c0392b">Cobranças em Atraso</strong></td>
    <td style="padding:10px 14px;border:1px solid #f0c0c0;text-align:right;font-size:24px;font-weight:bold;color:#c0392b">{overdue}</td>
  </tr>
  <tr style="background:#f2fdf5">
    <td style="padding:10px 14px;border:1px solid #b0e0c0"><strong style="color:#27ae60">Recebimentos Hoje</strong></td>
    <td style="padding:10px 14px;border:1px solid #b0e0c0;text-align:right;font-size:24px;font-weight:bold;color:#27ae60">{received}</td>
  </tr>
  <tr style="background:#f2f7fd">
    <td style="padding:10px 14px;border:1px solid #b0c8e8"><strong style="color:#2980b9">Saldo Asaas</strong></td>
    <td style="padding:10px 14px;border:1px solid #b0c8e8;text-align:right;font-size:24px;font-weight:bold;color:#2980b9">R$ {balance}</td>
  </tr>
</table>
<div style="background:#faf0ff;border-left:4px solid #9b59b6;padding:12px 16px;margin:16px 0">
  <p style="margin:0;font-size:12px;color:#888;text-transform:uppercase;letter-spacing:1px">Nova Movimentação</p>
  <p style="margin:6px 0 0;font-weight:bold">{mov_label}</p>
</div>
{''.join(f'<p style="margin:4px 0;font-size:13px">✅ Tarefa criada: {m["subject"]}</p>' for m in created_tasks) if created_tasks else '<p style="margin:4px 0;font-size:13px;color:#888">Nenhuma tarefa criada no LinkLei</p>'}
<p style="color:#ccc;font-size:11px;text-align:center;margin-top:24px">
  Gerado automaticamente · {TODAY_BR} 06:00 UTC
</p>
</body></html>"""

    if not GMAIL_APP_PASSWORD:
        print('  [Email] GMAIL_APP_PASSWORD não configurado — pulando envio')
        return
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f'Relatório Diário — {TODAY_BR}'
        msg['From'] = GMAIL_USER
        msg['To'] = GMAIL_USER
        msg.attach(MIMEText(html, 'html'))
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            s.sendmail(GMAIL_USER, GMAIL_USER, msg.as_string())
        print('  [Email] Relatório enviado ✓')
    except Exception as e:
        print(f'  [Email] Erro: {e}')

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print(f'\n=== Pereira & Ricci — Relatório Diário {TODAY_BR} ===\n')

    # 1. Dados Asaas
    print('[1/5] Buscando dados Asaas...')
    overdue, received, balance = get_asaas_data()
    print(f'  Atraso: {overdue} | Hoje: {received} | Saldo: R$ {balance}')

    # 2. Emails LinkLei
    print('\n[2/5] Lendo emails do LinkLei (IMAP)...')
    movements = get_linklei_emails()

    # 3. Criar tarefas no LinkLei
    print('\n[3/5] Criando tarefas no LinkLei...')
    created_tasks = []
    if movements:
        try:
            sess, api_token, ud = linklei_login()
            if api_token:
                deadline = (datetime.utcnow() + timedelta(days=4)).strftime('%Y-%m-%d')
                for mov in movements:
                    status = create_task(sess, api_token, mov['subject'], deadline, ud)
                    print(f'  HTTP {status}: {mov["subject"][:70]}')
                    if status in (200, 201):
                        created_tasks.append(mov)
                print(f'  {len(created_tasks)}/{len(movements)} tarefas criadas ✓')
            else:
                print('  Sem api-token — tarefas não criadas')
        except Exception as e:
            print(f'  Erro: {e}')
    else:
        print('  Nenhuma movimentação nova — sem tarefas a criar')

    # 4. Atualizar planilha
    print('\n[4/5] Atualizando planilha Google Sheets...')
    update_spreadsheet(overdue, received, balance, movements)

    # 5. Enviar relatório
    print('\n[5/5] Enviando relatório por email...')
    send_report(overdue, received, balance, movements, created_tasks)

    print('\n✓ Concluído.')

if __name__ == '__main__':
    main()
