import sys, json, subprocess, time
sys.path.insert(0,'/tmp')
from ccdesk import a11y_helper as h
from ccdesk.desktop import Desktop
from ccdesk.calc_clipboard import snapshot
import gi
gi.require_version('Gtk','3.0')
from gi.repository import Gdk, Gtk

# A binary-only clipboard proves that restoration is not a text round trip.
original=b'\x89PNG\x00\xffbinary clipboard bytes\x00\x01'
subprocess.run(['xclip','-selection','clipboard','-t','image/png'],input=original,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
time.sleep(.1)
clipboard=Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
before=snapshot(clipboard)
d=Desktop(screen_diff=False)
d.ui_tree(window='cells.fods',query='Sheet',shot_fallback=False)
ref=next(k for k,v in d.a11y.refs.items() if v.role=='table')
answer=d.set_value(ref,'binary restore check',cell='D9')
after=snapshot(clipboard)
assert before==after,(before,after)
print(json.dumps({'binary_restore_after_success':before==after,'targets':list(before),'result':answer}),flush=True)
# Force a selection failure after taking ownership, with the real clipboard owner.
frame=h.pick_window('cells.fods')[0][2]
def walk(o):
 yield o
 if o.get_role_name()=='table':return
 for _,c in h.children(o):yield from walk(c)
table=next(o for o in walk(frame) if o.get_role_name()=='table')
select=h.calc_select
def fail(*args):raise ValueError('injected selection failure')
h.calc_select=fail
try:
 h.calc_set({'cell':'E10','text':'failure check'},table)
 raise AssertionError('expected failure')
except ValueError as exc:
 assert 'injected selection failure' in str(exc)
 print(json.dumps({'injected_error':str(exc),'binary_restore_after_failure':snapshot(clipboard)==before}),flush=True)
 assert snapshot(clipboard)==before
finally:h.calc_select=select
# Table addressing works well outside the initial viewport, without enumerating it.
print(json.dumps({'distant_click':d.click_element(ref,cell='AA1001')}),flush=True)
selected=h.Atspi.Selection.get_selected_child(table,0)
assert tuple(h.Atspi.TableCell.get_position(selected))==(True,1000,26)
assert h.Atspi.Selection.get_n_selected_children(table)==1
print(json.dumps({'selected_position':[1000,26],'selection_count':1}),flush=True)
