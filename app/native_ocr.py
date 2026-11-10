#!/usr/bin/env python3
"""Fully local, bounded Tesseract OCR for image uploads and scanned PDF pages.

No cloud requests; no user-provided executable paths or shell commands.
"""
from __future__ import annotations
import io, re, shutil, subprocess, tempfile
from pathlib import Path

MAX_PIXELS=24_000_000
MAX_IMAGE=12*1024*1024
MAX_TEXT=256*1024
OCR_PDF_PAGES=12
LANG_PATTERN=re.compile(r'[a-z]{3}(?:\+[a-z]{3}){0,2}\Z')


def languages():
    if not shutil.which('tesseract'):return []
    try:
        p=subprocess.run(['tesseract','--list-langs'],capture_output=True,text=True,timeout=8,check=False)
    except (OSError,subprocess.TimeoutExpired):return []
    return sorted(set(re.findall(r'^([a-z]{3})$',p.stdout+'\n'+p.stderr,re.M)))


def _lang(code):
    available=languages()
    if not available:raise RuntimeError('Install tesseract-ocr and the language pack (e.g. tesseract-ocr-eng)')
    if code=='auto':return 'eng' if 'eng' in available else available[0]
    if not isinstance(code,str) or not LANG_PATTERN.fullmatch(code):raise ValueError('Invalid OCR language code')
    if any(piece not in available for piece in code.split('+')):raise ValueError('OCR language not installed: '+code)
    return code


def _prepare_image(raw):
    if not isinstance(raw,bytes) or len(raw)>MAX_IMAGE or len(raw)<12:
        raise ValueError('OCR image must be under 12 MiB')
    try:
        from PIL import Image,ImageOps,UnidentifiedImageError
    except ImportError as exc:raise RuntimeError('Install python3-pil for safe image OCR decoding') from exc
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in ('JPEG','PNG','WEBP','TIFF'):
                raise ValueError('Only JPG, PNG, WEBP and TIFF OCR images are supported')
            if image.width*image.height>MAX_PIXELS or image.width<1 or image.height<1:
                raise ValueError('OCR image exceeds 24 megapixels')
            image=ImageOps.exif_transpose(image).convert('L')
            out=io.BytesIO();image.save(out,format='PNG');return out.getvalue()
    except (OSError,ValueError,Image.DecompressionBombError) as exc:
        raise ValueError('Invalid or oversized OCR image') from exc


def image_text(raw,language='auto',runner=subprocess.run):
    lang=_lang(language)
    data=_prepare_image(raw)
    try:
        result=runner(['tesseract','stdin','stdout','-l',lang,'--psm','3'],input=data,
                      stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=35,check=False)
    except subprocess.TimeoutExpired as exc:raise RuntimeError('OCR timed out after 35 seconds') from exc
    if result.returncode:raise RuntimeError('Tesseract could not read the image')
    if len(result.stdout)>MAX_TEXT:raise ValueError('OCR result exceeds 256 KiB')
    return {'text':result.stdout.decode('utf-8','replace').strip(), 'language':lang, 'engine':'tesseract-offline'}


def pdf_page_text(pdf_path,page_number,language='auto'):
    """OCR exactly one page at modest DPI, bounded by timeout and the PIL pixel limit."""
    if not shutil.which('pdftoppm'):raise RuntimeError('Install poppler-utils for PDF page OCR')
    if type(page_number) is not int or not 1<=page_number<=200:raise ValueError('Invalid PDF page')
    with tempfile.TemporaryDirectory(prefix='doomsday-ocr-') as folder:
        base=Path(folder)/'page'
        try:
            p=subprocess.run(['pdftoppm','-f',str(page_number),'-l',str(page_number),
                              '-singlefile','-gray','-r','120','-png',str(pdf_path),str(base)],
                             capture_output=True,timeout=35,check=False)
        except subprocess.TimeoutExpired as exc:raise RuntimeError('PDF page render timed out') from exc
        if p.returncode:raise ValueError('Unable to render scanned PDF page')
        file=base.with_suffix('.png')
        if file.stat().st_size>MAX_IMAGE:raise ValueError('Rendered PDF image exceeds OCR memory limit')
        return image_text(file.read_bytes(),language)['text']


def main(argv=None):
    import argparse, json
    parser=argparse.ArgumentParser(description='DoomsdayOS local Tesseract OCR (no AI or Internet)')
    parser.add_argument('command',choices=['languages','image','pdf-page'])
    parser.add_argument('path',nargs='?')
    parser.add_argument('--language',default='auto')
    parser.add_argument('--page',type=int,default=1)
    args=parser.parse_args(argv)
    if args.command=='languages':
        print('Installed Tesseract OCR languages: '+(', '.join(languages()) or '(none)'));return 0
    if not args.path:parser.error('Supply a local image or PDF path')
    path=Path(args.path).resolve(strict=True)
    if not path.is_file() or path.is_symlink():parser.error('Not a regular file')
    if args.command=='image':
        if path.stat().st_size>MAX_IMAGE:parser.error('Image exceeds 12 MiB')
        text=image_text(path.read_bytes(),args.language)['text']
    else:
        if path.suffix.lower()!='.pdf':parser.error('Expected a PDF file')
        text=pdf_page_text(path,args.page,args.language)
    print(text)
    return 0
if __name__=='__main__':raise SystemExit(main())
