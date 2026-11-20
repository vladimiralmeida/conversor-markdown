#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
"""
pje_para_md.py — Converte autos do PJe (TJPE) em Markdown enxuto para análise no Claude.

Modos disponíveis:
  1  Íntegra sem OCR    — extrai todo o texto selecionável diretamente
  2  Íntegra com OCR    — rasteriza cada página e extrai via pytesseract
  3  Peças sem OCR      — você escolhe quais documentos incluir (texto direto)
  4  Peças com OCR      — você escolhe quais documentos incluir (via OCR)

Uso:
  python pje_para_md.py processo.pdf                        → menu interativo
  python pje_para_md.py processo.pdf --modo 1               → íntegra sem OCR
  python pje_para_md.py processo.pdf --modo 3 --ids 54064064 54064229
  python pje_para_md.py processo.pdf --listar               → só lista peças

Saída: <nome_do_processo>_autos.md no mesmo diretório do PDF

Dependências obrigatórias:
  pip install pdfminer.six          (fallback extração de texto)

Dependências opcionais mas recomendadas:
  poppler-utils  (pdftotext, pdftoppm) — muito mais rápido que pdfminer

Dependências para OCR (modos 2 e 4):
  pip install pytesseract pdf2image pillow
  + Tesseract instalado: https://github.com/tesseract-ocr/tesseract
"""

import sys
import re
import subprocess
import argparse
import shutil
from pathlib import Path

# ─────────────────────────────────────────────
# Padrões do PJe
# ─────────────────────────────────────────────
RE_NUM_PAG_1  = re.compile(r'Num\.\s+(\d+)\s+-\s+Pág\.\s+1\b')
RE_NUM_PAG_N  = re.compile(r'\nNum\.\s+\d+\s+-\s+Pág\.\s+\d+\s*\n')
RE_RODAPE     = re.compile(
    r'Este documento foi gerado pelo usuário[\s\S]*?'
    r'Assinado eletronicamente por:[^\n]+\n',
    re.MULTILINE
)
RE_RODAPE_SYS = re.compile(
    r'Este documento foi gerado pelo usuário .{0,200}?\n'
    r'Número do documento: \d+\n'
    r'https?://pje[\S]+\n',
    re.MULTILINE
)
RE_PAGBREAK   = re.compile(r'\x0c')

# Tipos → relevância para minutar (1=essencial, 2=útil, 3=ruído)
RELEVANCIA_TIPO = {
    "Petição Inicial": 1, "Sentença": 1, "Apelação": 1,
    "Contrarrazões": 1,   "Contestação": 1, "Embargos de Declaração": 1,
    "Recurso": 1,         "Acórdão": 1,     "Voto": 1,
    "Relatório": 1,       "Ementa": 1,      "Decisão": 1,
    "Despacho": 2,        "Certidão de julgamento": 2,
    "Manifestação": 2,    "Petição": 2,     "Requerimento": 2,
    "Habilitação": 2,
    "Outros Documentos": 3, "Certidão": 3,  "Intimação": 3,
    "Citação": 3,           "Procuração": 3, "Termo": 3,
    "Comprovante": 3,       "Guia": 3,       "Extrato": 3,
    "Planilha": 3,          "Estatuto": 3,   "Substabelecimento": 3,
    "Anexo": 3,
}

ESTRELAS = {1: "★★★", 2: "★★☆", 3: "★☆☆"}


# ─────────────────────────────────────────────
# Extração de texto
# ─────────────────────────────────────────────

def _tem_comando(cmd):
    return shutil.which(cmd) is not None


def extrair_texto_completo(pdf_path):
    """Extrai todo o texto do PDF: tenta pdftotext, depois pdfminer."""
    if _tem_comando("pdftotext"):
        r = subprocess.run(
            ["pdftotext", "-enc", "UTF-8", str(pdf_path), "-"],
            capture_output=True, timeout=180
        )
        if r.returncode == 0 and len(r.stdout) > 500:
            return r.stdout.decode("utf-8", errors="replace")

    try:
        from pdfminer.high_level import extract_text
        print("  (usando pdfminer — instale poppler-utils para mais velocidade)")
        return extract_text(str(pdf_path))
    except ImportError:
        print("ERRO: instale pdftotext (poppler-utils) ou pdfminer.six")
        print("  pip install pdfminer.six")
        sys.exit(1)


def extrair_texto_ocr(pdf_path, paginas=None):
    """
    OCR por página usando pytesseract + pdf2image.
    Retorna dict {num_pagina_1based: texto}.
    paginas=None → todas as páginas.
    """
    try:
        from pdf2image import convert_from_path
        import pytesseract
    except ImportError:
        print("ERRO: instale as dependências de OCR:")
        print("  pip install pytesseract pdf2image pillow")
        print("  + Tesseract: https://github.com/tesseract-ocr/tesseract")
        sys.exit(1)

    print(f"  Rasterizando PDF para OCR (pode demorar para arquivos grandes)...")
    imagens = convert_from_path(str(pdf_path), dpi=200, fmt="jpeg")
    total = len(imagens)
    resultado = {}

    indices = paginas if paginas else list(range(1, total + 1))
    for num_pag in indices:
        idx = num_pag - 1
        if idx < 0 or idx >= total:
            continue
        print(f"  OCR página {num_pag}/{total}...", end="\r")
        texto = pytesseract.image_to_string(imagens[idx], lang="por")
        resultado[num_pag] = texto

    print()
    return resultado


# ─────────────────────────────────────────────
# Parse do índice do PJe
# ─────────────────────────────────────────────

def parse_indice(pdf_path):
    """
    Extrai a lista de documentos do índice PJe.
    Usa pdftotext -layout nas primeiras páginas para preservar colunas.
    Retorna lista de dicts: {id, data, nome, tipo, relevancia}
    """
    # Extrair com layout preservado (só primeiras páginas)
    if _tem_comando("pdftotext"):
        r = subprocess.run(
            ["pdftotext", "-enc", "UTF-8", "-layout", "-f", "1", "-l", "8", str(pdf_path), "-"],
            capture_output=True, timeout=30
        )
        texto_layout = r.stdout.decode("utf-8", errors="replace") if r.returncode == 0 else ""
    else:
        try:
            from pdfminer.high_level import extract_text
            texto_layout = extract_text(str(pdf_path), page_numbers=list(range(8)))
        except Exception:
            texto_layout = ""

    documentos = []
    for linha in texto_layout.split('\n'):
        # Padrão de linha do índice com layout:
        # " 54064064 18/02/2025   Petição Inicial (Outras)          Petição Inicial (Outras)"
        m = re.match(
            r'\s*(\d{8})\s+(\d{2}/\d{2}/\d{4})\s+(.+?)\s{3,}(.+?)\s*$',
            linha
        )
        if m:
            doc_id   = m.group(1)
            data     = m.group(2)
            nome     = m.group(3).strip()
            tipo_raw = m.group(4).strip()

            rel = 3
            for chave, r_val in RELEVANCIA_TIPO.items():
                if chave.lower() in tipo_raw.lower() or chave.lower() in nome.lower():
                    rel = min(rel, r_val)

            documentos.append({
                "id":         doc_id,
                "data":       data,
                "nome":       nome,
                "tipo":       tipo_raw,
                "relevancia": rel,
            })

    return documentos


# ─────────────────────────────────────────────
# Divisão e limpeza
# ─────────────────────────────────────────────

def dividir_em_documentos(texto):
    """Divide o texto completo em fragmentos por documento PJe."""
    partes = re.split(r'(?=Num\.\s+\d+\s+-\s+Pág\.\s+1\b)', texto)
    fragmentos = {}
    for parte in partes:
        m = RE_NUM_PAG_1.search(parte)
        if not m:
            continue
        fragmentos[m.group(1)] = parte
    return fragmentos


def limpar_texto(texto):
    """Remove rodapés, quebras de página e marcadores do PJe."""
    texto = RE_PAGBREAK.sub('\n', texto)
    texto = RE_RODAPE.sub('', texto)
    texto = RE_RODAPE_SYS.sub('', texto)
    texto = RE_NUM_PAG_N.sub('\n', texto)
    texto = re.sub(r'Num\.\s+\d+\s+-\s+Pág\.\s+\d+\s*', '', texto)
    texto = re.sub(r'\n{4,}', '\n\n\n', texto)
    return texto.strip()


# ─────────────────────────────────────────────
# Geração do Markdown
# ─────────────────────────────────────────────

def gerar_cabecalho_processo(texto):
    campos = {}
    for campo in ['Número', 'Classe', 'Órgão julgador colegiado',
                  'Órgão julgador', 'Última distribuição',
                  'Valor da causa', 'Assuntos']:
        m = re.search(rf'{campo}\s*[:\–-]\s*(.+)', texto[:3000])
        if m:
            campos[campo] = m.group(1).strip()

    linhas = ["# AUTOS — PJe TJPE\n", "| Campo | Valor |", "|---|---|"]
    for k, v in campos.items():
        linhas.append(f"| **{k}** | {v} |")
    return '\n'.join(linhas) + '\n'


def documento_para_md(doc_id, info, conteudo):
    nome = info['nome'] if info else f"Documento {doc_id}"
    tipo = info['tipo'] if info else "—"
    data = info['data'] if info else "—"
    rel  = ESTRELAS.get(info['relevancia'], '') if info else ''

    md  = f"\n---\n\n## {rel} {nome}\n"
    md += f"**ID:** `{doc_id}` · **Data:** {data} · **Tipo:** {tipo}\n\n"
    md += limpar_texto(conteudo) + '\n'
    return md


def _salvar(md, output_path):
    output_path.write_text(md, encoding='utf-8')
    tam = len(md)
    tok = tam // 4
    print(f"\n✓ Salvo: {output_path}")
    print(f"  {tam:,} chars · ~{tok:,} tokens estimados")
    print(f"  (PDF original seria aprox. {tok * 6:,}+ tokens no Claude)")


# ─────────────────────────────────────────────
# Modos de conversão
# ─────────────────────────────────────────────

def modo_integra_sem_ocr(pdf_path, output_path):
    print("→ Modo 1: Íntegra sem OCR")
    texto = extrair_texto_completo(pdf_path)
    docs_info = {d['id']: d for d in parse_indice(pdf_path)}
    fragmentos = dividir_em_documentos(texto)

    md  = gerar_cabecalho_processo(texto)
    md += f"\n> **Modo:** Íntegra sem OCR · **Documentos:** {len(fragmentos)}\n"
    for doc_id, conteudo in fragmentos.items():
        md += documento_para_md(doc_id, docs_info.get(doc_id), conteudo)

    _salvar(md, output_path)


def modo_integra_com_ocr(pdf_path, output_path):
    print("→ Modo 2: Íntegra com OCR")
    textos_pag = extrair_texto_ocr(pdf_path)
    texto = "\n\f".join(textos_pag[p] for p in sorted(textos_pag))
    docs_info = {d['id']: d for d in parse_indice(pdf_path)}
    fragmentos = dividir_em_documentos(texto)

    md  = gerar_cabecalho_processo(texto)
    md += f"\n> **Modo:** Íntegra com OCR · **Documentos:** {len(fragmentos)}\n"
    for doc_id, conteudo in fragmentos.items():
        md += documento_para_md(doc_id, docs_info.get(doc_id), conteudo)

    _salvar(md, output_path)


def modo_pecas_sem_ocr(pdf_path, output_path, ids_selecionados):
    print(f"→ Modo 3: {len(ids_selecionados)} peça(s) sem OCR")
    texto = extrair_texto_completo(pdf_path)
    docs_info = {d['id']: d for d in parse_indice(pdf_path)}
    fragmentos = dividir_em_documentos(texto)

    md  = gerar_cabecalho_processo(texto)
    md += f"\n> **Modo:** Peças selecionadas sem OCR · **Selecionadas:** {len(ids_selecionados)}\n"

    ok = 0
    for doc_id in ids_selecionados:
        if doc_id in fragmentos:
            md += documento_para_md(doc_id, docs_info.get(doc_id), fragmentos[doc_id])
            ok += 1
        else:
            print(f"  AVISO: {doc_id} não encontrado")

    print(f"  {ok}/{len(ids_selecionados)} extraídas.")
    _salvar(md, output_path)


def modo_pecas_com_ocr(pdf_path, output_path, ids_selecionados):
    print(f"→ Modo 4: {len(ids_selecionados)} peça(s) com OCR")
    # Mapear páginas por documento via extração direta
    texto_base = extrair_texto_completo(pdf_path)
    docs_info = {d['id']: d for d in parse_indice(pdf_path)}
    paginas_por_doc = _mapear_paginas_por_doc(texto_base)

    md  = gerar_cabecalho_processo(texto_base)
    md += f"\n> **Modo:** Peças selecionadas com OCR · **Selecionadas:** {len(ids_selecionados)}\n"

    for doc_id in ids_selecionados:
        pags = paginas_por_doc.get(doc_id, [])
        if not pags:
            print(f"  AVISO: páginas de {doc_id} não mapeadas — usando extração direta")
            fragmentos = dividir_em_documentos(texto_base)
            conteudo = fragmentos.get(doc_id, "(não encontrado)")
        else:
            print(f"  OCR doc {doc_id} (págs {pags[0]}–{pags[-1]})...")
            textos_pag = extrair_texto_ocr(pdf_path, paginas=pags)
            conteudo = "\n\f".join(textos_pag.get(p, '') for p in pags)

        md += documento_para_md(doc_id, docs_info.get(doc_id), conteudo)

    _salvar(md, output_path)


def _mapear_paginas_por_doc(texto):
    """Mapeia doc_id → lista de páginas PDF usando contagem de \f."""
    paginas_por_doc = {}
    pos = 0
    pag_atual = 1
    ultimo_id = None
    ultimo_inicio = 1
    ids_ordem = []

    for m in re.finditer(r'Num\.\s+(\d+)\s+-\s+Pág\.\s+1\b', texto):
        trecho = texto[pos:m.start()]
        pag_atual += trecho.count('\x0c')
        if ultimo_id is not None:
            paginas_por_doc[ultimo_id] = list(range(ultimo_inicio, pag_atual))
        ultimo_id = m.group(1)
        ultimo_inicio = pag_atual
        ids_ordem.append(ultimo_id)
        pos = m.start()

    if ultimo_id:
        total_pags = texto.count('\x0c') + 1
        paginas_por_doc[ultimo_id] = list(range(ultimo_inicio, total_pags + 1))

    return paginas_por_doc


# ─────────────────────────────────────────────
# Menu interativo
# ─────────────────────────────────────────────

def listar_pecas(pdf_path):
    docs = parse_indice(pdf_path)
    print(f"\n{'─'*76}")
    print(f"  {'ID':<10} {'DATA':<12} {'REL':<5} {'NOME':<32} TIPO")
    print(f"{'─'*76}")
    for d in docs:
        rel = ESTRELAS.get(d['relevancia'], '')
        nome = d['nome'][:31]
        tipo = d['tipo'][:28]
        print(f"  {d['id']:<10} {d['data']:<12} {rel:<5} {nome:<32} {tipo}")
    print(f"{'─'*76}")
    print(f"  Total: {len(docs)} documentos")
    print(f"  ★★★ essencial para minutar | ★★☆ útil | ★☆☆ ruído/anexo\n")
    return docs


def menu_selecionar_pecas(docs):
    print("  Digite os IDs separados por espaço,")
    print("  ou 'relevantes' para selecionar todos ★★★ e ★★☆:")
    entrada = input("  > ").strip()

    if entrada.lower() == 'relevantes':
        ids = [d['id'] for d in docs if d['relevancia'] <= 2]
        print(f"  → {len(ids)} documentos selecionados")
        return ids

    ids = [x.strip() for x in entrada.split() if re.match(r'^\d{8}$', x.strip())]
    if not ids:
        print("Nenhum ID válido informado.")
        sys.exit(1)
    return ids


def menu_principal(pdf_path, output_path):
    print(f"\n{'═'*56}")
    print("   PJe → Markdown")
    print(f"{'═'*56}")
    print(f"   Arquivo : {pdf_path.name}")
    print(f"   Saída   : {output_path.name}")
    print(f"{'─'*56}")
    print("   1. Íntegra — sem OCR   (rápido, texto selecionável)")
    print("   2. Íntegra — com OCR   (lento, re-processa visualmente)")
    print("   3. Peças   — sem OCR   (selecione documentos)")
    print("   4. Peças   — com OCR   (selecione + re-processa)")
    print("   L. Listar documentos   (só exibe índice)")
    print(f"{'─'*56}")
    modo = input("   Escolha [1/2/3/4/L]: ").strip().upper()

    if modo == 'L':
        listar_pecas(pdf_path)
        return

    if modo not in ('1', '2', '3', '4'):
        print("Opção inválida.")
        sys.exit(1)

    ids = []
    if modo in ('3', '4'):
        docs = listar_pecas(pdf_path)
        ids = menu_selecionar_pecas(docs)

    print()
    _executar_modo(modo, pdf_path, output_path, ids)


def _executar_modo(modo, pdf_path, output_path, ids):
    {"1": modo_integra_sem_ocr,
     "2": modo_integra_com_ocr,
     "3": lambda p, o: modo_pecas_sem_ocr(p, o, ids),
     "4": lambda p, o: modo_pecas_com_ocr(p, o, ids),
    }[modo](pdf_path, output_path)


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Converte autos do PJe (TJPE) em Markdown para análise no Claude."
    )
    parser.add_argument("pdf", help="Caminho para o PDF dos autos")
    parser.add_argument("--modo", choices=["1","2","3","4"],
                        help="1=íntegra/sem-OCR 2=íntegra/OCR 3=peças/sem-OCR 4=peças/OCR")
    parser.add_argument("--ids", nargs="+", metavar="ID",
                        help="IDs dos documentos (modos 3 e 4)")
    parser.add_argument("--listar", action="store_true",
                        help="Lista documentos e encerra")
    parser.add_argument("--saida", metavar="ARQUIVO",
                        help="Caminho de saída .md (padrão: mesmo dir do PDF)")

    args = parser.parse_args()
    pdf_path = Path(args.pdf)

    # Se não for caminho absoluto e o arquivo não existir no diretório atual,
    # procura automaticamente na pasta Downloads
    if not pdf_path.is_absolute() and not pdf_path.exists():
        downloads = Path.home() / "Downloads" / pdf_path
        if downloads.exists():
            pdf_path = downloads
        else:
            pdf_path = pdf_path.resolve()

    pdf_path = pdf_path.resolve()

    if not pdf_path.exists():
        print(f"ERRO: arquivo não encontrado: {pdf_path}")
        sys.exit(1)

    saida = args.saida or str(pdf_path.parent / (pdf_path.stem + "_autos.md"))
    output_path = Path(saida)

    if args.listar:
        listar_pecas(pdf_path)
        return

    if args.modo:
        ids = args.ids or []
        if args.modo in ('3', '4') and not ids:
            docs = listar_pecas(pdf_path)
            ids = menu_selecionar_pecas(docs)
        _executar_modo(args.modo, pdf_path, output_path, ids)
    else:
        menu_principal(pdf_path, output_path)


if __name__ == "__main__":
    main()
