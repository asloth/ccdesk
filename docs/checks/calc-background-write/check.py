import sys, json, time, threading
sys.path.insert(0, '/tmp')
from Xlib import X, display
from ccdesk.desktop import Desktop
from ccdesk.backends.x11 import X11Backend
from ccdesk import a11y_helper as h

d = display.Display()
r = d.screen().root

def snap(conn=d):
    root = conn.screen().root
    p = root.query_pointer()
    def prop(name):
        v = root.get_full_property(conn.intern_atom(name), X.AnyPropertyType)
        return list(v.value) if v else []
    return {'active': prop('_NET_ACTIVE_WINDOW'), 'stack': prop('_NET_CLIENT_LIST_STACKING'),
            'pointer': [p.root_x,p.root_y], 'keys': open('/tmp/keylog').read()}

# Use the public Desktop entry points and real helper process, without an agent cursor.
desk = Desktop(backend=X11Backend(), screen_diff=False)
result = desk.ui_tree(window='cells.fods', query='Sheet', shot_fallback=False)
ref = next(k for k,v in desk.a11y.refs.items() if v.role == 'table')
records=[]
for address, text in [('A1','43')]:
    before=snap(); changes=[]; stop=threading.Event()
    def monitor():
        md=display.Display()
        while not stop.is_set():
            state=snap(md)
            if state != before: changes.append(state)
            stop.wait(.005)
        md.close()
    th=threading.Thread(target=monitor);th.start()
    try:
        answer=desk.set_value(ref,text,cell=address)
    except Exception as exc:
        answer={'error':str(exc)}
    time.sleep(.1);after=snap();stop.set();th.join()
    record=dict(cell=address,text=text,result=answer,before=before,after=after,transient_changes=changes)
    print(json.dumps(record),flush=True);records.append(record)
# Independent fresh AT-SPI numeric readback and distant exclusive selection.
frame=h.pick_window('cells.fods')[0][2]
def walk(o):
    yield o
    if o.get_role_name()=='table':return
    for _,c in h.children(o):yield from walk(c)
table=next(o for o in walk(frame) if o.get_role_name()=='table')
for row,col in [(0,0)]:
    cell=h.Atspi.Table.get_accessible_at(table,row,col)
    print(json.dumps({'readback':[row,col], 'text':h.full_text(cell), 'value':h.Atspi.Value.get_current_value(cell),'attributes':cell.get_attributes()}),flush=True)
assert all(row['result'].get('verified') and row['before'] == row['after']
           and not row['transient_changes'] for row in records)
print(json.dumps({'distant_click':desk.click_element(ref,cell='AA1001')}),flush=True)
selected=h.Atspi.Selection.get_selected_child(table,0)
assert tuple(h.Atspi.TableCell.get_position(selected))==(True,1000,26)
assert h.Atspi.Selection.get_n_selected_children(table)==1
open('/tmp/calc-evidence.json','w').write(json.dumps(records,indent=2))
