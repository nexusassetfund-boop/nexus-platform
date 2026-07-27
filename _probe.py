# 임시 프로브 — 커밋 금지
import io, re, sys, zipfile, urllib.request
KEY = open('_dartkey.tmp').read().strip()

def doc(rcept):
    url = f"https://opendart.fss.or.kr/api/document.xml?crtfc_key={KEY}&rcept_no={rcept}"
    with urllib.request.urlopen(url, timeout=30) as r:
        raw = r.read()
    z = zipfile.ZipFile(io.BytesIO(raw))
    data = z.read(z.namelist()[0])
    for enc in ("utf-8", "euc-kr", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")

mode = sys.argv[1]
for rc in sys.argv[2:]:
    t = doc(rc)
    t = re.sub(r"<[^>]+>", " ", t)
    print("="*20, rc)
    if mode == "prov":
        # 단위 문맥
        for m in re.finditer(r".{0,40}단위.{0,60}", t):
            print("UNIT:", re.sub(r"\s+"," ",m.group(0))[:120])
        toks = t.split()
        for kw in ("매출액","영업이익","당기순이익"):
            for i,tk in enumerate(toks):
                if tk == kw or tk.startswith(kw):
                    print(kw, "->", toks[i:i+14])
                    break
    else:  # ir
        # 제목/목적/내용 근처
        for kw in ("목적","제목","내용","일시"):
            for m in re.finditer(kw + r".{0,150}", t):
                s = re.sub(r"\s+"," ",m.group(0))[:170]
                print(s)
                break
    print()
