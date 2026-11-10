#!/usr/bin/env python3
"""Opt-in local whisper.cpp ASR. No network, uploaded audio never persisted.

Requires manually installed multilingual whisper model. Browser microphone
permission is separate and explicitly requested by the browser.
"""
from __future__ import annotations
import os, shutil, subprocess, tempfile
from pathlib import Path

MODEL=Path(os.environ.get('DOOMSDAY_WHISPER_MODEL','/var/lib/doomsday-vault/models/ggml-small.bin'))
EXEC=os.environ.get('DOOMSDAY_WHISPER_CLI','whisper-cli')
AUDIO_MAX=16*1024*1024
LANGUAGES={
 'auto':'Automatic detection', 'en':'English','pl':'Polish','de':'German','fr':'French','es':'Spanish',
 'it':'Italian','pt':'Portuguese','nl':'Dutch','sv':'Swedish','fi':'Finnish','da':'Danish',
 'no':'Norwegian','is':'Icelandic','et':'Estonian','lv':'Latvian','lt':'Lithuanian',
 'cs':'Czech','sk':'Slovak','sl':'Slovenian','hr':'Croatian','bg':'Bulgarian','ro':'Romanian',
 'hu':'Hungarian','el':'Greek','mt':'Maltese','ga':'Irish','ca':'Catalan',
 'ru':'Russian','uk':'Ukrainian','be':'Belarusian','sr':'Serbian','bs':'Bosnian',
 'tr':'Turkish','ar':'Arabic','zh':'Chinese','ja':'Japanese',
}


def asr_status():
    return {'enabled':bool(MODEL.is_file() and not MODEL.is_symlink() and (shutil.which(EXEC) or Path(EXEC).is_file()) and shutil.which('ffmpeg')),
            'model_path':str(MODEL),'model_installed':MODEL.is_file(),'whisper_cli':bool(shutil.which(EXEC) or Path(EXEC).is_file()),
            'ffmpeg':bool(shutil.which('ffmpeg')),'languages':LANGUAGES,
            'note':'Multilingual speech recognition, accuracy depends on locally installed Whisper model. Requires CPU/RAM; no online service.'}


def transcribe_audio(data:bytes,content_type:str,language='auto',runner=subprocess.run):
    if language not in LANGUAGES:raise ValueError('Unsupported source language')
    if not data or len(data)>AUDIO_MAX:raise ValueError('Audio upload must be 1 byte to 16 MiB')
    mime=content_type.split(';',1)[0].lower().strip()
    if mime not in ('audio/wav','audio/x-wav','audio/webm','audio/ogg','audio/mpeg','audio/mp4','audio/aac'):
        raise ValueError('Unsupported audio format. Use WAV/WebM/OGG/MP3/MP4.')
    state=asr_status()
    if not state['enabled']:raise RuntimeError('Install whisper-cli, ffmpeg and a multilingual Whisper model at '+str(MODEL))
    ext={'audio/webm':'.webm','audio/ogg':'.ogg','audio/mpeg':'.mp3','audio/mp4':'.m4a','audio/aac':'.aac'}.get(mime,'.wav')
    with tempfile.TemporaryDirectory(prefix='doomsday-asr-') as tmp:
        root=Path(tmp);original=root/('input'+ext);wav=root/'normalized.wav';output=root/'transcript'
        original.write_bytes(data)
        # Bound decode length and resource usage; never invoke a shell.
        ff=runner(['ffmpeg','-hide_banner','-nostdin','-loglevel','error','-y','-i',str(original),'-t','90',
                   '-ar','16000','-ac','1','-c:a','pcm_s16le',str(wav)],capture_output=True,timeout=35,check=False)
        if ff.returncode!=0 or not wav.exists():raise ValueError('Unable to decode the audio file locally')
        cmd=[EXEC,'-m',str(MODEL),'-f',str(wav),'-l',language,'--no-timestamps','-otxt','-of',str(output)]
        done=runner(cmd,capture_output=True,timeout=180,check=False)
        transcript=Path(str(output)+'.txt')
        if done.returncode!=0 or not transcript.exists():raise RuntimeError('whisper.cpp transcription failed; check multilingual model and CLI version')
        result=transcript.read_text(encoding='utf-8',errors='replace').strip()[:12000]
        return {'transcript':result,'source_language':language,'engine':'local-whisper.cpp','max_audio_seconds':90}
