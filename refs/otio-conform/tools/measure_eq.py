import subprocess, numpy as np
SR=48000
def pcm(p,s=2.0,d=20.0):
    o=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-ss',str(s),'-t',str(d),
        '-i',p,'-map','0:a:0','-ac','1','-ar',str(SR),'-f','f32le','-'],capture_output=True).stdout
    return np.frombuffer(o,dtype=np.float32)
def spec(x,nfft=8192):
    w=np.hanning(nfft); acc=np.zeros(nfft//2+1); n=0
    for i in range(0,len(x)-nfft,nfft//2):
        acc+=np.abs(np.fft.rfft(x[i:i+nfft]*w)); n+=1
    return np.fft.rfftfreq(nfft,1.0/SR), acc/max(n,1)
def band(f,m,fc,frac=3.0):
    lo,hi=fc/(2**(0.5/frac)), fc*(2**(0.5/frac)); k=(f>=lo)&(f<=hi)
    return 20*np.log10(max(np.sqrt((m[k]**2).sum()),1e-12))

fc_,mc = spec(pcm('aud_T61_control.mov'))
fe,me  = spec(pcm('aud_T67_noui.mov'))
print("EQ authored: band 2 -> 1000 Hz, +24.0 dB, Q 0.75")
print("Prediction: a PEAK at 1 kHz, tapering away on both sides.\n")
print("%8s %10s %10s %9s  %s" % ("Hz","control","eq","delta",""))
peak=(None,-99)
for fc in [63,125,250,500,700,1000,1400,2000,4000,8000,16000]:
    c=band(fc_,mc,fc); e=band(fe,me,fc); d=e-c
    if d>peak[1]: peak=(fc,d)
    bar="#"*max(0,int(round(d*2))) if d>0 else ""
    print("%8d %10.2f %10.2f %+9.2f  %s" % (fc,c,e,d,bar))
print("\nlargest boost at %d Hz (+%.2f dB)  <- authored frequency was 1000 Hz" % peak)
