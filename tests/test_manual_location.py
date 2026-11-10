"""Manual GPS-free map location: privacy, browser persistence and integration guards."""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

class ManualLocationTests(unittest.TestCase):
    def test_web_can_set_and_forget_location_without_gps(self):
        html=(ROOT/'app/web/index.html').read_text()
        js=(ROOT/'app/web/app.js').read_text()
        for item in ('manualLocationInput','manualLocationSave','manualLocationClear','manualLocationStart','manualLocationStatus'):
            self.assertIn('id="'+item+'"',html)
            self.assertIn("$('"+item+"')",js)
        self.assertIn('<option value="manualLocation">My location (manual)</option>',html)
        self.assertIn("if(target==='manualLocation')",js)
        self.assertIn('mapInfo()',js)
        self.assertIn('if(manualLocation)loc={...manualLocation}',js)
        self.assertIn("if(mode==='agent'&&$('agentUseLocation').checked)",js)
        self.assertIn('No GPS required',html)
        self.assertIn('window.DoomsdayManualLocation.save(position)',js)
        self.assertIn('window.DoomsdayManualLocation.clear()',js)

    def test_static_route_for_helper(self):
        gateway=(ROOT/'app/ai_gateway.py').read_text()
        self.assertIn("'/static/manual_location.js':'manual_location.js'",gateway)
        html=(ROOT/'app/web/index.html').read_text()
        self.assertLess(html.index('/static/manual_location.js'),html.index('/static/app.js'))

    @unittest.skipUnless(shutil.which('node'),'Node is unavailable')
    def test_local_storage_validation_and_no_external_dependencies(self):
        helper=ROOT/'app/web/manual_location.js'
        script=r'''
const assert=require('node:assert/strict');
const api=require(process.argv[1]);
const data=new Map();
const storage={getItem:k=>data.has(k)?data.get(k):null,setItem:(k,v)=>data.set(k,v),removeItem:k=>data.delete(k)};
assert.equal(api.load(storage),null);
const saved=api.save({lat:53.4808,lon:-2.2426,name:' Manchester '},storage);
assert.deepEqual(saved,{lat:53.4808,lon:-2.2426,name:'Manchester'});
assert.deepEqual(api.load(storage),saved);
for(const obj of [{lat:91,lon:1},{lat:2,lon:-181},{lat:NaN,lon:0},{lat:'2',lon:2},{lat:0,lon:Infinity},null])
 assert.throws(()=>api.save(obj,storage));
assert.deepEqual(api.load(storage),saved);
// Browser-storage-disabled mode retains a valid in-session point without crashing.
const blocked={getItem:()=>{throw Error('blocked')},setItem:()=>{throw Error('blocked')},removeItem:()=>{throw Error('blocked')}};
assert.equal(api.load(blocked),null);
assert.equal(api.save({lat:1.2,lon:3.4},blocked).lat,1.2);
api.clear(blocked);
// Corrupt browser entry cannot become a location.
storage.setItem(api.key,JSON.stringify({lat:999,lon:0}));
assert.equal(api.load(storage),null);
api.clear(storage);
assert.equal(api.load(storage),null);
'''
        done=subprocess.run(['node','-e',script,str(helper)],capture_output=True,text=True,timeout=15)
        self.assertEqual(done.returncode,0,done.stderr)

if __name__=='__main__':unittest.main()
