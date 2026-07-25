import subprocess, sys, numpy as np
SR=48000
def pcm(p, s=2.0, d=20.0):
    o=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-ss',str(s),'-t',str(d),
        '-i',p,'-map','0:a:0','-ac','1','-ar',str(SR),'-f','f32le','-'],capture_output=True).stdout
    return np.frombuffer(o,dtype=np.float32)
def spec(x,nfft=8192):
    w=np.hanning(nfft); acc=np.zeros(nfft//2+1); n=0
    for i in range(0,len(x)-nfft,nfft//2):
        acc+=np.abs(np.fft.rfft(x[i:i+nfft]*w)); n+=1
    return np.fft.rfftfreq(nfft,1.0/SR), acc/max(n,1)
def band(f,m,fc,frac=3.0):     # 1/3-octave band, tighter than before
    lo,hi=fc/(2**(0.5/frac)), fc*(2**(0.5/frac)); k=(f>=lo)&(f<=hi)
    return 20*np.log10(max(np.sqrt((m[k]**2).sum()),1e-12))

fc_, mc = spec(pcm('aud_T61_control.mov'))
fp, mp = spec(pcm('aud_T62_pitch12.mov'))

print("HYPOTHESIS: +12 semitones doubles every frequency.")
print("So energy at f in the control should reappear at 2f in the pitched render.\n")
print("%9s %9s %12s %12s %9s   %12s %9s" % ("f","2f","ctrl@f","pitch@2f","err","pitch@f","err"))
errs_shift=[]; errs_same=[]
for fc in [100,160,250,400,630,1000,1600,2500]:
    c  = band(fc_,mc,fc)
    p2 = band(fp,mp,fc*2)
    p1 = band(fp,mp,fc)
    errs_shift.append(abs(p2-c)); errs_same.append(abs(p1-c))
    print("%9d %9d %12.2f %12.2f %+9.2f   %12.2f %+9.2f" % (fc,fc*2,c,p2,p2-c,p1,p1-c))
print("\nmean |error| if pitch DID shift an octave : %.2f dB" % np.mean(errs_shift))
print("mean |error| if pitch did NOTHING         : %.2f dB" % np.mean(errs_same))
