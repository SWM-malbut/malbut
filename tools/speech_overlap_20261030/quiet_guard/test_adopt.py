import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import adopt
class Checks(unittest.TestCase):
 def test_existing_user_unit_is_preserved_before_service_mutation(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'normal.service';p.write_text('user content')
   with patch.object(adopt,'UNIT',p),patch.object(adopt,'command') as call:
    with self.assertRaises(AssertionError):adopt.main()
    call.assert_not_called();self.assertEqual(p.read_text(),'user content')
 def test_rollback_refuses_changed_unit_before_service_mutation(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);p=root/'normal.service';p.write_text('changed');(root/'created-unit.json').write_text('{}')
   with patch.object(adopt,'ROOT',root),patch.object(adopt,'UNIT',p),patch.object(adopt,'command') as call:
    with self.assertRaises(AssertionError):adopt.rollback()
    call.assert_not_called();self.assertEqual(p.read_text(),'changed')
 def test_recovery_before_unit_creation_keeps_original_service(self):
  with tempfile.TemporaryDirectory() as t,patch.object(adopt,'ROOT',Path(t)),patch.object(adopt,'command') as call:
   adopt.rollback();call.assert_not_called()
if __name__=='__main__':unittest.main()
