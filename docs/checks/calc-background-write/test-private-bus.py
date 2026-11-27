import os, subprocess, sys, tempfile, shutil
runtime=tempfile.mkdtemp(prefix='ccdesk-test-bus-')
env=dict(os.environ, XDG_RUNTIME_DIR=runtime)
procs=[]
try:
    for kind, args in [('session',['--session']),('a11y',['--config-file=/usr/share/defaults/at-spi2/accessibility.conf'])]:
        p=subprocess.Popen(['dbus-daemon',*args,'--nofork','--print-address=1'],env=env,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,start_new_session=True)
        procs.append(p)
        address=p.stdout.readline().strip()
        if not address:raise RuntimeError('bus did not start')
        env['DBUS_SESSION_BUS_ADDRESS' if kind=='session' else 'AT_SPI_BUS_ADDRESS']=address
    registry=subprocess.Popen(['/usr/lib/at-spi2-registryd'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    procs.append(registry)
    code=subprocess.call(['xvfb-run','-a','uv','run','pytest','-q',*sys.argv[1:]],env=env)
finally:
    for p in reversed(procs):
        p.terminate()
        p.wait(timeout=5)
    shutil.rmtree(runtime)
sys.exit(code)
