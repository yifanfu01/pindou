#!/usr/bin/env python3
"""General-purpose image-to-fuse-bead converter (Pillow only).

Features: automatic subject crop/background removal, illustration/photo/pixel modes,
CIELAB palette matching, color-count control, small-speck cleanup, chart and BOM.
"""
from __future__ import annotations

import argparse, csv, math
from collections import Counter
from pathlib import Path
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont


def hex_rgb(value):
    s=value.strip().lstrip("#"); return tuple(int(s[i:i+2],16) for i in (0,2,4))


def rgb_lab(c):
    v=[]
    for x in c:
        x=x/255.0; v.append(((x+0.055)/1.055)**2.4 if x>0.04045 else x/12.92)
    r,g,b=v
    x=(0.4124564*r+0.3575761*g+0.1804375*b)/0.95047
    y=(0.2126729*r+0.7151522*g+0.0721750*b)
    z=(0.0193339*r+0.1191920*g+0.9503041*b)/1.08883
    f=lambda q: q**(1/3) if q>0.008856 else 7.787*q+16/116
    fx,fy,fz=f(x),f(y),f(z)
    return (116*fy-16,500*(fx-fy),200*(fy-fz))


def de76(a,b): return sum((x-y)**2 for x,y in zip(a,b))


def load_palette(path):
    with open(path,newline="",encoding="utf-8-sig") as f: rows=list(csv.DictReader(f))
    if not rows or not {"code","name","hex"}.issubset(rows[0]):
        raise ValueError("色卡必须包含 code,name,hex 三列")
    return [(r["code"],r["name"],hex_rgb(r["hex"]),rgb_lab(hex_rgb(r["hex"]))) for r in rows]


def edge_background_mask(im, threshold):
    """Return edge-connected near-white background pixels."""
    w,h=im.size; p=im.load(); seen=set(); stack=[]
    stack += [(x,0) for x in range(w)] + [(x,h-1) for x in range(w)]
    stack += [(0,y) for y in range(h)] + [(w-1,y) for y in range(h)]
    while stack:
        x,y=stack.pop()
        if (x,y) in seen or min(p[x,y])<threshold: continue
        seen.add((x,y))
        for q in ((x-1,y),(x+1,y),(x,y-1),(x,y+1)):
            if 0<=q[0]<w and 0<=q[1]<h: stack.append(q)
    return seen


def auto_crop(im, threshold, padding):
    # Work on a thumbnail so large photographs remain fast.
    probe=im.copy(); probe.thumbnail((600,600),Image.Resampling.LANCZOS)
    bg=edge_background_mask(probe,threshold)
    fg=[(x,y) for y in range(probe.height) for x in range(probe.width) if (x,y) not in bg]
    if not fg: return im
    xs=[q[0] for q in fg]; ys=[q[1] for q in fg]
    x0,x1=min(xs),max(xs)+1; y0,y1=min(ys),max(ys)+1
    pad=round(max(x1-x0,y1-y0)*padding)
    x0=max(0,x0-pad); y0=max(0,y0-pad); x1=min(probe.width,x1+pad); y1=min(probe.height,y1+pad)
    sx=im.width/probe.width; sy=im.height/probe.height
    return im.crop((round(x0*sx),round(y0*sy),round(x1*sx),round(y1*sy)))


def prepare(im,w,h,mode,threshold,padding):
    if threshold: im=auto_crop(im,threshold,padding)
    scale=min(w/im.width,h/im.height); size=(max(1,round(im.width*scale)),max(1,round(im.height*scale)))
    if mode=="pixel": resample=Image.Resampling.NEAREST
    else: resample=Image.Resampling.LANCZOS
    small=im.resize(size,resample)
    if mode=="illustration":
        small=ImageEnhance.Contrast(small).enhance(1.10)
        small=small.filter(ImageFilter.UnsharpMask(radius=0.8,percent=180,threshold=5))
    elif mode=="photo":
        small=ImageEnhance.Color(small).enhance(0.95)
        small=small.filter(ImageFilter.UnsharpMask(radius=0.6,percent=80,threshold=7))
    canvas=Image.new("RGB",(w,h),"white"); ox=(w-size[0])//2; oy=(h-size[1])//2
    canvas.paste(small,(ox,oy)); return canvas


def reduce_colors(im, blank, max_colors):
    if max_colors<=0: return im
    # White is temporarily reserved for empty cells, leaving requested colors for subject.
    q=im.quantize(colors=max_colors+1,method=Image.Quantize.MEDIANCUT,dither=Image.Dither.NONE).convert("RGB")
    # Median-cut tends to discard thin dark outlines because they occupy few pixels.
    # Preserve the original value for sufficiently dark cells (eyes, nose, line art).
    dark=[]
    for y in range(im.height):
        for x in range(im.width):
            if (x,y) in blank: q.putpixel((x,y),(255,255,255)); continue
            c=im.getpixel((x,y))
            if rgb_lab(c)[0] < 58: dark.append((x,y,c))
    if dark:
        strip=Image.new("RGB",(len(dark),1))
        for i,(_,_,c) in enumerate(dark): strip.putpixel((i,0),c)
        strip=strip.quantize(colors=min(3,len(dark)),method=Image.Quantize.MEDIANCUT,dither=Image.Dither.NONE).convert("RGB")
        for i,(x,y,_) in enumerate(dark): q.putpixel((x,y),strip.getpixel((i,0)))
    return q


def cleanup(codes,palette_lookup,passes=1):
    h=len(codes); w=len(codes[0])
    for _ in range(passes):
        old=[r[:] for r in codes]
        for y in range(h):
            for x in range(w):
                c=old[y][x]
                if not c: continue
                ns=[old[yy][xx] for xx,yy in ((x-1,y),(x+1,y),(x,y-1),(x,y+1)) if 0<=xx<w and 0<=yy<h and old[yy][xx]]
                # Preserve dark single-pixel details such as eyes and outlines.
                if c not in ns and ns and palette_lookup[c][2][0]>42:
                    winner,count=Counter(ns).most_common(1)[0]
                    if count>=3: codes[y][x]=winner


def save_outputs(out,codes,palette,w,h,cell):
    lookup={c:(n,rgb,lab) for c,n,rgb,lab in palette}; colors=[[lookup[c][1] if c else (255,255,255) for c in row] for row in codes]
    with open(str(out)+"_matrix.csv","w",newline="",encoding="utf-8-sig") as f:
        wr=csv.writer(f); wr.writerow(["行/列"]+list(range(1,w+1)))
        for i,row in enumerate(codes,1): wr.writerow([i]+row)
    counts=Counter(c for row in codes for c in row if c)
    with open(str(out)+"_materials.csv","w",newline="",encoding="utf-8-sig") as f:
        wr=csv.writer(f); wr.writerow(["色号","名称","HEX","数量"])
        for c,n in counts.most_common(): wr.writerow([c,lookup[c][0],"#%02X%02X%02X"%lookup[c][1],n])
    preview=Image.new("RGB",(w,h))
    for y,row in enumerate(colors):
        for x,c in enumerate(row): preview.putpixel((x,y),c)
    preview.resize((w*16,h*16),Image.Resampling.NEAREST).save(str(out)+"_preview.png")
    margin=42; chart=Image.new("RGB",(margin+w*cell+1,margin+h*cell+1),"white"); d=ImageDraw.Draw(chart); font=ImageFont.load_default()
    for y in range(h):
        for x in range(w):
            x0=margin+x*cell; y0=margin+y*cell; fill=colors[y][x]
            d.rectangle((x0,y0,x0+cell,y0+cell),fill=fill,outline="#AAAAAA")
            if codes[y][x]:
                s=codes[y][x]; box=d.textbbox((0,0),s,font=font); ink="white" if rgb_lab(fill)[0]<48 else "black"
                d.text((x0+(cell-box[2])/2,y0+(cell-(box[3]-box[1]))/2),s,font=font,fill=ink)
    for x in range(w):
        if x%5==0: d.text((margin+x*cell+2,25),str(x+1),font=font,fill="black")
    for y in range(h):
        if y%5==0: d.text((8,margin+y*cell+3),str(y+1),font=font,fill="black")
    chart.save(str(out)+"_chart.png")
    return counts


def main():
    p=argparse.ArgumentParser(description="图片转 MARD/自定义色卡拼豆图纸")
    p.add_argument("image"); p.add_argument("--size",type=int,help="正方形尺寸，例如 30")
    p.add_argument("--width",type=int,default=64); p.add_argument("--height",type=int,default=64)
    p.add_argument("--palette",default=str(Path(__file__).with_name("mard_221_palette.csv")))
    p.add_argument("--mode",choices=["illustration","photo","pixel"],default="illustration")
    p.add_argument("--max-colors",type=int,default=10); p.add_argument("--output",default="bead_pattern")
    p.add_argument("--background-threshold",type=int,default=245); p.add_argument("--crop-padding",type=float,default=.06)
    p.add_argument("--cleanup-passes",type=int,default=1); p.add_argument("--cell",type=int,default=22)
    a=p.parse_args(); w=h=a.size if a.size else None; w=w or a.width; h=h or a.height
    palette=load_palette(a.palette); src=Image.open(a.image).convert("RGB")
    canvas=prepare(src,w,h,a.mode,a.background_threshold,a.crop_padding)
    blank=edge_background_mask(canvas,a.background_threshold) if a.background_threshold else set()
    work=reduce_colors(canvas,blank,a.max_colors)
    codes=[]
    for y in range(h):
        row=[]
        for x in range(w):
            if (x,y) in blank: row.append(""); continue
            lab=rgb_lab(work.getpixel((x,y))); row.append(min(palette,key=lambda q:de76(lab,q[3]))[0])
        codes.append(row)
    lookup={c:(n,rgb,lab) for c,n,rgb,lab in palette}; cleanup(codes,lookup,a.cleanup_passes)
    out=Path(a.output); out.parent.mkdir(parents=True,exist_ok=True)
    counts=save_outputs(out,codes,palette,w,h,a.cell)
    print(f"完成：{w}×{h}，{sum(counts.values())} 颗，{len(counts)} 种颜色")


if __name__=="__main__": main()
