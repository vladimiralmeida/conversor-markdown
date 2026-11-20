#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
conversor_md.py — Interface web local unificada para converter em Markdown:
  • Processos do PJe (usa pje_para_md.py)
  • PDFs genéricos: livros, artigos, contratos (usa pdf_md_engine.py)
  • DOCX, ODT, RTF, HTML, EPUB, PPTX, TXT

Os motores originais NÃO são alterados. Este arquivo só reaproveita as funções
deles e reescreve os laços de página para poder informar o progresso real.

Roda 100% na sua máquina (http://127.0.0.1:8477). Nada é enviado para a internet.
A saída vai para a pasta Downloads.

Dependências já instaladas: pdfplumber, pypdf, pdf2image, pytesseract, mammoth,
markdownify, beautifulsoup4, striprtf  (+ Tesseract e Poppler no PATH).
"""

import io
import os
import re
import sys
import json
import time
import uuid
import shutil
import zipfile
import tempfile
import threading
import traceback
import subprocess
import webbrowser
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor, as_completed

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))

# Os motores trocam sys.stdout por um TextIOWrapper ao serem importados.
# Isolamos isso para não fechar o stdout real (e para funcionar sob pythonw).
_orig_out, _orig_err = sys.stdout, sys.stderr
_descarte = []  # mantém os wrappers vivos: se forem coletados, fecham o buffer do próximo


def _stream_falso():
    w = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    _descarte.append(w)
    return w


try:
    sys.stdout, sys.stderr = _stream_falso(), _stream_falso()
    import pje_para_md as pje
    sys.stdout, sys.stderr = _stream_falso(), _stream_falso()
    import pdf_md_engine as eng
finally:
    sys.stdout, sys.stderr = _orig_out, _orig_err

PORT = 8477
DOWNLOADS = Path.home() / "Downloads"
TMP = Path(tempfile.gettempdir()) / "conversor_md"
HTML = AQUI / "conversor_md.html"

EXT_DOC = {".docx", ".odt", ".rtf", ".html", ".htm", ".epub", ".pptx", ".txt", ".md"}
EXT_ACEITAS = {".pdf"} | EXT_DOC | {".doc"}


def log(*a):
    try:
        if sys.stdout:
            print(*a, flush=True)
    except Exception:
        pass


# ─────────────────────────────────────────────
# Ambiente: Tesseract / Poppler
# ─────────────────────────────────────────────
POPPLER_PATH = None


def preparar_ambiente():
    global POPPLER_PATH
    candidatos_poppler = [
        r"C:\Program Files\Poppler\Library\bin", r"C:\Program Files\Poppler\bin",
        r"C:\Program Files\poppler\Library\bin", r"C:\poppler\Library\bin",
    ]
    if not shutil.which("pdftoppm") or not shutil.which("pdftotext"):
        for c in candidatos_poppler:
            if Path(c, "pdftoppm.exe").exists():
                os.environ["PATH"] = c + os.pathsep + os.environ.get("PATH", "")
                break
    if shutil.which("pdftoppm"):
        POPPLER_PATH = None  # já no PATH
    if not shutil.which("tesseract"):
        for c in (r"C:\Program Files\Tesseract-OCR", r"C:\Program Files (x86)\Tesseract-OCR"):
            if Path(c, "tesseract.exe").exists():
                os.environ["PATH"] = c + os.pathsep + os.environ.get("PATH", "")
                break


def status_ambiente():
    langs = []
    tess = bool(shutil.which("tesseract"))
    if tess:
        try:
            r = subprocess.run(["tesseract", "--list-langs"], capture_output=True, timeout=10)
            langs = [l.strip() for l in r.stdout.decode("utf-8", "replace").splitlines()[1:] if l.strip()]
        except Exception:
            pass
    return {
        "tesseract": tess,
        "idiomas": langs,
        "poppler": bool(shutil.which("pdftoppm")),
        "pdftotext": bool(shutil.which("pdftotext")),
        "downloads": str(DOWNLOADS),
        "extensoes": sorted(EXT_ACEITAS),
    }


# ─────────────────────────────────────────────
# Jobs (conversões em segundo plano)
# ─────────────────────────────────────────────
class Cancelado(Exception):
    pass


class Job:
    def __init__(self, tipo):
        self.id = uuid.uuid4().hex[:12]
        self.tipo = tipo            # 'analise' | 'conversao'
        self.status = "running"     # running | done | error | cancelled
        self.pct = 0.0
        self.etapa = "Iniciando…"
        self.linhas = []
        self.resultado = None
        self.erro = None
        self.cancel = threading.Event()
        self.t0 = time.time()
        self.t1 = None
        self.lock = threading.Lock()

    def set(self, pct=None, etapa=None):
        with self.lock:
            if pct is not None:
                self.pct = max(self.pct, min(100.0, float(pct)))
            if etapa is not None:
                self.etapa = etapa

    def say(self, msg):
        with self.lock:
            self.linhas.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
            self.linhas = self.linhas[-200:]

    def check(self):
        if self.cancel.is_set():
            raise Cancelado()

    def snap(self):
        with self.lock:
            fim = self.t1 or time.time()
            return {
                "id": self.id, "tipo": self.tipo, "status": self.status,
                "pct": round(self.pct, 1), "etapa": self.etapa,
                "decorrido": round(fim - self.t0, 1),
                "log": self.linhas[-60:], "resultado": self.resultado, "erro": self.erro,
            }


JOBS = {}
UPLOADS = {}


class Up:
    """Arquivo enviado pelo navegador."""
    def __init__(self, nome, path):
        self.id = path.parent.name
        self.nome = nome
        self.path = path
        self.ext = path.suffix.lower()
        self.cache = {}
        self.escaneado = False
        self.lock = threading.Lock()


def rodar(job, fn, *args):
    def alvo():
        try:
            fn(job, *args)
            if job.status == "running":
                job.status = "done"
                job.pct = 100.0
        except Cancelado:
            job.status = "cancelled"
            job.etapa = "Cancelado"
        except BaseException as e:  # inclui SystemExit dos motores
            job.status = "error"
            job.erro = mensagem_erro(e)
            job.say("ERRO: " + job.erro)
            log(traceback.format_exc())
        finally:
            job.t1 = time.time()
    JOBS[job.id] = job
    threading.Thread(target=alvo, daemon=True).start()


def mensagem_erro(e):
    if isinstance(e, SystemExit):
        return "Dependência ausente ou falha no motor de conversão."
    if isinstance(e, ImportError):
        return f"Biblioteca ausente: {e.name or e}. Instale com pip."
    if isinstance(e, FileNotFoundError) and "tesseract" in str(e).lower():
        return "Tesseract não encontrado. Instale-o ou adicione ao PATH."
    return str(e) or e.__class__.__name__


# ─────────────────────────────────────────────
# Utilidades
# ─────────────────────────────────────────────
def seguro(nome):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", nome).strip(" .") or "arquivo"


def unico(p):
    p = Path(p)
    if not p.exists():
        return p
    i = 2
    while True:
        q = p.with_name(f"{p.stem} ({i}){p.suffix}")
        if not q.exists():
            return q
        i += 1


def estimar(md_len):
    return int(md_len / 3.5)  # português: ~3,5 caracteres por token (estimativa)


# Tokens por página quando um PDF é enviado direto ao Claude (estimativa conservadora):
# PDF com texto ≈ 1.500–3.000 por página (sem contar a imagem da página);
# PDF digitalizado ≈ 1.200–2.000 por página (praticamente só imagem).
TOK_PAG = {"texto": (1500, 3000), "digitalizado": (1200, 2000)}


def economia(up, paginas, pecas=None):
    if not paginas:
        return None
    esc = bool(getattr(up, "escaneado", False))
    lo, hi = TOK_PAG["digitalizado" if esc else "texto"]
    return {"paginas": paginas, "lo": paginas * lo, "hi": paginas * hi,
            "digitalizado": esc, "pecas": pecas, "por_pagina": [lo, hi]}


def total_paginas(path):
    from pypdf import PdfReader
    return len(PdfReader(str(path), strict=False).pages)


def amostra_texto(path, ate=12):
    if shutil.which("pdftotext"):
        r = subprocess.run(["pdftotext", "-enc", "UTF-8", "-f", "1", "-l", str(ate), str(path), "-"],
                           capture_output=True, timeout=60)
        if r.returncode == 0:
            return r.stdout.decode("utf-8", "replace")
    try:
        import pdfplumber
        with pdfplumber.open(str(path)) as pdf:
            return "\n\f".join((p.extract_text() or "") for p in pdf.pages[:ate])
    except Exception:
        return ""


def detectar_pdf(path):
    texto = amostra_texto(path)
    return {
        "paginas": total_paginas(path),
        "pje": bool(pje.RE_NUM_PAG_1.search(texto)),
        "escaneado": len(texto.strip()) <= 80,
    }


# ─────────────────────────────────────────────
# OCR página a página (paralelo, memória constante)
# ─────────────────────────────────────────────
def ocr_paginas(job, path, paginas, dpi, idioma, preprocess, colunas, base, span, rotulo="OCR"):
    """Retorna {pagina: texto}. Rasteriza uma página por vez, 2-4 em paralelo."""
    from pdf2image import convert_from_path
    import pytesseract
    total = len(paginas)
    resultado = {}
    if total == 0:
        return resultado

    def trabalho(p):
        job.check()
        img = convert_from_path(str(path), dpi=dpi, first_page=p, last_page=p, fmt="jpeg")[0]
        if colunas == "2":
            w, h = img.size
            m = w // 2
            t = (eng._ocr_pagina(img.crop((0, 0, m, h)), idioma, preprocess) + "\n\n" +
                 eng._ocr_pagina(img.crop((m, 0, w, h)), idioma, preprocess))
        else:
            t = eng._ocr_pagina(img, idioma, preprocess)
        return p, t

    workers = max(1, min(4, (os.cpu_count() or 2) // 2))
    ex = ThreadPoolExecutor(max_workers=workers)
    feitos = 0
    try:
        futs = [ex.submit(trabalho, p) for p in paginas]
        for f in as_completed(futs):
            p, t = f.result()
            resultado[p] = t
            feitos += 1
            job.set(base + span * feitos / total, f"{rotulo}: página {feitos} de {total}")
    finally:
        ex.shutdown(wait=True, cancel_futures=True)
    return resultado


# ─────────────────────────────────────────────
# PJe
# ─────────────────────────────────────────────
RE_LINHA_IDX = re.compile(r"^\s*(\d{8,9})\s+(\d{2}/\d{2}/\d{4})\s+(\S.*?)(?:\s{3,}.*)?$")


def _relevancia(nome, tipo=""):
    rel = 3
    for chave, v in pje.RELEVANCIA_TIPO.items():
        if chave.lower() in tipo.lower() or chave.lower() in nome.lower():
            rel = min(rel, v)
    return rel


def info_docs_pje(up):
    """Metadados por documento. O parse_indice original só reconhece IDs de 8 dígitos
    e o layout antigo do índice; o PJe atual usa 9 dígitos. Aqui combinamos os dois."""
    with up.lock:
        if "pje_info" in up.cache:
            return up.cache["pje_info"]
    info = {d["id"]: d for d in pje.parse_indice(up.path)}
    if shutil.which("pdftotext"):
        r = subprocess.run(["pdftotext", "-enc", "UTF-8", "-layout", "-f", "1", "-l", "25", str(up.path), "-"],
                           capture_output=True, timeout=60)
        for linha in r.stdout.decode("utf-8", "replace").splitlines():
            m = RE_LINHA_IDX.match(linha)
            if m and m.group(1) not in info:
                nome = m.group(3).strip()
                info[m.group(1)] = {"id": m.group(1), "data": m.group(2), "nome": nome,
                                    "tipo": "", "relevancia": _relevancia(nome)}
    with up.lock:
        up.cache["pje_info"] = info
    return info


def itens_pje(job, up):
    texto = texto_base_pje(job, up, 5, 80)
    frag = pje.dividir_em_documentos(texto)
    if not frag:
        raise RuntimeError(
            "Não encontrei os marcadores de documento do PJe (“Num. … - Pág. 1”) neste PDF. "
            "Se for digitalizado, converta a íntegra com OCR; se não for um processo do PJe, "
            "escolha “PDF genérico”.")
    job.set(92, "Identificando as peças…")
    info = info_docs_pje(up)
    itens = []
    for i in frag:
        d = info.get(i)
        itens.append({"key": i, "titulo": d["nome"] if d else f"Documento {i}",
                      "tipo": d["tipo"] if d else "", "data": d["data"] if d else "",
                      "rel": d["relevancia"] if d else 3})
    job.set(100, f"{len(itens)} documentos encontrados")
    return itens


def texto_base_pje(job, up, base, span):
    with up.lock:
        if "pje_texto" in up.cache:
            return up.cache["pje_texto"]
    job.set(base, "Extraindo o texto do PDF (pode levar alguns instantes)…")
    texto = pje.extrair_texto_completo(up.path)
    job.check()
    job.set(base + span, "Texto extraído")
    with up.lock:
        up.cache["pje_texto"] = texto
    return texto


def converter_pje(job, up, p):
    ocr, sel = p["ocr"], p["escopo"] == "selecao"
    dpi, idioma, pre = int(p["dpi"]), p["idioma"], bool(p["preprocess"])
    out = unico(DOWNLOADS / f"{seguro(Path(up.nome).stem)}_autos.md")
    ids = [str(x) for x in p.get("selecao", [])]
    if sel and not ids:
        raise ValueError("Nenhuma peça selecionada.")

    if not sel and not ocr:
        job.say("PJe · íntegra sem OCR")
        texto = texto_base_pje(job, up, 5, 55)
        modo = "Íntegra sem OCR"
        frag = pje.dividir_em_documentos(texto)
        escolhidos = list(frag.keys())
        conteudos = {i: frag[i] for i in escolhidos}

    elif not sel and ocr:
        job.say(f"PJe · íntegra com OCR ({idioma}, {dpi} dpi)")
        n = total_paginas(up.path)
        pags = ocr_paginas(job, up.path, list(range(1, n + 1)), dpi, idioma, pre, "1", 3, 87)
        texto = "\n\f".join(pags[k] for k in sorted(pags))
        modo = "Íntegra com OCR"
        frag = pje.dividir_em_documentos(texto)
        escolhidos = list(frag.keys())
        conteudos = {i: frag[i] for i in escolhidos}

    elif sel and not ocr:
        job.say(f"PJe · {len(ids)} peça(s) sem OCR")
        texto = texto_base_pje(job, up, 5, 60)
        frag = pje.dividir_em_documentos(texto)
        modo = "Peças selecionadas sem OCR"
        escolhidos, conteudos = [], {}
        for i in ids:
            if i in frag:
                escolhidos.append(i)
                conteudos[i] = frag[i]
            else:
                job.say(f"Aviso: documento {i} não localizado no texto.")

    else:
        job.say(f"PJe · {len(ids)} peça(s) com OCR ({idioma}, {dpi} dpi)")
        texto = texto_base_pje(job, up, 3, 22)
        mapa = pje._mapear_paginas_por_doc(texto)
        frag = pje.dividir_em_documentos(texto)
        modo = "Peças selecionadas com OCR"
        todas = sorted({pg for i in ids for pg in mapa.get(i, [])})
        pags = ocr_paginas(job, up.path, todas, dpi, idioma, pre, "1", 26, 66)
        escolhidos, conteudos = [], {}
        for i in ids:
            if mapa.get(i):
                conteudos[i] = "\n\f".join(pags.get(pg, "") for pg in mapa[i])
                escolhidos.append(i)
            elif i in frag:
                job.say(f"Aviso: páginas de {i} não mapeadas — usei a extração direta.")
                conteudos[i] = frag[i]
                escolhidos.append(i)
            else:
                job.say(f"Aviso: documento {i} não localizado.")

    if not escolhidos:
        raise RuntimeError(
            "Nenhuma peça do PJe foi localizada no texto do PDF. "
            "Se o PDF for digitalizado, use a conversão com OCR.")

    if sel:
        mapa_pg = pje._mapear_paginas_por_doc(texto)
        pg_orig = sum(len(mapa_pg.get(i, [])) for i in escolhidos)
        pecas = [len(escolhidos), len(frag)]
    else:
        pg_orig, pecas = total_paginas(up.path), None
    job.set(94, "Montando o Markdown…")
    docs_info = info_docs_pje(up)
    md = pje.gerar_cabecalho_processo(texto)
    if sel:
        md += f"\n> **Modo:** {modo} · **Selecionadas:** {len(escolhidos)}\n"
    else:
        md += f"\n> **Modo:** {modo} · **Documentos:** {len(escolhidos)}\n"
    for i in escolhidos:
        md += pje.documento_para_md(i, docs_info.get(i), conteudos[i])

    out.write_text(md, encoding="utf-8")
    job.set(100, "Concluído")
    job.resultado = {"saida": str(out), "nome": out.name, "pasta": False,
                     "chars": len(md), "tokens": estimar(len(md)),
                     "detalhe": f"{len(escolhidos)} documento(s)",
                     "economia": economia(up, pg_orig, pecas)}


# ─────────────────────────────────────────────
# PDF genérico
# ─────────────────────────────────────────────
def estrutura_pdf(job, up, p, base, span):
    """Retorna (paginas_texto, capitulos, total). Usa cache por configuração."""
    ocr = bool(p["ocr"])
    colunas = p.get("colunas", "auto")
    dpi, idioma, pre = int(p["dpi"]), p["idioma"], bool(p["preprocess"])
    chave = ("ocr", dpi, idioma, pre, colunas) if ocr else ("direto", colunas)
    with up.lock:
        if chave in up.cache:
            job.set(base + span, "Estrutura já analisada")
            return up.cache[chave]

    total = total_paginas(up.path)
    candidatos = {}
    paginas_texto = {}

    if ocr:
        job.say(f"OCR de {total} páginas ({idioma}, {dpi} dpi)")
        paginas_texto = ocr_paginas(job, up.path, list(range(1, total + 1)), dpi, idioma, pre,
                                    "2" if colunas == "2" else "1", base, span * 0.95)
        for i, texto in paginas_texto.items():
            candidatos[i] = [{"pagina": i, "titulo": l.strip()} for l in texto.split("\n")
                             if l.strip() and eng._eh_heading(l.strip())]
    else:
        import pdfplumber
        tamanhos, brutos = [], {}
        with pdfplumber.open(str(up.path)) as pdf:
            total = len(pdf.pages)
            for i, page in enumerate(pdf.pages, start=1):
                job.check()
                dois = True if colunas == "2" else False if colunas == "1" else eng.detectar_colunas(page)
                paginas_texto[i] = eng.extrair_texto_2col(page) if dois else (page.extract_text() or "")
                lista = []
                for t, s in eng._linhas_com_tamanho(page):
                    tamanhos.append(s)
                    lista.append({"pagina": i, "titulo": t, "tamanho": s})
                brutos[i] = lista
                try:
                    page.flush_cache()
                except Exception:
                    pass
                if i % 3 == 0 or i == total:
                    job.set(base + span * 0.95 * i / total, f"Lendo página {i} de {total}")
        mediana = sorted(tamanhos)[len(tamanhos) // 2] if tamanhos else None
        candidatos = {pg: [c for c in l if eng._eh_heading(c["titulo"], c["tamanho"], mediana)]
                      for pg, l in brutos.items()}

    job.check()
    job.set(base + span * 0.97, "Detectando capítulos e seções…")
    caps = eng._numerar_capitulos(eng._filtrar_ruido(candidatos, total), total)
    if not caps:
        caps = eng.capitulos_fallback_por_paginas(total)
    res = (paginas_texto, caps, total)
    with up.lock:
        up.cache[chave] = res
    return res


def analisar_pdf_generico(job, up, p):
    if not p["ocr"] and not eng.texto_extraivel(up.path):
        raise RuntimeError("Este PDF parece digitalizado (sem texto selecionável). "
                           "Ative o OCR para analisá-lo.")
    _, caps, total = estrutura_pdf(job, up, p, 0, 100)
    job.resultado = {
        "total_paginas": total,
        "itens": [{"key": c["n"], "titulo": c["titulo"],
                   "pag": f"{c['pagina_inicio']}–{c['pagina_fim']}"} for c in caps]}


def converter_pdf_generico(job, up, p):
    ocr, sel = bool(p["ocr"]), p["escopo"] == "selecao"
    if not ocr and not eng.texto_extraivel(up.path):
        raise RuntimeError("Este PDF parece digitalizado (sem texto selecionável). "
                           "Ative o OCR para converter.")
    job.say(f"PDF · {'capítulos' if sel else 'íntegra'} · {'com' if ocr else 'sem'} OCR")
    paginas, caps, total = estrutura_pdf(job, up, p, 2, 88)
    stem = seguro(Path(up.nome).stem)
    modo_desc = {(False, False): "Íntegra sem OCR", (False, True): "Capítulos selecionados sem OCR",
                 (True, False): "Íntegra com OCR", (True, True): "Capítulos selecionados com OCR"}[(ocr, sel)]
    job.set(93, "Gerando o Markdown…")

    extra = ""
    if p.get("imagens"):
        pasta_img = unico(DOWNLOADS / f"{stem}_imagens")
        n = eng.extrair_imagens(up.path, pasta_img)
        extra = (f" · {n} imagem(ns) em {pasta_img.name}" if n
                 else " · extração de imagens indisponível ou sem imagens")

    if not sel:
        out = unico(DOWNLOADS / f"{stem}_conteudo.md")
        tam, tok = eng.gerar_integra(up.path, paginas, total, out, modo_desc)
        job.resultado = {"saida": str(out), "nome": out.name, "pasta": False, "chars": tam,
                         "tokens": estimar(tam), "detalhe": f"{total} página(s)" + extra,
                         "economia": economia(up, total)}
        return

    ids = {int(x) for x in p.get("selecao", [])}
    escolhidos = [c for c in caps if c["n"] in ids]
    if not escolhidos:
        raise ValueError("Nenhum capítulo selecionado.")
    if p.get("por_capitulo"):
        pasta = unico(DOWNLOADS / f"{stem}_capitulos")
        tam, tok = eng.gerar_selecionado_arquivo_por_capitulo(up.path, paginas, escolhidos, total, pasta, modo_desc)
        job.resultado = {"saida": str(pasta), "nome": pasta.name, "pasta": True, "chars": tam,
                         "tokens": estimar(tam), "detalhe": f"{len(escolhidos)} arquivo(s) + índice" + extra,
                         "economia": economia(up, sum(c["pagina_fim"] - c["pagina_inicio"] + 1 for c in escolhidos), [len(escolhidos), len(caps)])}
    else:
        out = unico(DOWNLOADS / f"{stem}_selecionado.md")
        tam, tok = eng.gerar_selecionado_arquivo_unico(up.path, paginas, escolhidos, total, out, modo_desc)
        job.resultado = {"saida": str(out), "nome": out.name, "pasta": False, "chars": tam,
                         "tokens": estimar(tam), "detalhe": f"{len(escolhidos)} capítulo(s)" + extra,
                         "economia": economia(up, sum(c["pagina_fim"] - c["pagina_inicio"] + 1 for c in escolhidos), [len(escolhidos), len(caps)])}


# ─────────────────────────────────────────────
# DOCX, ODT, RTF, HTML, EPUB, PPTX, TXT
# ─────────────────────────────────────────────
def _html_para_md(html):
    from markdownify import markdownify
    md = markdownify(html, heading_style="ATX", strip=["img", "script", "style"])
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def _docx(path):
    import mammoth
    with open(path, "rb") as f:
        r = mammoth.convert_to_html(f, convert_image=mammoth.images.img_element(lambda i: {"src": ""}))
    return _html_para_md(r.value)


def _ler_texto(path):
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _rtf(path):
    from striprtf.striprtf import rtf_to_text
    return rtf_to_text(_ler_texto(path), errors="ignore").strip()


def _html(path):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(_ler_texto(path), "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    corpo = soup.body or soup
    return _html_para_md(str(corpo))


def _epub(path):
    from bs4 import BeautifulSoup
    with zipfile.ZipFile(path) as z:
        cont = ET.fromstring(z.read("META-INF/container.xml"))
        opf_path = cont.find(".//{*}rootfile").get("full-path")
        base = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
        opf = ET.fromstring(z.read(opf_path))
        manifest = {i.get("id"): i.get("href") for i in opf.findall(".//{*}manifest/{*}item")}
        partes = []
        for ir in opf.findall(".//{*}spine/{*}itemref"):
            href = manifest.get(ir.get("idref"))
            if not href:
                continue
            try:
                dados = z.read(base + href.split("#")[0]).decode("utf-8", "replace")
            except KeyError:
                continue
            soup = BeautifulSoup(dados, "html.parser")
            md = _html_para_md(str(soup.body or soup))
            if md:
                partes.append(md)
    return "\n\n---\n\n".join(partes)


def _pptx(path):
    ns = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    with zipfile.ZipFile(path) as z:
        slides = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                        key=lambda n: int(re.findall(r"\d+", n)[-1]))
        out = []
        for k, n in enumerate(slides, 1):
            root = ET.fromstring(z.read(n))
            linhas = []
            for par in root.iter(ns + "p"):
                t = "".join(x.text or "" for x in par.iter(ns + "t")).strip()
                if t:
                    linhas.append(t)
            out.append(f"## Slide {k}\n\n" + "\n\n".join(linhas))
    return "\n\n".join(out)


def _odt(path):
    T = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
    TB = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("content.xml"))

    def texto(el):
        s = el.text or ""
        for c in el:
            if c.tag == T + "s":
                s += " " * int(c.get(T + "c", "1"))
            elif c.tag == T + "tab":
                s += "\t"
            elif c.tag == T + "line-break":
                s += "\n"
            else:
                s += texto(c)
            s += c.tail or ""
        return s

    saida = []

    def visita(el, nivel_lista=0):
        for c in el:
            if c.tag == T + "h":
                saida.append("#" * min(int(c.get(T + "outline-level", "1")), 6) + " " + texto(c).strip())
            elif c.tag == T + "p":
                t = texto(c).strip()
                if t:
                    saida.append(t)
            elif c.tag == T + "list":
                for li in c.iter(T + "p"):
                    saida.append("  " * nivel_lista + "- " + texto(li).strip())
            elif c.tag == TB + "table":
                linhas = []
                for tr in c.iter(TB + "table-row"):
                    cel = [" ".join(texto(x).strip() for x in td.iter(T + "p")).replace("|", "\\|")
                           for td in tr.findall(TB + "table-cell")]
                    linhas.append("| " + " | ".join(cel) + " |")
                if linhas:
                    n = linhas[0].count("|") - 1
                    linhas.insert(1, "|" + "---|" * max(n, 1))
                    saida.append("\n".join(linhas))
            else:
                visita(c, nivel_lista)

    visita(root.find(".//{*}body"))
    return "\n\n".join(saida)


def converter_documento(job, up, p):
    ext = up.ext
    if ext == ".doc":
        raise RuntimeError("O formato .doc (Word antigo) não é suportado diretamente. "
                           "Abra no Word e use Salvar como → .docx.")
    funcs = {".docx": _docx, ".odt": _odt, ".rtf": _rtf, ".html": _html, ".htm": _html,
             ".epub": _epub, ".pptx": _pptx, ".txt": _ler_texto, ".md": _ler_texto}
    job.set(10, f"Lendo {ext.upper().lstrip('.')}…")
    job.say(f"Documento {ext}")
    corpo = funcs[ext](up.path)
    job.check()
    job.set(80, "Montando o Markdown…")
    if not corpo.strip():
        raise RuntimeError("Não foi possível extrair texto deste arquivo.")
    stem = Path(up.nome).stem
    md = f"# {stem}\n\n> **Arquivo original:** {up.nome} · **Formato:** {ext.lstrip('.').upper()}\n\n{corpo}\n"
    out = unico(DOWNLOADS / f"{seguro(stem)}.md")
    out.write_text(md, encoding="utf-8")
    job.set(100, "Concluído")
    job.resultado = {"saida": str(out), "nome": out.name, "pasta": False,
                     "chars": len(md), "tokens": estimar(len(md)), "detalhe": ext.lstrip(".").upper()}


# ─────────────────────────────────────────────
# Despacho dos jobs
# ─────────────────────────────────────────────
def normalizar(p):
    return {
        "tipo": p.get("tipo", "pdf"),
        "escopo": p.get("escopo", "integra"),
        "ocr": bool(p.get("ocr", False)),
        "selecao": p.get("selecao", []),
        "idioma": p.get("idioma") if p.get("idioma") in ("por", "eng", "por+eng") else "por",
        "dpi": p.get("dpi") if str(p.get("dpi")) in ("200", "300", "400") else 200,
        "preprocess": bool(p.get("preprocess", False)),
        "colunas": p.get("colunas") if p.get("colunas") in ("auto", "1", "2") else "auto",
        "por_capitulo": bool(p.get("por_capitulo", False)),
        "imagens": bool(p.get("imagens", False)),
    }


def job_analise(job, up, p):
    if p["tipo"] == "pje":
        job.resultado = {"itens": itens_pje(job, up)}
    else:
        analisar_pdf_generico(job, up, p)


def job_conversao(job, up, p):
    if p["tipo"] == "pje":
        converter_pje(job, up, p)
    elif p["tipo"] == "doc":
        converter_documento(job, up, p)
    else:
        converter_pdf_generico(job, up, p)


# ─────────────────────────────────────────────
# Servidor HTTP
# ─────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ConversorMD"

    def log_message(self, *a):
        pass

    # --- segurança: só aceita Host/Origin locais (evita CSRF e DNS rebinding)
    def _host_ok(self):
        porta = self.server.server_address[1]
        ok = {f"127.0.0.1:{porta}", f"localhost:{porta}"}
        if self.headers.get("Host", "") not in ok:
            return False
        origem = self.headers.get("Origin")
        if origem and origem not in {f"http://{h}" for h in ok}:
            return False
        return True

    def _json(self, obj, code=200):
        dados = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(dados)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(dados)

    def _corpo_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000:
            raise ValueError("Requisição grande demais.")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if not self._host_ok():
            return self._json({"erro": "Acesso negado."}, 403)
        u = urlparse(self.path)
        try:
            if u.path in ("/", "/index.html"):
                dados = HTML.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(dados)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(dados)
            elif u.path == "/api/status":
                self._json(status_ambiente())
            elif u.path.startswith("/api/jobs/"):
                j = JOBS.get(u.path.rsplit("/", 1)[-1])
                self._json(j.snap() if j else {"erro": "Tarefa não encontrada."}, 200 if j else 404)
            else:
                self._json({"erro": "Não encontrado."}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if not self._host_ok():
            return self._json({"erro": "Acesso negado."}, 403)
        u = urlparse(self.path)
        try:
            if u.path == "/api/upload":
                return self._upload(u)
            corpo = self._corpo_json()
            if u.path in ("/api/analisar", "/api/converter"):
                up = UPLOADS.get(corpo.get("id"))
                if not up:
                    return self._json({"erro": "Arquivo não encontrado. Envie-o novamente."}, 404)
                p = normalizar(corpo)
                if up.ext in EXT_DOC or up.ext == ".doc":
                    p["tipo"] = "doc"
                elif p["tipo"] not in ("pje", "pdf"):
                    p["tipo"] = "pdf"
                if p["ocr"] and not shutil.which("tesseract"):
                    return self._json({"erro": "Tesseract não encontrado no PATH."}, 400)
                if u.path == "/api/analisar":
                    j = Job("analise")
                    rodar(j, job_analise, up, p)
                else:
                    j = Job("conversao")
                    rodar(j, job_conversao, up, p)
                self._json({"job": j.id})
            elif u.path == "/api/cancelar":
                j = JOBS.get(corpo.get("job"))
                if j:
                    j.cancel.set()
                self._json({"ok": True})
            elif u.path in ("/api/abrir-pasta", "/api/abrir-arquivo"):
                j = JOBS.get(corpo.get("job"))
                if not j or not j.resultado:
                    return self._json({"erro": "Resultado não encontrado."}, 404)
                alvo = Path(j.resultado["saida"])
                if u.path == "/api/abrir-pasta":
                    if alvo.is_dir():
                        os.startfile(str(alvo))
                    else:
                        subprocess.Popen(["explorer", f"/select,{alvo}"])
                else:
                    os.startfile(str(alvo if alvo.is_file() else alvo / "00_indice.md"))
                self._json({"ok": True})
            elif u.path == "/api/descartar":
                up = UPLOADS.pop(corpo.get("id"), None)
                if up:
                    shutil.rmtree(up.path.parent, ignore_errors=True)
                self._json({"ok": True})
            elif u.path == "/api/encerrar":
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._json({"erro": "Não encontrado."}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(traceback.format_exc())
            try:
                self._json({"erro": mensagem_erro(e)}, 500)
            except Exception:
                pass

    def _upload(self, u):
        nome = Path(parse_qs(u.query).get("nome", ["arquivo"])[0]).name
        ext = Path(nome).suffix.lower()
        if ext not in EXT_ACEITAS:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            return self._json({"erro": f"Formato {ext or 'desconhecido'} não suportado. "
                                       f"Aceitos: {', '.join(sorted(EXT_ACEITAS))}."}, 415)
        pasta = TMP / uuid.uuid4().hex[:12]
        pasta.mkdir(parents=True, exist_ok=True)
        destino = pasta / seguro(nome)
        restante = int(self.headers.get("Content-Length") or 0)
        with open(destino, "wb") as f:
            while restante > 0:
                bloco = self.rfile.read(min(1 << 20, restante))
                if not bloco:
                    break
                f.write(bloco)
                restante -= len(bloco)
        up = Up(nome, destino)
        info = {"id": up.id, "nome": nome, "ext": ext, "tamanho": destino.stat().st_size,
                "kind": "pdf" if ext == ".pdf" else "doc",
                "paginas": None, "pje": False, "escaneado": False}
        if ext == ".pdf":
            try:
                info.update(detectar_pdf(destino))
                up.escaneado = info["escaneado"]
            except Exception as e:
                shutil.rmtree(pasta, ignore_errors=True)
                return self._json({"erro": f"Não consegui abrir o PDF: {mensagem_erro(e)}"}, 422)
        UPLOADS[up.id] = up
        self._json(info)


def limpar_tmp():
    shutil.rmtree(TMP, ignore_errors=True)


def main():
    preparar_ambiente()
    limpar_tmp()
    TMP.mkdir(parents=True, exist_ok=True)
    url = f"http://127.0.0.1:{PORT}/"
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        log(f"Já há um servidor na porta {PORT}; abrindo o navegador.")
        webbrowser.open(url)
        return
    srv.daemon_threads = True
    log(f"Conversor MD rodando em {url}  (Ctrl+C para encerrar)")
    threading.Timer(0.6, webbrowser.open, [url]).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        limpar_tmp()


if __name__ == "__main__":
    main()
