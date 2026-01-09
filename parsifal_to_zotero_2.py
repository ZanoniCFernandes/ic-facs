"""
Parsifal -> Zotero (versão final, robusta)
- Seleciona CSV (janela).
- Detecta encoding e delimitador automaticamente.
- Normaliza cabeçalhos (lowercase, strip).
- Filtra linhas com coluna 'status' em um conjunto flexível (accepted/aceito/selecionado/aprovado/...).
- Mapeia campos do CSV para um item Zotero (título, autores, resumo, DOI, journal, year, url, pages, volume, publisher, issn, language, keywords).
- Envia cada item à API do Zotero (users/{user_id}/items ou groups/{group_id}/items).
- Faz retries com backoff em falhas transitórias.
- Gera logs: impressão no console, arquivo JSONL de logs detalhados e failed_rows.csv com motivo.
- Mostra popups informativos (Tkinter).
"""


import csv, json, time, os, requests
from tkinter import filedialog, simpledialog, messagebox, Tk

# ---------- Configurações ----------
STATUS_VALIDOS = {"accepted", "aceito", "selecionado", "aprovado", "include", "included"}
ENCODINGS_TO_TRY = ("utf-8-sig", "utf-8", "latin-1", "windows-1252")
POSSIBLE_DELIMS = [',', ';', '\t', '|']
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_BASE = 1.0  # segundos
SLEEP_BETWEEN = 1.0  # pausa entre requisições
#TAG_DEFAULT = "Parsifal"

# ---------- Utilitários GUI ----------
def ask_file_csv():
    root = Tk(); root.withdraw()
    path = filedialog.askopenfilename(title="Selecione o CSV exportado do Parsifal", filetypes=[("CSV files","*.csv"),("All files","*.*")])
    root.destroy()
    return path

def ask_string(title, prompt, hide=False):
    root = Tk(); root.withdraw()
    try:
        if hide:
            val = simpledialog.askstring(title, prompt, show="*")
        else:
            val = simpledialog.askstring(title, prompt)
    finally:
        root.destroy()
    return val

def info_popup(title, msg):
    root = Tk(); root.withdraw(); messagebox.showinfo(title, msg); root.destroy()

def error_popup(title, msg):
    root = Tk(); root.withdraw(); messagebox.showerror(title, msg); root.destroy()

# ---------- Leitura robusta do CSV ----------
def read_csv_normalized(path):
    last_err = None
    for enc in ENCODINGS_TO_TRY:
        try:
            with open(path, encoding=enc, newline='') as f:
                sample = f.read(8192)
                f.seek(0)
                # heurística para detectar delimitador: conta ocorrências na primeira linha
                first_line = sample.splitlines()[0] if sample.splitlines() else ''
                delim_scores = {d: first_line.count(d) for d in POSSIBLE_DELIMS}
                chosen_delim = max(delim_scores, key=delim_scores.get)
                if delim_scores[chosen_delim] == 0:
                    chosen_delim = ','
                # fallback sniff
                try:
                    sniffer = csv.Sniffer()
                    dialect = sniffer.sniff(sample)
                    det = dialect.delimiter
                    if first_line.count(det) >= max(1, max(delim_scores.values())):
                        chosen_delim = det
                except Exception:
                    pass

                reader = csv.DictReader(f, delimiter=chosen_delim)
                rows_raw = list(reader)

                # Normaliza chaves e valores (garante strings)
                normalized = []
                for r in rows_raw:
                    nr = {}
                    for k, v in r.items():
                        key = str(k or "").strip().lower()
                        value = str(v or "").strip()
                        nr[key] = value
                    normalized.append(nr)

                return normalized, enc, chosen_delim
        except UnicodeDecodeError as ue:
            last_err = ue
            continue
        except Exception as e:
            last_err = e
            continue
    raise last_err or Exception("Falha ao ler CSV com os encodings testados.")

# ---------- Mapeamento de autores ----------
def parse_creators(authors_field):
    if not authors_field:
        return []
    s = authors_field.strip()
    # heurística simples: preferir ';' ou ' and ' como separador; senão ficar com único string
    if ';' in s:
        parts = [p.strip() for p in s.split(';') if p.strip()]
    elif ' and ' in s.lower():
        parts = [p.strip() for p in s.replace(' and ', ';').split(';') if p.strip()]
    elif ',' in s and s.count(',') > 1:
        # caso "Sobrenome, Nome; Sobrenome2, Nome2" - manter como listado por ';' não existir
        parts = [p.strip() for p in s.split(',') if p.strip()]
        # se isso gerar muitos pedaços, volte a tratar tudo como única string
        if len(parts) <= 2:
            parts = [s]
    else:
        parts = [s]
    creators = []
    for p in parts:
        creators.append({"creatorType": "author", "name": p})
    return creators

# ---------- Campos válidos por itemType (Zotero) ----------
ALLOWED_FIELDS_BY_TYPE = {
    "journalArticle": {
        "itemType", "title", "creators", "abstractNote", "publicationTitle",
        "date", "volume", "issue", "pages", "DOI", "ISSN", "language", "url", "tags", "extra"
    },
    "book": {
        "itemType", "title", "creators", "abstractNote", "publisher",
        "date", "pages", "volume", "ISBN", "language", "url", "tags", "extra"
    },
    "webpage": {
        "itemType", "title", "creators", "abstractNote", "url",
        "date", "language", "tags", "extra"
    },
    # fallback - permite campos básicos
    "default": {"itemType", "title", "creators", "abstractNote", "url", "date", "tags", "extra"}
}

def _sanitize_data_for_itemtype(data, itemType):
    allowed = ALLOWED_FIELDS_BY_TYPE.get(itemType, ALLOWED_FIELDS_BY_TYPE["default"])
    # Filtra apenas chaves permitidas pelo tipo
    return {k: v for k, v in data.items() if k in allowed}

def create_tags(keywords):
    nKeywords = keywords.replace(';', ',') 
    nKey = nKeywords.split(',')  
    output = []
    for value in nKey:
        value = value.strip()
        output.append({"tag": value})

    return(output)


# ---------- Construção do item Zotero ----------
def build_item_from_row(row):
    # Campos possíveis no CSV (em lowercase): title, author/authors, abstract, doi, url, journal, year, pages, volume, publisher, issn, language, keywords, bibtex_key
    title = row.get('title') or row.get('bibtex_key') or row.get('document_title') or ""
    authors = row.get('author') or row.get('authors') or ""
    creators = parse_creators(authors)
    abstract = row.get('abstract', "")
    doi = row.get('doi', "")
    url = row.get('url', "")
    journal = row.get('journal') or row.get('source') or ""
    year = row.get('year') or ""
    pages = row.get('pages') or ""
    volume = row.get('volume') or ""
    publisher = row.get('publisher') or ""
    issn = row.get('issn') or ""
    language = row.get('language') or ""
    keywords = row.get('keywords') or row.get('author_keywords') or ""

    # decide itemType
    itype = "journalArticle" if journal or doi else ("book" if 'book' in row.get('document_type','').lower() else "webpage")

    data = {
        "title": title or url or "No title",
        "creators": creators,
        "abstractNote": abstract,
        "url": url,
        "DOI": doi,
        "publicationTitle": journal,
        "date": year,
        "pages": pages,
        "volume": volume,
        "publisher": publisher,
        "ISSN": issn,
        "language": language,
    }
    # tags
    #data["tags"] = [{"tag": TAG_DEFAULT}]
    data["tags"] = create_tags(keywords)
    # incluir itemType dentro de data (exigido pelo Zotero ao criar itens)
    data["itemType"] = itype
    # remove chaves vazias antes de sanitizar
    data = {k: v for k, v in data.items() if v}

    # filtra campos inválidos para o itemType selecionado
    data = _sanitize_data_for_itemtype(data, itype)

    # o Zotero aceita um array de objetos onde cada objeto tem a chave "data"
    return {"data": data}

# ---------- Verificação de destino / credenciais ----------
def verify_destination(api_url, api_key):
    headers = {"Zotero-API-Key": api_key}
    try:
        # tenta ler 1 item para validar ID e permissões
        resp = requests.get(api_url, headers=headers, params={"limit": 1}, timeout=10)
        if resp.status_code == 200:
            return True, None
        if resp.status_code in (401, 403):
            return False, f"Chave inválida ou sem permissões de escrita/biblioteca (HTTP {resp.status_code}). Verifique 'Allow library access' e 'Allow write access' na chave."
        if resp.status_code == 404:
            return False, f"ID de usuário/grupo inválido (HTTP 404). Verifique se você usou o ID numérico correto."
        return False, f"Resposta inesperada {resp.status_code}: {resp.text}"
    except requests.RequestException as e:
        return False, f"Erro de conexão ao verificar destino: {e}"

# ---------- Envio com retries ----------
def send_item_zotero(api_url, api_key, item):
    headers = {
        "Zotero-API-Key": api_key,
        "Content-Type": "application/json"
    }

    payload = [item]
    
    attempt = 0
    while attempt < RETRY_ATTEMPTS:
        try:
            # Adicionar mais logs para debug
            print(f"\nDEBUG: Enviando para URL: {api_url}")
            print(f"DEBUG: Payload: {json.dumps(payload, indent=2, ensure_ascii=False)}")
            
            resp = requests.post(api_url, json=payload, headers=headers, timeout=30)
            print(f"DEBUG: Response status: {resp.status_code}")
            print(f"DEBUG: Response headers: {dict(resp.headers)}")
            print(f"DEBUG: Response body: {resp.text}")
            
            if 200 <= resp.status_code < 300:
                return True, None
            
            txt = resp.text
            err = f"HTTP {resp.status_code}: {txt}"
            
            attempt += 1
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
                continue
                
            return False, err
            
        except requests.RequestException as e:
            err = f"Connection error: {e}"
            attempt += 1
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
                continue
            return False, err

# ---------- Função principal ----------
def main():
    # 1) escolher csv
    csv_path = ask_file_csv()
    if not csv_path:
        print("Cancelado pelo usuário.")
        return

    # 2) ler e normalizar
    try:
        rows, enc, delim = read_csv_normalized(csv_path)
    except Exception as e:
        error_popup("Erro ao abrir CSV", f"Falha ao abrir/decodificar CSV:\n{e}")
        return

    print(f"[INFO] CSV lido com sucesso ({enc}) — {len(rows)} linhas — delimitador detectado: '{delim}'")
    if not rows:
        error_popup("Arquivo vazio", "O CSV foi lido mas não contém linhas.")
        return

    # 3) verificar coluna status
    if 'status' not in rows[0]:
        # mostra as colunas detectadas pra ajudar debugging
        cols = list(rows[0].keys())
        error_popup("Coluna 'status' ausente", f"A coluna 'status' não foi encontrada.\nColunas detectadas: {cols}")
        return

    # 4) filtrar aceitos (flexível)
    accepted = []
    for r in rows:
        st = str(r.get('status', '')).strip().lower()
        if st in STATUS_VALIDOS:
            accepted.append(r)
    print(f"[INFO] Itens com status aceito encontrados: {len(accepted)}")

    if not accepted:
        info_popup("Nenhum Accepted", "Não foram encontrados itens com status aceito (verifique valores na coluna 'status').")
        return

    # 5) pedir credenciais Zotero
    # pergunta se usuário quer usar biblioteca de usuário ou grupo
    root = Tk(); root.withdraw()
    tipo = simpledialog.askstring("Destino Zotero", "Enviar para USER ou GROUP? (digite 'user' ou 'group')", initialvalue="user")
    root.destroy()
    if not tipo:
        error_popup("Cancelado", "Operação cancelada (sem destino).")
        return
    tipo = tipo.strip().lower()
    user_or_group = 'user' if tipo == 'user' else 'group'
    id_prompt = "User ID (número)" if user_or_group == 'user' else "Group ID (número)"
    id_val = ask_string("ID Zotero", id_prompt)
    api_key = ask_string("Zotero API Key", "Cole sua Zotero API Key (será ocultada)?", hide=True)
    if not id_val or not api_key:
        error_popup("Credenciais faltando", "User/Group ID e API Key são obrigatórios.")
        return

    if user_or_group == 'user':
        api_url = f"https://api.zotero.org/users/{id_val}/items"
    else:
        api_url = f"https://api.zotero.org/groups/{id_val}/items"

    # verifica antes de prosseguir
    ok, why = verify_destination(api_url, api_key)
    if not ok:
        error_popup("Verificação Zotero falhou", why)
        print("Abortando:", why)
        return

    # 6) confirmar início
    if not messagebox.askyesno("Confirmação", f"Serão enviados {len(accepted)} itens para Zotero. Deseja continuar?"):
        print("Usuário cancelou envio.")
        return

    # 7) preparar logs
    folder = os.path.dirname(csv_path) or os.getcwd()
    failed_csv = os.path.join(folder, "failed_rows.csv")
    detailed_log = os.path.join(folder, "zotero_upload_log.jsonl")
    successes = 0
    failures = []

    # 8) verificar destino e credenciais
    valid, err = verify_destination(api_url, api_key)
    if not valid:
        error_popup("Erro de destino", err)
        return
    print("[INFO] Verificação de destino bem-sucedida.")

    # 9) loop de envio
    for idx, row in enumerate(accepted, start=1):
        try:
            item = build_item_from_row(row)
        except Exception as e:
            err = f"Erro ao montar item: {e}"
            print(f"[{idx}] FAIL (build): {err}")
            row["_error"] = err
            failures.append(row)
            continue

        ok, err = send_item_zotero(api_url, api_key, item)
        if ok:
            successes += 1
            print(f"[{idx}/{len(accepted)}] OK - {item['data'].get('title','(no title)')}")
        else:
            print(f"[{idx}/{len(accepted)}] FAIL - {item['data'].get('title','(no title)')} -> {err}")
            row["_error"] = err
            failures.append(row)
            # escreve log detalhado incremental
            try:
                with open(detailed_log, "a", encoding="utf-8") as lf:
                    lf.write(json.dumps({"index": idx, "title": item['data'].get('title'), "url": item['data'].get('url'), "error": err, "row": row}, ensure_ascii=False) + "\n")
            except Exception:
                pass
        time.sleep(SLEEP_BETWEEN)

    # 10) salvar failed_rows.csv, se houver
    if failures:
        keys = list(failures[0].keys())
        try:
            with open(failed_csv, "w", newline="", encoding="utf-8") as ff:
                writer = csv.DictWriter(ff, fieldnames=keys)
                writer.writeheader()
                for fr in failures:
                    writer.writerow(fr)
        except Exception as e:
            print("Erro ao salvar failed_rows.csv:", e)

    # 11) resumo final
    total = len(accepted)
    msg = f"Envio concluído. Sucesso: {successes}/{total}. Falhas: {len(failures)}."
    if failures:
        msg += f"\nArquivo de falhas: {failed_csv}\nLog detalhado: {detailed_log}"
    print(msg)
    info_popup("Concluído", msg)

if __name__ == "__main__":
    main()
