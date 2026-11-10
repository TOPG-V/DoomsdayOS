import io, json, os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
import native_ocr as ocr
import system_maintenance as maintenance
import ai_gateway as gateway
import research_library as research
import agent_tools

class OCRTests(unittest.TestCase):
    def png(self):
        from PIL import Image
        image=Image.new('RGB',(300,70),'white')
        output=io.BytesIO();image.save(output,format='PNG');return output.getvalue()

    def test_local_ocr_no_model_required(self):
        with mock.patch.object(ocr,'languages',return_value=['eng','pol']):
            runner=mock.Mock(return_value=mock.Mock(returncode=0,stdout=b'Storage policy',stderr=b''))
            result=ocr.image_text(self.png(),'eng',runner=runner)
            self.assertEqual(result['text'],'Storage policy')
            self.assertEqual(runner.call_args.args[0][:4],['tesseract','stdin','stdout','-l'])
            self.assertNotIn('http',str(runner.call_args))

    def test_reject_uninstalled_languages_and_injection(self):
        with mock.patch.object(ocr,'languages',return_value=['eng','pol']):
            for value in ('eng; rm -rf /','../../etc/shadow','rus','',123):
                with self.subTest(value=value),self.assertRaises(ValueError):ocr._lang(value)
            self.assertEqual(ocr._lang('eng+pol'),'eng+pol')

    def test_image_pixel_cap_and_bad_type(self):
        from PIL import Image
        img=Image.new('RGB',(5000,5000))
        out=io.BytesIO();img.save(out,format='PNG')
        with self.assertRaises(ValueError):ocr._prepare_image(out.getvalue())
        with self.assertRaises(ValueError):ocr._prepare_image(b'not-an-image-data')

    def test_core_mode_ocr_http_api_action(self):
        import base64
        image='data:image/png;base64,'+base64.b64encode(self.png()).decode()
        with mock.patch.object(gateway,'ai_enabled',return_value=False),mock.patch.object(gateway.native_ocr,'image_text',return_value={'text':'OCR works'}) as mocked:
            self.assertEqual(gateway.dispatch({'action':'ocr_image','image':image})['text'],'OCR works')
            self.assertTrue(mocked.called)

    def test_pdf_ocr_empty_page_only(self):
        import shutil
        if not (shutil.which('pdftotext') and shutil.which('pdftoppm')):self.skipTest('Poppler not installed')
        from reportlab.pdfgen.canvas import Canvas
        data=io.BytesIO();pdf=Canvas(data)
        pdf.drawString(50,700,'The textual first page is already indexed.')
        pdf.showPage();pdf.line(12,12,14,14);pdf.showPage();pdf.save()
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'sample.pdf';f.write_bytes(data.getvalue())
            with mock.patch.object(ocr,'languages',return_value=['eng']),mock.patch.object(ocr,'pdf_page_text',return_value='SECOND PAGE OCR') as mocked:
                pages=research._pdf_pages(f)
                self.assertIn('textual first page',pages[0]);self.assertIn('SECOND PAGE',pages[1]);self.assertEqual(mocked.call_count,1)
                self.assertEqual(mocked.call_args.args[1],2)

class MaintenanceTests(unittest.TestCase):
    def test_only_safe_named_scripts(self):
        for name in (';rm -rf /','../../etc/shadow','bash','custom'):
            with self.subTest(name=name),self.assertRaises(ValueError):maintenance.script_report(name)
        with mock.patch.object(maintenance,'_run',return_value={'code':0,'text':'ok'}),mock.patch.object(maintenance,'_readonly_report',return_value={'code':0,'text':'readonly=on'}):
            result=maintenance.script_report('all')
            self.assertEqual(set(result['results']),{'df','zpool','mounts','failed','dpkg','readonly'})

    def test_analyze_reports_degraded_and_readonly_problem(self):
        results={'df':{'code':0,'text':'file'},'zpool':{'code':0,'text':'pool: tank STATE: DEGRADED'},'mounts':{'code':0,'text':'ZFS'},
                 'failed':{'code':0,'text':'doomsday-kiwix.service failed'},'dpkg':{'code':0,'text':'package is not configured'},
                 'readonly':{'code':0,'text':'zim_dir dataset tank/zims readonly=off'}}
        with mock.patch.object(maintenance,'script_report',return_value={'results':results}):
            data=maintenance.analyze()
        self.assertTrue(data['read_only'])
        self.assertIn('protect-zims',[a['id'] for a in data['actions']])
        self.assertIn('restart-doomsday-kiwix',[a['id'] for a in data['actions']])
        self.assertTrue(any(f['severity']=='critical' for f in data['findings']))

    def test_approved_repair_requires_root_and_typed_confirmation(self):
        fake=mock.Mock(return_value={'code':0,'text':'ok'})
        with mock.patch.object(maintenance.os,'geteuid',return_value=1000):
            with self.assertRaises(PermissionError):maintenance.apply('restart-doomsday-kiwix','APPLY-restart-doomsday-kiwix',runner=fake)
        with mock.patch.object(maintenance.os,'geteuid',return_value=0):
            with self.assertRaises(ValueError):maintenance.apply('restart-doomsday-kiwix','YES',runner=fake)
            with self.assertRaises(ValueError):maintenance.apply('rm','APPLY-rm',runner=fake)
            fake.assert_not_called()
            with mock.patch.object(maintenance,'_write_log'):
                maintenance.apply('restart-doomsday-kiwix','APPLY-restart-doomsday-kiwix',runner=fake)
                self.assertEqual(fake.call_args.args[0],['systemctl','restart','doomsday-kiwix.service'])

    def test_automated_repairs_are_limited_and_rate_limited(self):
        with tempfile.TemporaryDirectory() as d,mock.patch.object(maintenance,'STATE',Path(d)),mock.patch.object(maintenance.os,'geteuid',return_value=0):
            def run(args,timeout=12):
                if args[:2]==['systemctl','is-failed']:
                    return {'code':0 if args[2]=='doomsday-kiwix.service' else 1,
                            'text':'failed' if args[2]=='doomsday-kiwix.service' else 'active'}
                self.assertEqual(args,['systemctl','restart','doomsday-kiwix.service'])
                return {'code':0,'text':'ok'}
            for i in range(3):
                result=maintenance.auto_heal(runner=run)
                if i==2:self.assertEqual(result['restarts'][0]['result'],'restart limit reached')
                else:self.assertEqual(result['restarts'][0]['code'],0)

    def test_agent_cannot_run_arbitrary_repair_tool(self):
        self.assertNotIn('run_shell',agent_tools.NAMES)
        self.assertNotIn('apply_repair',agent_tools.NAMES)
        with mock.patch.object(maintenance,'analyze',return_value={'findings':[],'read_only':True}):
            self.assertTrue(agent_tools.execute_tool('system_diagnostics',{})['read_only'])
        with self.assertRaises(ValueError):agent_tools.execute_tool('shell',{'cmd':'rm -rf /'})

if __name__=='__main__':unittest.main()
