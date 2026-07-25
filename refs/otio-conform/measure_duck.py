import subprocess, re


def seg(f, ss, to):
    out = subprocess.run(
        ['ffmpeg', '-hide_banner', '-nostats', '-ss', str(ss), '-to', str(to),
         '-i', f, '-map', '0:a:0', '-af', 'volumedetect', '-f', 'null', '-'],
        capture_output=True, text=True).stderr
    m = re.search(r'mean_volume:\s*(-?[\d.]+) dB', out)
    return float(m.group(1)) if m else None


# T07 duck keyframes: clip frames 0, 250, 300, 900, 950 @ 50fps -> 0s, 5s, 6s, 18s, 19s
# T07 envelope: 0 dB outside, -15 dB between f300-900
# T05 control:  flat -10 dB throughout
windows = [('before duck', 1, 5, '+10 (0 vs -10)'),
           ('inside duck', 7, 17, '-5 (-15 vs -10)'),
           ('after duck', 19, 20, '+10 (0 vs -10)')]

print('%-13s %10s %10s %9s   %s' % ('window', 'T07 duck', 'T05 ctrl', 'delta', 'expected'))
for lbl, a, b, exp in windows:
    d = seg('aud_T07_duck.mov', a, b)
    c = seg('aud_T05_control_full.mov', a, b)
    print('%-13s %10.1f %10.1f %+9.1f   %s' % (lbl, d, c, d - c, exp))
