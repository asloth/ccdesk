import json,time,threading,subprocess,sys
from pathlib import Path
from Xlib import X,display
sys.path.insert(0,'/tmp')
from ccdesk.desktop import Desktop
from ccdesk.backends.x11 import X11Backend
from ccdesk.errors import CcdeskError
D=display.Display(); R=D.screen().root

def prop(name):
    p=R.get_full_property(D.intern_atom(name),X.AnyPropertyType)
    return list(p.value) if p else []
def snap():
    p=R.query_pointer()
    return {'active':prop('_NET_ACTIVE_WINDOW'),'stack':prop('_NET_CLIENT_LIST_STACKING'), 'pointer':[p.root_x,p.root_y], 'focus':getattr(D.get_input_focus().focus,'id',None)}
b=X11Backend()
d=Desktop(backend=b,agent='routes',shots_dir='/tmp/routes-shots',screen_diff=False)
d.ui_tree(window='Background fields')

if not any('Routes sentinel' in w.title for w in b.list_windows()):
    subprocess.Popen(['/usr/bin/python3','/tmp/routes-sentinel.py'])
for _ in range(100):
    ws=[w for w in b.list_windows() if 'Routes sentinel' in w.title]
    if ws:break
    time.sleep(.1)
assert ws
subprocess.run(['wmctrl','-ia',ws[0].id],check=True)
time.sleep(.5)
windows=b.list_windows();target=next(w for w in windows if 'Background fields' in w.title)
assert b.window_covered(target.id)
# Read Chromium refs again while it is covered.
tree=d.ui_tree(window='Background fields')

combo=next(k for k in d.a11y.refs if d.a11y.resolve(k)['role']=='combo box')
button=next(k for k in d.a11y.refs if d.a11y.resolve(k)['name']=='Submit')
entry=next(k for k in d.a11y.refs if d.a11y.resolve(k)['name']=='Input' and d.a11y.resolve(k)['role']=='entry')
results=[]
for value in ('old','new','old'):
    before=snap();samples=[];stop=threading.Event()
    def monitor():
        conn=display.Display();root=conn.screen().root
        while not stop.wait(.005):
            def p(n):
                v=root.get_full_property(conn.intern_atom(n),X.AnyPropertyType)
                return list(v.value) if v else []
            xy=root.query_pointer()
            samples.append({'active':p('_NET_ACTIVE_WINDOW'),'stack':p('_NET_CLIENT_LIST_STACKING'),'pointer':[xy.root_x,xy.root_y], 'focus':getattr(conn.get_input_focus().focus,'id',None)})
        conn.close()
    t=threading.Thread(target=monitor);t.start()
    try:
        selected=d.set_value(combo,value)
        pressed=d.click_element(button)
        try:d.set_value(entry,'forbidden')
        except CcdeskError as exc:
            refusal={'code':exc.code,'input_emitted':exc.input_emitted,'message':str(exc)}
        else:raise AssertionError('text was not refused')
        time.sleep(.2)
    finally:stop.set();t.join()
    after=snap()
    assert before==after and all(s==before for s in samples), (before,after,samples)
    assert selected['verified'] and pressed['activated']=='press'
    assert refusal['code']=='background_text_unsupported' and not refusal['input_emitted']
    # Read page status through AT-SPI, no DOM scripting.
    page=d.ui_tree(window='Background fields')
    status=[]
    for k in d.a11y.refs:
        node=d.a11y.detail(k)
        for field in ('text','value','name'):
            v=node.get(field,'')
            if 'submitted' in v:status.append(v)
    assert status, page
    report=next(json.loads(s) for s in status if s.startswith('{'))
    assert report['submitted']['dom']==['old','old',value]
    assert report['submitted']['state']['Select']==value
    assert report['state']['Input']=='old'
    results.append({'value':value,'selected':selected,'pressed':pressed,'refusal':refusal,
                    'before':before,'after':after,'samples':len(samples),'all_samples_unchanged':True,'status':report})
    # New tree maintains refs, reacquire by role/name.
    combo=next(k for k in d.a11y.refs if d.a11y.resolve(k)['role']=='combo box')
    button=next(k for k in d.a11y.refs if d.a11y.resolve(k)['name']=='Submit')
    entry=next(k for k in d.a11y.refs if d.a11y.resolve(k)['name']=='Input' and d.a11y.resolve(k)['role']=='entry')
Path('/tmp/routes-evidence.json').write_text(json.dumps(results,indent=2))
print(json.dumps(results,indent=2),flush=True)
d.a11y.close()
