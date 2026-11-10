#!/usr/bin/env python3
"""Runtime backend selection for llama.cpp; never installs drivers or recompiles binaries.

Only offloads if the existing llama-server actually advertises a compatible GPU
backend and system detection agrees. Unknown capability fails closed to CPU.
"""
from __future__ import annotations
import argparse, json, os, re, shutil, subprocess

CHOICES=('auto','cpu','cuda','rocm','vulkan')

def call(argv,timeout=8):
    try:
        result=subprocess.run(argv,capture_output=True,text=True,timeout=timeout,check=False)
        return (result.stdout+'\n'+result.stderr) if result.returncode==0 else ''
    except (OSError,subprocess.TimeoutExpired):
        return ''

def hardware():
    nvidia=bool(shutil.which('nvidia-smi') and re.search(r'GPU\s+\d+:',call(['nvidia-smi','-L'])))
    rocm=bool(shutil.which('rocminfo') and re.search(r'gfx[0-9a-f]+',call(['rocminfo'])))
    vulkan=bool(shutil.which('vulkaninfo') and re.search(r'(deviceName|device name)\s*[:=]',call(['vulkaninfo','--summary']),re.I))
    return {'cuda':nvidia,'rocm':rocm,'vulkan':vulkan}

def devices(executable='llama-server'):
    text=call([executable,'--list-devices'])
    found=set()
    for line in text.lower().splitlines():
        # Restrict matching to reported device lines; help text alone does not
        # confirm acceleration. CPU-only builds can print backend names in help.
        if not re.search(r'(\bdevice\b|\bgpu\b|\bid\s*\d+\b|\[\d+\])',line):
            continue
        if re.search(r'(cuda|ggml_cuda)',line):found.add('cuda')
        if re.search(r'(rocm|\bhip\b|ggml_hip)',line):found.add('rocm')
        if re.search(r'(vulkan|ggml_vk)',line):found.add('vulkan')
    return found

def choose(executable='llama-server',preferred=None,detected_hardware=None,detected_devices=None):
    selected=(preferred or os.environ.get('DOOMSDAY_LLAMA_BACKEND','auto')).lower()
    if selected not in CHOICES:raise ValueError('Backend must be one of '+', '.join(CHOICES))
    available_hw=hardware() if detected_hardware is None else detected_hardware
    available_build=devices(executable) if detected_devices is None else set(detected_devices)
    usable=[name for name in ('cuda','rocm','vulkan') if available_hw.get(name) and name in available_build]
    if selected=='cpu':backend='cpu';reason='CPU requested'
    elif selected=='auto':backend=usable[0] if usable else 'cpu';reason='Detected compatible backend' if usable else 'No confirmed compatible GPU backend'
    elif selected in usable:backend=selected;reason='Requested compatible backend'
    else:backend='cpu';reason=selected+' was unavailable; safe CPU fallback'
    return {'requested':selected,'backend':backend,'gpu_available':usable,
            'gpu_layers':0 if backend=='cpu' else 99,'reason':reason}

def main():
    parser=argparse.ArgumentParser(description='Inspect DoomsdayOS llama.cpp backend compatibility')
    parser.add_argument('--backend',choices=CHOICES,default='auto')
    parser.add_argument('--binary',default='llama-server')
    args=parser.parse_args()
    print(json.dumps(choose(args.binary,args.backend),indent=2))

if __name__=='__main__':main()
