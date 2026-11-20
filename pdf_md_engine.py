#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys, io
# Sob pythonw.exe (sem console, usado pela GUI) sys.stdout/stderr são None —
# só reconfigura o encoding quando há um console de verdade por trás (CLI).
if sys.stdout is not None:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr is not None:
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
"""
pdf_md_engine.py — Motor de conversão de qualquer PDF para Markdown.

Suporta:
  - Extração direta de texto (com detecção automática de colunas, útil para
    artigos científicos em duas colunas)
  - OCR (pytesseract + pdf2image) para PDFs escaneados, com DPI/idioma/
    pré-processamento configuráveis
  - Detecção heurística de capítulos/seções (tamanho de fonte + padrões de
    título como "Capítulo", "Seção", "Chapter" etc.)
  - Extração de imagens embutidas (via pdfimages, se disponível)
  - Saída em arquivo único ou em uma pasta com um .md por capítulo

Modos:
  1  Íntegra sem OCR    — documento inteiro, texto selecionável direto
  2  Íntegra com OCR    — documento inteiro, via OCR
  3  Capítulos sem OCR  — você escolhe quais capítulos/seções incluir
  4  Capítulos com OCR  — idem, via OCR

Uso via CLI:
  python pdf_md_engine.py livro.pdf --listar
  python pdf_md_engine.py livro.pdf --modo 1
  python pdf_md_engine.py artigo.pdf --modo 1 --colunas 2
  python pdf_md_engine.py escaneado.pdf --modo 2 --idioma por+eng --dpi 300
  python pdf_md_engine.py livro.pdf --modo 3 --capitulos 1 3 4 --arquivo-por-capitulo

Dependências obrigatórias:
  pip install pdfplumber pdfminer.six

Dependências para OCR (modos 2 e 4):
  pip install pytesseract pdf2image pillow
  + Tesseract instalado, com os idiomas necessários (por/eng)

Dependência opcional para extração de imagens:
  poppler-utils (pdfimages)
"""

import re
import shutil
import subprocess
import unicodedata
import argparse
from pathlib import Path
from collections import Counter

# ─────────────────────────────────────────────
# Padrões e constantes
# ─────────────────────────────────────────────

PADRAO_HEADING = re.compile(
    r'^(cap[ií]tulo|chapter|parte|part|se[cç][aã]o|section|anexo|appendix|'
    r'unidade|m[oó]dulo|t[íi]tulo)\s*[\divxlc]{0,6}\b',
    re.IGNORECASE
)

IDIOMAS_OCR = ("por", "eng", "por+eng")
DPIS_OCR = (200, 300, 400)


def _tem_comando(cmd):
    return shutil.which(cmd) is not None


def slugify(texto, max_len=50):
    texto = unicodedata.normalize('NFKD', texto).encode('ascii', 'ignore').decode('ascii')
    texto = re.sub(r'[^\w\s-]', '', texto).strip().lower()
    texto = re.sub(r'[\s_-]+', '-', texto)
    return texto[:max_len].strip('-') or "capitulo"


# ─────────────────────────────────────────────
# Detecção de colunas (extração direta)
# ─────────────────────────────────────────────

def detectar_colunas(page):
    """Heurística: a página tem 2 colunas se houver bastante texto nas
    metades esquerda e direita e pouco texto atravessando o centro."""
    palavras = page.extract_words()
    if len(palavras) < 20:
        return False
    meio = page.width / 2
    faixa = page.width * 0.04
    esquerda = sum(1 for p in palavras if p['x1'] < meio - faixa / 2)
    direita = sum(1 for p in palavras if p['x0'] > meio + faixa / 2)
    centro = sum(1 for p in palavras if p['x0'] <= meio + faixa / 2 and p['x1'] >= meio - faixa / 2)
    total = len(palavras)
    return esquerda > total * 0.25 and direita > total * 0.25 and centro < total * 0.08


def extrair_texto_2col(page):
    palavras = page.extract_words(keep_blank_chars=False)
    if not palavras:
        return page.extract_text() or ''
    meio = page.width / 2

    def montar(lista):
        linhas = {}
        for p in lista:
            chave = round(p['top'] / 3) * 3
            linhas.setdefault(chave, []).append(p)
        saida = []
        for chave in sorted(linhas):
            palavras_linha = sorted(linhas[chave], key=lambda p: p['x0'])
            saida.append(' '.join(p['text'] for p in palavras_linha))
        return '\n'.join(saida)

    esquerda = [p for p in palavras if (p['x0'] + p['x1']) / 2 < meio]
    direita = [p for p in palavras if (p['x0'] + p['x1']) / 2 >= meio]
    return montar(esquerda) + '\n\n' + montar(direita)


# ─────────────────────────────────────────────
# Detecção de capítulos/seções (heurística)
# ─────────────────────────────────────────────

def _linhas_com_tamanho(page):
    """Lista de (texto_da_linha, tamanho_medio_da_fonte) a partir dos chars da página."""
    chars = page.chars
    if not chars:
        return []
    linhas = {}
    for c in chars:
        if c['text'] == '\n':
            continue
        chave = round(c['top'])
        linhas.setdefault(chave, []).append(c)
    resultado = []
    for chave in sorted(linhas):
        cs = sorted(linhas[chave], key=lambda c: c['x0'])
        texto = ''.join(c['text'] for c in cs).strip()
        if not texto:
            continue
        tamanho = sum(c['size'] for c in cs) / len(cs)
        resultado.append((texto, tamanho))
    return resultado


def _eh_heading(texto, tamanho=None, mediana=None):
    if not (3 <= len(texto) <= 90):
        return False
    if PADRAO_HEADING.match(texto):
        return True
    if tamanho is not None and mediana:
        palavras = texto.split()
        if tamanho >= mediana * 1.15 and 1 <= len(palavras) <= 12:
            return True
    if texto.isupper() and 2 <= len(texto.split()) <= 10 and not texto.endswith(('.', ',', ';', ':')):
        return True
    return False


def _filtrar_ruido(candidatos_por_pagina, total_paginas):
    """Remove cabeçalhos/rodapés repetidos (o mesmo texto aparecendo em muitas páginas)."""
    contagem = Counter(
        c['titulo'].strip().lower()
        for lst in candidatos_por_pagina.values() for c in lst
    )
    limite = max(3, int(total_paginas * 0.15))
    capitulos = []
    for pagina in sorted(candidatos_por_pagina):
        for c in candidatos_por_pagina[pagina]:
            chave = c['titulo'].strip().lower()
            if contagem[chave] > limite:
                continue
            if capitulos and capitulos[-1]['titulo'].strip().lower() == chave:
                continue
            capitulos.append(c)
    return capitulos


def _numerar_capitulos(capitulos, total_paginas):
    numerados = []
    for i, c in enumerate(capitulos):
        pagina_fim = (capitulos[i + 1]['pagina'] - 1) if i + 1 < len(capitulos) else total_paginas
        numerados.append({
            "n": i + 1,
            "pagina_inicio": c['pagina'],
            "pagina_fim": max(pagina_fim, c['pagina']),
            "titulo": c['titulo'],
        })
    return numerados


def capitulos_fallback_por_paginas(total_paginas, tamanho_bloco=15):
    """Quando nenhuma estrutura é detectada, divide o documento em blocos fixos de páginas."""
    numerados = []
    inicio = 1
    n = 1
    while inicio <= total_paginas:
        fim = min(inicio + tamanho_bloco - 1, total_paginas)
        numerados.append({
            "n": n, "pagina_inicio": inicio, "pagina_fim": fim,
            "titulo": f"Páginas {inicio}–{fim}",
        })
        inicio = fim + 1
        n += 1
    return numerados


# ─────────────────────────────────────────────
# Extração direta (texto selecionável)
# ─────────────────────────────────────────────

def analisar_pdf_direto(pdf_path, colunas="auto"):
    """
    Lê o PDF inteiro com pdfplumber (necessário para detectar colunas e
    tamanhos de fonte). Retorna (paginas_texto, capitulos, total_paginas).
    """
    try:
        import pdfplumber
    except ImportError:
        raise RuntimeError("Dependência ausente: instale pdfplumber (pip install pdfplumber)")

    paginas_texto = {}
    candidatos_por_pagina = {}
    tamanhos = []

    with pdfplumber.open(str(pdf_path)) as pdf:
        total = len(pdf.pages)
        forcar_2col = (colunas == "2")
        forcar_1col = (colunas == "1")

        for i, page in enumerate(pdf.pages, start=1):
            if forcar_1col:
                dois = False
            elif forcar_2col:
                dois = True
            else:
                dois = detectar_colunas(page)

            texto = extrair_texto_2col(page) if dois else (page.extract_text() or '')
            paginas_texto[i] = texto

            candidatos = []
            for texto_linha, tamanho in _linhas_com_tamanho(page):
                tamanhos.append(tamanho)
                candidatos.append({"pagina": i, "titulo": texto_linha, "tamanho": tamanho})
            candidatos_por_pagina[i] = candidatos

        mediana = sorted(tamanhos)[len(tamanhos) // 2] if tamanhos else None
        heading_por_pagina = {
            pagina: [c for c in lst if _eh_heading(c['titulo'], c['tamanho'], mediana)]
            for pagina, lst in candidatos_por_pagina.items()
        }
        capitulos_brutos = _filtrar_ruido(heading_por_pagina, total)
        capitulos = _numerar_capitulos(capitulos_brutos, total)

    return paginas_texto, capitulos, total


def texto_extraivel(pdf_path, limite_chars=80):
    """Verifica rapidamente se o PDF tem texto selecionável (não é 100% escaneado)."""
    if _tem_comando("pdftotext"):
        r = subprocess.run(
            ["pdftotext", "-enc", "UTF-8", "-f", "1", "-l", "5", str(pdf_path), "-"],
            capture_output=True, timeout=30
        )
        texto = r.stdout.decode("utf-8", errors="replace") if r.returncode == 0 else ""
        return len(texto.strip()) > limite_chars
    try:
        import pdfplumber
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page in pdf.pages[:5]:
                if len((page.extract_text() or '').strip()) > limite_chars:
                    return True
        return False
    except Exception:
        return False


# ─────────────────────────────────────────────
# OCR
# ─────────────────────────────────────────────

def _preprocessar_imagem(img):
    from PIL import ImageOps
    img = img.convert("L")
    img = ImageOps.autocontrast(img)
    return img


def _ocr_pagina(img, idioma, preprocess):
    import pytesseract
    if preprocess:
        img = _preprocessar_imagem(img)
    return pytesseract.image_to_string(img, lang=idioma)


def analisar_pdf_ocr(pdf_path, dpi=300, idioma="por+eng", preprocess=False,
                      colunas="1", progresso=None):
    """
    Rasteriza o PDF e roda OCR página a página.
    colunas: '1' (padrão, página inteira) ou '2' (divide a imagem ao meio e
    faz OCR de cada metade na ordem de leitura). Não há detecção automática
    de colunas em modo OCR — sem uma camada de texto não dá pra confiar
    numa heurística visual, então é o usuário quem escolhe.
    Retorna (paginas_texto, capitulos, total_paginas).
    """
    try:
        from pdf2image import convert_from_path
    except ImportError:
        raise RuntimeError(
            "Dependências de OCR ausentes: instale pytesseract, pdf2image e pillow "
            "(pip install pytesseract pdf2image pillow) + Tesseract."
        )

    imagens = convert_from_path(str(pdf_path), dpi=dpi, fmt="jpeg")
    total = len(imagens)
    paginas_texto = {}
    candidatos_por_pagina = {}
    dividir = (colunas == "2")

    for i, img in enumerate(imagens, start=1):
        if progresso:
            progresso(i, total)
        if dividir:
            largura, altura = img.size
            meio = largura // 2
            texto = (_ocr_pagina(img.crop((0, 0, meio, altura)), idioma, preprocess) + "\n\n" +
                     _ocr_pagina(img.crop((meio, 0, largura, altura)), idioma, preprocess))
        else:
            texto = _ocr_pagina(img, idioma, preprocess)
        paginas_texto[i] = texto

        candidatos = []
        for linha in texto.split('\n'):
            linha = linha.strip()
            if linha and _eh_heading(linha):
                candidatos.append({"pagina": i, "titulo": linha})
        candidatos_por_pagina[i] = candidatos

    if progresso:
        progresso(total, total)
    capitulos_brutos = _filtrar_ruido(candidatos_por_pagina, total)
    capitulos = _numerar_capitulos(capitulos_brutos, total)
    return paginas_texto, capitulos, total


# ─────────────────────────────────────────────
# Extração de imagens
# ─────────────────────────────────────────────

def extrair_imagens(pdf_path, pasta_saida):
    """Extrai as imagens embutidas no PDF via pdfimages (poppler-utils).
    Retorna a quantidade extraída, ou None se pdfimages não está disponível."""
    if not _tem_comando("pdfimages"):
        return None
    pasta_saida.mkdir(parents=True, exist_ok=True)
    prefixo = str(pasta_saida / "img")
    try:
        subprocess.run(
            ["pdfimages", "-all", str(pdf_path), prefixo],
            capture_output=True, timeout=300, check=True
        )
    except Exception:
        return None
    return len(list(pasta_saida.glob("img-*")))


# ─────────────────────────────────────────────
# Geração de Markdown
# ─────────────────────────────────────────────

def _cabecalho(pdf_path, modo_desc, total_paginas, extra=""):
    linhas = [
        f"# {pdf_path.stem}",
        "",
        f"> **Arquivo original:** {pdf_path.name} · **Páginas:** {total_paginas} · **Modo:** {modo_desc}",
    ]
    if extra:
        linhas.append(f"> {extra}")
    linhas.append("")
    return "\n".join(linhas)


def _texto_paginas(paginas_texto, inicio, fim):
    partes = [paginas_texto.get(p, "") for p in range(inicio, fim + 1)]
    texto = "\n\n".join(p for p in partes if p.strip())
    texto = re.sub(r'\n{4,}', '\n\n\n', texto)
    return texto.strip()


def _salvar(md, output_path):
    # Sem print() aqui: esta função roda tanto na CLI (console) quanto
    # dentro da GUI via pythonw.exe, onde sys.stdout é None e print() quebra.
    output_path.write_text(md, encoding="utf-8")
    tam = len(md)
    tok = tam // 4
    return tam, tok


def gerar_integra(pdf_path, paginas_texto, total_paginas, output_path, modo_desc):
    md = _cabecalho(pdf_path, modo_desc, total_paginas)
    md += "\n" + _texto_paginas(paginas_texto, 1, total_paginas) + "\n"
    return _salvar(md, output_path)


def gerar_selecionado_arquivo_unico(pdf_path, paginas_texto, capitulos_sel, total_paginas,
                                     output_path, modo_desc):
    md = _cabecalho(pdf_path, modo_desc, total_paginas,
                     extra=f"{len(capitulos_sel)} capítulo(s)/seção(ões) selecionado(s)")
    for cap in capitulos_sel:
        md += f"\n---\n\n## {cap['titulo']}\n"
        md += f"*(págs. {cap['pagina_inicio']}–{cap['pagina_fim']})*\n\n"
        md += _texto_paginas(paginas_texto, cap['pagina_inicio'], cap['pagina_fim']) + "\n"
    return _salvar(md, output_path)


def gerar_selecionado_arquivo_por_capitulo(pdf_path, paginas_texto, capitulos_sel,
                                            total_paginas, pasta_saida, modo_desc):
    pasta_saida.mkdir(parents=True, exist_ok=True)
    indice_md = _cabecalho(pdf_path, modo_desc, total_paginas,
                            extra=f"{len(capitulos_sel)} capítulo(s)/seção(ões) — um arquivo por capítulo")
    indice_md += "\n## Índice\n\n"

    tam_total = 0
    for pos, cap in enumerate(capitulos_sel, start=1):
        nome_arquivo = f"{pos:02d}_{slugify(cap['titulo'])}.md"
        conteudo = f"# {cap['titulo']}\n\n"
        conteudo += f"*(págs. {cap['pagina_inicio']}–{cap['pagina_fim']} · {pdf_path.name})*\n\n"
        conteudo += _texto_paginas(paginas_texto, cap['pagina_inicio'], cap['pagina_fim']) + "\n"
        (pasta_saida / nome_arquivo).write_text(conteudo, encoding="utf-8")
        tam_total += len(conteudo)
        indice_md += f"{pos}. [{cap['titulo']}]({nome_arquivo}) *(págs. {cap['pagina_inicio']}–{cap['pagina_fim']})*\n"

    (pasta_saida / "00_indice.md").write_text(indice_md, encoding="utf-8")
    return tam_total, tam_total // 4


# ─────────────────────────────────────────────
# API de alto nível (usada pela CLI e pela GUI)
# ─────────────────────────────────────────────

def listar_estrutura(pdf_path, colunas="auto", ocr=False, dpi=300, idioma="por+eng", preprocess=False):
    """Retorna (capitulos, total_paginas, escaneado:bool)."""
    escaneado = not texto_extraivel(pdf_path)
    if not ocr and escaneado:
        return [], 0, True

    if ocr:
        _, capitulos, total = analisar_pdf_ocr(pdf_path, dpi=dpi, idioma=idioma, preprocess=preprocess,
                                                colunas=("2" if colunas == "2" else "1"))
    else:
        _, capitulos, total = analisar_pdf_direto(pdf_path, colunas=colunas)

    if not capitulos:
        capitulos = capitulos_fallback_por_paginas(total)
    return capitulos, total, escaneado


def converter(pdf_path, modo, output_path=None, colunas="auto", idioma="por+eng",
              dpi=300, preprocess=False, capitulos_ids=None, arquivo_por_capitulo=False,
              extrair_imgs=False, progresso=None):
    """
    modo: '1' íntegra sem OCR · '2' íntegra com OCR ·
          '3' capítulos sem OCR · '4' capítulos com OCR
    Retorna dict com informações do resultado.
    """
    pdf_path = Path(pdf_path)
    ocr = modo in ("2", "4")
    seletivo = modo in ("3", "4")

    modo_desc = {
        "1": "Íntegra sem OCR", "2": "Íntegra com OCR",
        "3": "Capítulos selecionados sem OCR", "4": "Capítulos selecionados com OCR",
    }[modo]

    if ocr:
        paginas_texto, capitulos_detectados, total = analisar_pdf_ocr(
            pdf_path, dpi=dpi, idioma=idioma, preprocess=preprocess,
            colunas=("2" if colunas == "2" else "1"), progresso=progresso
        )
    else:
        paginas_texto, capitulos_detectados, total = analisar_pdf_direto(pdf_path, colunas=colunas)

    if not capitulos_detectados:
        capitulos_detectados = capitulos_fallback_por_paginas(total)

    if output_path is None:
        sufixo = "_conteudo.md" if not seletivo else "_selecionado.md"
        output_path = pdf_path.parent / (pdf_path.stem + sufixo)
    else:
        output_path = Path(output_path)

    resultado = {"modo": modo, "capitulos_detectados": capitulos_detectados, "total_paginas": total}

    if extrair_imgs:
        pasta_imgs = output_path.parent / (pdf_path.stem + "_imagens")
        n_imgs = extrair_imagens(pdf_path, pasta_imgs)
        resultado["imagens"] = n_imgs
        resultado["pasta_imagens"] = str(pasta_imgs) if n_imgs else None

    if not seletivo:
        tam, tok = gerar_integra(pdf_path, paginas_texto, total, output_path, modo_desc)
        resultado.update({"output": str(output_path), "chars": tam, "tokens": tok, "pasta": False})
        return resultado

    if capitulos_ids:
        capitulos_sel = [c for c in capitulos_detectados if c["n"] in capitulos_ids]
    else:
        capitulos_sel = capitulos_detectados

    if not capitulos_sel:
        raise ValueError("Nenhum capítulo válido selecionado.")

    if arquivo_por_capitulo:
        pasta_saida = output_path.parent / (pdf_path.stem + "_capitulos")
        tam, tok = gerar_selecionado_arquivo_por_capitulo(
            pdf_path, paginas_texto, capitulos_sel, total, pasta_saida, modo_desc
        )
        resultado.update({"output": str(pasta_saida), "chars": tam, "tokens": tok,
                           "pasta": True, "n_capitulos": len(capitulos_sel)})
    else:
        tam, tok = gerar_selecionado_arquivo_unico(
            pdf_path, paginas_texto, capitulos_sel, total, output_path, modo_desc
        )
        resultado.update({"output": str(output_path), "chars": tam, "tokens": tok,
                           "pasta": False, "n_capitulos": len(capitulos_sel)})

    return resultado


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def _imprimir_estrutura(capitulos, total, escaneado):
    if escaneado:
        print("AVISO: este PDF parece ser escaneado (sem texto selecionável).")
        print("       Use --modo 2 ou 4 (OCR) para converter; a estrutura por")
        print("       capítulo só pode ser detectada depois do OCR.")
        return
    print(f"\n{'─'*70}")
    print(f"  {'Nº':<4} {'Pág.':<6} TÍTULO DETECTADO")
    print(f"{'─'*70}")
    for c in capitulos:
        print(f"  {c['n']:<4} {c['pagina_inicio']:<6} {c['titulo'][:58]}")
    print(f"{'─'*70}")
    print(f"  Total: {total} páginas · {len(capitulos)} seção(ões)/capítulo(s) detectado(s)\n")


def main():
    parser = argparse.ArgumentParser(
        description="Converte qualquer PDF (livros, artigos, contratos, digitalizações) em Markdown."
    )
    parser.add_argument("pdf", help="Caminho para o PDF")
    parser.add_argument("--modo", choices=["1", "2", "3", "4"],
                         help="1=íntegra/sem-OCR 2=íntegra/OCR 3=capítulos/sem-OCR 4=capítulos/OCR")
    parser.add_argument("--colunas", choices=["auto", "1", "2"], default="auto")
    parser.add_argument("--idioma", choices=list(IDIOMAS_OCR), default="por+eng", help="Idioma(s) do OCR")
    parser.add_argument("--dpi", type=int, choices=list(DPIS_OCR), default=300)
    parser.add_argument("--preprocess", action="store_true",
                         help="Pré-processa imagem antes do OCR (ajuda digitalizações ruins)")
    parser.add_argument("--imagens", action="store_true", help="Extrai imagens embutidas do PDF")
    parser.add_argument("--capitulos", nargs="+", type=int, metavar="N",
                         help="Números dos capítulos a incluir (ver --listar)")
    parser.add_argument("--arquivo-por-capitulo", action="store_true")
    parser.add_argument("--listar", action="store_true")
    parser.add_argument("--saida", metavar="CAMINHO")

    args = parser.parse_args()
    pdf_path = Path(args.pdf).resolve()
    if not pdf_path.exists():
        print(f"ERRO: arquivo não encontrado: {pdf_path}")
        sys.exit(1)

    if args.listar:
        capitulos, total, escaneado = listar_estrutura(pdf_path, colunas=args.colunas)
        _imprimir_estrutura(capitulos, total, escaneado)
        return

    if not args.modo:
        print("Especifique --modo (1/2/3/4) ou use --listar.")
        sys.exit(1)

    resultado = converter(
        pdf_path, args.modo, output_path=args.saida, colunas=args.colunas,
        idioma=args.idioma, dpi=args.dpi, preprocess=args.preprocess,
        capitulos_ids=args.capitulos, arquivo_por_capitulo=args.arquivo_por_capitulo,
        extrair_imgs=args.imagens,
        progresso=(lambda i, t: print(f"  OCR página {i}/{t}...", end="\r"))
    )

    if resultado.get("pasta"):
        print(f"\n✓ Salvo: {resultado['output']} ({resultado['n_capitulos']} arquivo(s) + índice)")
    else:
        print(f"\n✓ Salvo: {resultado['output']}")
    print(f"  {resultado['chars']:,} chars · ~{resultado['tokens']:,} tokens estimados")
    if resultado.get("imagens"):
        print(f"  {resultado['imagens']} imagem(ns) extraída(s) em: {resultado['pasta_imagens']}")


if __name__ == "__main__":
    main()
