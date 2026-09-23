ALTER TABLE robot_runtime_state
  ADD COLUMN IF NOT EXISTS drive_mode_json TEXT NOT NULL
  DEFAULT '{"mode":"idle","state":"idle","sessionId":null,"message":null}';

ALTER TABLE robot_commands
  DROP CONSTRAINT IF EXISTS robot_commands_operation_check;

ALTER TABLE robot_commands
  ADD CONSTRAINT robot_commands_operation_check CHECK (operation IN (
    'start', 'finish', 'cancel',
    'navigation_preview', 'navigation_start', 'navigation_cancel',
    'drive_mode_start', 'drive_mode_pause', 'drive_mode_resume',
    'drive_mode_stop',
    'room_split', 'room_merge', 'rooms_save', 'zones_apply'
  ));
