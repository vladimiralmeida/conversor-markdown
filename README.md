# Conversor para Markdown

Interface web local para converter em Markdown:

- **Processos do PJe** (PDF exportado do PJe): íntegra ou peças escolhidas, com ou sem OCR.
- **PDFs genéricos** (livros, artigos, contratos, digitalizações): íntegra ou capítulos escolhidos, com ou sem OCR.
- **DOCX, ODT, RTF, HTML, EPUB, PPTX e TXT**: conversão direta.

Tudo roda na sua máquina (`http://127.0.0.1:8477`); nenhum arquivo é enviado à internet. O resultado é salvo na pasta **Downloads**, com painel de progresso em percentual.

## Instalação (Windows)

1. Instale o **Python 3.10+** (marque *Add python.exe to PATH*): <https://www.python.org/downloads/>
2. Para **OCR**, instale também (e deixe no PATH):
   - **Tesseract** com o idioma português: <https://github.com/UB-Mannheim/tesseract/wiki> (marque *Portuguese* na instalação)
   - **Poppler** para Windows: <https://github.com/oschwartz10612/poppler-windows/releases> (adicione a pasta `Library\bin` ao PATH)
3. Baixe este repositório (`git clone` ou *Code → Download ZIP*) e execute **`instalar.bat`**. Ele instala as bibliotecas e cria o atalho na área de trabalho.

Sem Tesseract/Poppler o conversor funciona normalmente, apenas sem as opções de OCR.

## Uso

Abra o atalho **Conversor para Markdown** (ou `iniciar.bat`). O navegador abre sozinho. Para encerrar, clique em *Encerrar servidor* no rodapé ou feche a janela do console.

## Estrutura

| Arquivo | Função |
|---|---|
| `conversor_md.py` / `conversor_md.html` | Servidor local e interface |
| `pje_para_md.py` | Motor de conversão de autos do PJe (também funciona via linha de comando) |
| `pdf_md_engine.py` | Motor de conversão de PDFs genéricos (também funciona via linha de comando) |

Desenvolvido por Vladimir Almeida com Claude Code em Novembro de 2026.

## Licença

[MIT](LICENSE) © 2026 Vladimir Almeida
