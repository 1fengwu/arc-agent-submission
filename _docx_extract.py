import zipfile, re, sys
from pathlib import Path
def extract(path):
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read('word/document.xml').decode('utf-8', 'ignore')
    except Exception as e:
        return f"[ERR reading {path}: {e}]"
    # paragraphs
    paras = re.split(r'</w:p>', xml)
    out=[]
    for p in paras:
        texts = re.findall(r'<w:t[^>]*>(.*?)</w:t>', p, re.S)
        line = ''.join(texts)
        line = re.sub(r'<[^>]+>', '', line)
        if line.strip():
            out.append(line)
    return '\n'.join(out)
for f in ["D:/比赛内容/github.docx", "D:/比赛内容/sheet.docx"]:
    print("\n\n#################### FILE:", f, "####################")
    print(extract(f))
