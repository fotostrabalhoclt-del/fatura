#!/usr/bin/env python3
"""Lê a fatura do cartão (PDF) e gera o checklist (index.html).

Formatos aceitos: Inter e Itaú/Hipercard (detecta sozinho).

Uso:
    python gerar_checklist.py                    # usa o PDF mais novo da pasta faturas/
    python gerar_checklist.py faturas/out.pdf    # usa um PDF específico
    python gerar_checklist.py fatura.pdf -o index.html

Requisitos:
    Inter : pip install pypdf   (ou poppler)
    Itaú  : poppler (Termux: pkg install poppler)
"""
import argparse, glob, json, os, re, shutil, subprocess, sys

MESES = {"jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
         "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12}

AQUI = os.path.dirname(os.path.abspath(__file__))
VALOR = r"\d{1,3}(?:\.\d{3})*,\d{2}"


# ---------------------------------------------------------------- utilidades
def ler_texto(caminho):
    """Extrai o texto do PDF. Devolve (texto, motor)."""
    if shutil.which("pdftotext"):
        r = subprocess.run(["pdftotext", "-layout", caminho, "-"],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout, "pdftotext"
    try:
        from pypdf import PdfReader
    except ImportError:
        sys.exit("Instale um leitor de PDF:  pip install pypdf   (ou: pkg install poppler)")
    texto = "\n".join((p.extract_text() or "") for p in PdfReader(caminho).pages)
    return texto, "pypdf"


def centavos(txt):
    return int(txt.replace(".", "").replace(",", ""))


def reais(c):
    s = "R$ {:,.2f}".format(abs(c) / 100).replace(",", "X").replace(".", ",").replace("X", ".")
    return ("- " + s) if c < 0 else s


def limpar_parcela(desc):
    # "(Parcela 05 de 18)" -> "(parcela 5 de 18)"
    return re.sub(r"\(Parcela\s+0*(\d+)\s+de\s+0*(\d+)\)",
                  r"(parcela \1 de \2)", desc, flags=re.I)


# ---------------------------------------------------------------------- Inter
RE_ITEM_INTER = re.compile(
    r"(\d{2})\s+de\s+([a-zç]{3})\.?\s+(\d{4})\s+(.+?)\s+-\s+(\+\s*)?R\$\s*(" + VALOR + r")\s*$",
    re.I)
RE_CARTAO_INTER = re.compile(r"^\s*CART[ÃA]O\s+(\S*(\d{4}))\s*$")
RE_TOTAL_CARTAO_INTER = re.compile(r"Total\s+CART[ÃA]O\s+\S*(\d{4})\s+R\$\s*(" + VALOR + ")", re.I)
RE_CABECALHO_INTER = re.compile(r"(\d{2}/\d{2}/\d{4})\s+R\$\s*(" + VALOR + ")")


def analisar_inter(texto):
    venc = total = None
    cartoes, totais = [], {}
    atual = None
    for linha in texto.splitlines():
        if venc is None:
            m = RE_CABECALHO_INTER.search(linha)
            if m:
                venc, total = m.group(1), centavos(m.group(2))
        m = RE_TOTAL_CARTAO_INTER.search(linha)
        if m:
            totais[m.group(1)] = centavos(m.group(2))
            continue
        m = RE_CARTAO_INTER.match(linha)
        if m:
            # o título do cartão se repete em cada página: reaproveita o mesmo
            atual = next((c for c in cartoes if c["final"] == m.group(2)), None)
            if atual is None:
                atual = {"name": "Cartão final " + m.group(2), "final": m.group(2), "items": []}
                cartoes.append(atual)
            continue
        m = RE_ITEM_INTER.search(linha)
        if m and atual is not None:
            dia, mes, ano, desc, credito, valor = m.groups()
            if credito:  # pagamentos/créditos não entram no checklist
                continue
            mes_num = MESES.get(mes.lower())
            if not mes_num:
                continue
            data = "{}/{:02d}/{}".format(dia, mes_num, ano)
            atual["items"].append([data, limpar_parcela(re.sub(r"\s+", " ", desc).strip()), centavos(valor)])
    return venc, total, cartoes, totais


# ----------------------------------------------------------------------- Itaú
# Linha de compra: "08/07  VitorViniciusDa 03/03     199,16"  (pode ter 2 colunas na mesma linha)
RE_ITEM_ITAU = re.compile(
    r"(?<![\w/])(\d{2}/\d{2})(?![\w/])\s+(\S.*?)\s{2,}(-\s*)?(" + VALOR + r")(?=\s{2,}|\s*$)")
RE_CARTAO_ITAU = re.compile(r"([A-ZÀ-Ú][A-ZÀ-Ú .'-]+?)\s+\(final\s+(\d{4})\)")
RE_TOTAL_CARTAO_ITAU = re.compile(r"Lançamentos\s+no\s+cartão\s+\(final\s+(\d{4})\)\s+(" + VALOR + ")")
RE_PARCELA_ITAU = re.compile(r"^(.*\S)\s+(\d{2})/(\d{2})$")


def analisar_itau(texto):
    m = re.search(r"Vencimento:\s*(\d{2}/\d{2}/\d{4})", texto)
    venc = m.group(1) if m else None
    m = re.search(r"Total desta fatura\s+(" + VALOR + ")", texto)
    total = centavos(m.group(1)) if m else None
    if not venc:
        return None, total, [], {}
    v_dia, v_mes, v_ano = (int(x) for x in venc.split("/"))

    # 1) junta todos os eventos de todas as páginas, na ordem de leitura
    #    (a fatura tem 2 colunas por página: lê a coluna da esquerda e depois a da direita)
    eventos = []
    for pn, pagina in enumerate(texto.split("\f")):
        linhas = pagina.splitlines()
        achados = []
        for ln, linha in enumerate(linhas):
            for m in RE_ITEM_ITAU.finditer(linha):
                achados.append((ln, m))
        posicoes = [m.start(1) for _, m in achados]
        limite = (min(posicoes) + max(posicoes)) / 2 if posicoes and max(posicoes) - min(posicoes) > 40 else float("inf")
        col = lambda pos: 0 if pos < limite else 1
        for ln, m in achados:
            eventos.append((pn, col(m.start(1)), ln, "item", m))
        for ln, linha in enumerate(linhas):
            m = RE_TOTAL_CARTAO_ITAU.search(linha)
            if m:
                eventos.append((pn, col(m.start()), ln, "total", m))
                continue
            m = RE_CARTAO_ITAU.search(linha)
            if m:
                eventos.append((pn, col(m.start()), ln, "cartao", m))
    eventos.sort(key=lambda e: e[:3])

    # 2) percorre os eventos: só vale o que está entre o título do cartão e
    #    "Lançamentos no cartão (final ....)"; depois disso vêm as parcelas das
    #    próximas faturas, que não entram no checklist
    cartoes, totais = [], {}
    atual = None
    for _, _, _, tipo, m in eventos:
        if tipo == "cartao":
            final = m.group(2)
            atual = next((c for c in cartoes if c["final"] == final), None)
            if atual is None:
                atual = {"name": "Cartão final {} ({})".format(final, m.group(1).strip().title()),
                         "final": final, "items": []}
                cartoes.append(atual)
        elif tipo == "total":
            totais[m.group(1)] = centavos(m.group(2))
            atual = None
        elif tipo == "item" and atual is not None:
            dm, desc, negativo, valor = m.group(1), m.group(2), m.group(3), m.group(4)
            dia, mes = (int(x) for x in dm.split("/"))
            ano = v_ano - 1 if mes > v_mes else v_ano
            desc = re.sub(r"\s+", " ", desc).strip()
            mp = RE_PARCELA_ITAU.match(desc)
            if mp:
                desc = "{} (parcela {} de {})".format(mp.group(1), int(mp.group(2)), int(mp.group(3)))
            c = centavos(valor)
            atual["items"].append(["{:02d}/{:02d}/{}".format(dia, mes, ano), desc, -c if negativo else c])
    return venc, total, cartoes, totais


# ----------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf", nargs="?")
    ap.add_argument("-o", "--saida", default=os.path.join(AQUI, "index.html"))
    args = ap.parse_args()

    pdf = args.pdf
    if not pdf:
        pdfs = sorted(glob.glob(os.path.join(AQUI, "faturas", "*.pdf")), key=os.path.getmtime)
        if not pdfs:
            sys.exit("Nenhum PDF em faturas/. Coloque a fatura lá ou passe o caminho.")
        pdf = pdfs[-1]
    print("Lendo:", pdf)

    texto, motor = ler_texto(pdf)
    if "Lançamentos: compras e saques" in texto:
        banco = "Itaú"
        if motor != "pdftotext":
            sys.exit("Para a fatura do Itaú é preciso o poppler.\n"
                     "No Termux:  pkg install poppler")
        venc, total, cartoes, totais = analisar_itau(texto)
    else:
        banco = "Inter"
        venc, total, cartoes, totais = analisar_inter(texto)
    print("Banco:", banco)
    if not venc or not cartoes:
        sys.exit("Não consegui ler a fatura. O formato do PDF mudou?")

    ok = True
    soma_geral = 0
    for c in cartoes:
        soma = sum(i[2] for i in c["items"])
        soma_geral += soma
        esperado = totais.get(c["final"])
        if esperado != soma:
            ok = False
        print("  {} : {} compras, soma {} (fatura diz {}) -> {}".format(
            c["name"], len(c["items"]), reais(soma),
            reais(esperado) if esperado is not None else "?",
            "OK" if esperado == soma else "DIFERENTE"))
    if total is not None and total != soma_geral:
        ok = False
        print("  Total da fatura {} != soma das compras {}".format(reais(total), reais(soma_geral)))
    if not ok:
        print("ATENÇÃO: os valores não bateram. Confira o checklist antes de enviar.")

    for c in cartoes:
        c.pop("final")

    modelo = open(os.path.join(AQUI, "modelo.html"), encoding="utf-8").read()
    html = (modelo.replace("__CARDS__", json.dumps(cartoes, ensure_ascii=False))
                  .replace("__VENC__", json.dumps(venc))
                  .replace("__BANCO__", json.dumps(banco, ensure_ascii=False))
                  .replace("__TOTAL__", str(total if total is not None else soma_geral)))
    with open(args.saida, "w", encoding="utf-8") as f:
        f.write(html)
    print("Gerado:", args.saida)


if __name__ == "__main__":
    main()
