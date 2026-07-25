import sys, os, hashlib
from PIL import Image, ImageChops

def stats(p):
    im = Image.open(p).convert("RGB")
    px = im.load(); w,h = im.size
    n=0; chroma_max=0; chroma_sum=0; mono=0; rs=gs=bs=0
    for y in range(0,h,7):
        for x in range(0,w,7):
            r,g,b = px[x,y]
            n+=1; rs+=r; gs+=g; bs+=b
            c = max(abs(r-g), abs(g-b), abs(r-b))
            chroma_sum+=c; chroma_max=max(chroma_max,c)
            if r==g==b: mono+=1
    return dict(path=p, size=(w,h), mean=(rs/n,gs/n,bs/n),
                chroma_mean=chroma_sum/n, chroma_max=chroma_max,
                pct_mono=100.0*mono/n,
                md5=hashlib.md5(open(p,'rb').read()).hexdigest()[:12])

a = stats(sys.argv[1]); b = stats(sys.argv[2])
for s in (a,b):
    print("%-18s %s  mean RGB=(%.1f, %.1f, %.1f)  chroma mean=%.2f max=%d  R==G==B on %.2f%%  md5=%s"
          % (os.path.basename(s['path']), s['size'], s['mean'][0], s['mean'][1], s['mean'][2],
             s['chroma_mean'], s['chroma_max'], s['pct_mono'], s['md5']))

diff = ImageChops.difference(Image.open(sys.argv[1]).convert("RGB"),
                             Image.open(sys.argv[2]).convert("RGB"))
print()
print("diff bbox :", diff.getbbox())
print("identical :", a['md5']==b['md5'])
